"""GET /stores/{store_id}/anomalies — active operational anomalies."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal, Optional
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import and_, distinct, func, select, text

from app.db import events, get_conn, store_sessions

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["anomalies"])

QUEUE_WARN_THRESHOLD = 5
QUEUE_CRITICAL_THRESHOLD = 10
DEAD_ZONE_WINDOW_MIN = 30
STALE_CAMERA_WINDOW_MIN = 10
CONVERSION_DROP_RATIO = 0.8
CONVERSION_MIN_TODAY_SESSIONS = 10
CONVERSION_MIN_HIST_DAYS = 7


# ---------- response models ----------

class Anomaly(BaseModel):
    anomaly_id: str
    type: str
    severity: Literal["INFO", "WARN", "CRITICAL"]
    detail: str
    suggested_action: str
    detected_at: str


class AnomaliesResponse(BaseModel):
    store_id: str
    checked_at: str
    active_anomalies: list[Anomaly]


# ---------- helpers ----------

def _format_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    normalised = s[:-1] + "+00:00" if s.endswith("Z") else s
    return datetime.fromisoformat(normalised)


def _layout_for(request: Request, store_id: str) -> Optional[dict]:
    layout = getattr(request.app.state, "store_layout", None)
    if layout and layout.get("store_id") == store_id:
        return layout
    return None


def _mk(
    type_: str,
    severity: str,
    detail: str,
    suggested_action: str,
    detected_at: datetime,
) -> dict:
    return {
        "anomaly_id": str(uuid.uuid4()),
        "type": type_,
        "severity": severity,
        "detail": detail,
        "suggested_action": suggested_action,
        "detected_at": _format_z(detected_at),
    }


def _today_start_utc(now_utc: datetime) -> datetime:
    return now_utc.replace(hour=0, minute=0, second=0, microsecond=0)


def _open_hour_bounds_utc(layout: dict, now_utc: datetime) -> tuple[datetime, datetime]:
    tz = ZoneInfo(layout.get("timezone", "UTC"))
    oh = layout.get("open_hours") or {"start": "09:00", "end": "21:00"}
    sh, sm = (int(x) for x in oh["start"].split(":"))
    eh, em = (int(x) for x in oh["end"].split(":"))
    now_local = now_utc.astimezone(tz)
    start_local = now_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_local = now_local.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _in_open_hours(layout: dict, now_utc: datetime) -> bool:
    start_utc, end_utc = _open_hour_bounds_utc(layout, now_utc)
    return start_utc <= now_utc <= end_utc


# ---------- detectors ----------

async def _detect_queue_spike(
    conn, store_id: str, layout: dict, now_utc: datetime
) -> list[dict]:
    today_start = _today_start_utc(now_utc).isoformat()
    stmt = (
        select(events.c.queue_depth, events.c.timestamp)
        .where(
            and_(
                events.c.store_id == store_id,
                events.c.event_type == "BILLING_QUEUE_JOIN",
                events.c.timestamp >= today_start,
            )
        )
        .order_by(events.c.timestamp.desc())
        .limit(1)
    )
    row = (await conn.execute(stmt)).first()
    if row is None or row[0] is None:
        return []
    qd = int(row[0])
    if qd <= QUEUE_WARN_THRESHOLD:
        return []
    severity = "CRITICAL" if qd >= QUEUE_CRITICAL_THRESHOLD else "WARN"
    event_ts = _parse_iso(row[1])
    return [
        _mk(
            "BILLING_QUEUE_SPIKE",
            severity,
            f"Current queue depth: {qd}",
            "Activate additional billing counter immediately",
            event_ts,
        )
    ]


_CONV_TODAY_SQL = text(
    """
    SELECT
      COUNT(DISTINCT visitor_id) AS today_unique,
      COUNT(DISTINCT CASE WHEN converted = 1 THEN visitor_id END) AS today_converted
    FROM store_sessions
    WHERE store_id = :store_id
      AND entry_ts >= :today_start
    """
)

_CONV_HIST_SQL = text(
    """
    SELECT
      DATE(entry_ts) AS day,
      COUNT(DISTINCT visitor_id) AS uniq,
      COUNT(DISTINCT CASE WHEN converted = 1 THEN visitor_id END) AS conv
    FROM store_sessions
    WHERE store_id = :store_id
      AND entry_ts >= :seven_days_ago
      AND entry_ts <  :today_start
    GROUP BY DATE(entry_ts)
    """
)


async def _detect_conversion_drop(
    conn, store_id: str, layout: dict, now_utc: datetime
) -> list[dict]:
    today_start = _today_start_utc(now_utc)
    seven_days_ago = today_start - timedelta(days=7)

    today_row = (
        await conn.execute(
            _CONV_TODAY_SQL.bindparams(
                store_id=store_id,
                today_start=today_start.isoformat(),
            )
        )
    ).first()
    today_unique = int(today_row.today_unique or 0) if today_row else 0
    today_converted = int(today_row.today_converted or 0) if today_row else 0

    if today_unique < CONVERSION_MIN_TODAY_SESSIONS:
        return []

    hist_rows = (
        await conn.execute(
            _CONV_HIST_SQL.bindparams(
                store_id=store_id,
                seven_days_ago=seven_days_ago.isoformat(),
                today_start=today_start.isoformat(),
            )
        )
    ).all()

    if len(hist_rows) < CONVERSION_MIN_HIST_DAYS:
        return []

    daily_rates = [
        (r.conv or 0) / r.uniq for r in hist_rows if r.uniq and r.uniq > 0
    ]
    if not daily_rates:
        return []
    avg_rate = sum(daily_rates) / len(daily_rates)
    today_rate = today_converted / today_unique

    if today_rate >= avg_rate * CONVERSION_DROP_RATIO:
        return []

    return [
        _mk(
            "CONVERSION_DROP",
            "WARN",
            f"Today: {today_rate:.1%}, 7-day avg: {avg_rate:.1%}",
            "Review floor staff deployment and promotional materials",
            now_utc,
        )
    ]


async def _detect_dead_zones(
    conn, store_id: str, layout: dict, now_utc: datetime
) -> list[dict]:
    if not _in_open_hours(layout, now_utc):
        return []

    today_start = _today_start_utc(now_utc).isoformat()
    visitor_stmt = select(func.count(distinct(events.c.visitor_id))).where(
        and_(
            events.c.store_id == store_id,
            events.c.is_staff == 0,
            events.c.timestamp >= today_start,
        )
    )
    visitor_count = int((await conn.execute(visitor_stmt)).scalar() or 0)
    if visitor_count == 0:
        return []

    last_stmt = (
        select(events.c.zone_id, func.max(events.c.timestamp).label("last_ts"))
        .where(
            and_(
                events.c.store_id == store_id,
                events.c.event_type == "ZONE_ENTER",
            )
        )
        .group_by(events.c.zone_id)
    )
    rows = (await conn.execute(last_stmt)).all()
    last_by_zone: dict[str, datetime] = {}
    for r in rows:
        if r[0] and r[1]:
            last_by_zone[r[0]] = _parse_iso(r[1])

    cutoff = now_utc - timedelta(minutes=DEAD_ZONE_WINDOW_MIN)
    out: list[dict] = []
    for z in layout.get("zones", []):
        # Threshold cameras emit ENTRY/EXIT, not ZONE_ENTER — skip them.
        if z.get("type") == "threshold":
            continue
        zid = z["zone_id"]
        label = z.get("label", zid)
        last = last_by_zone.get(zid)
        if last is None or last < cutoff:
            out.append(
                _mk(
                    "DEAD_ZONE",
                    "INFO",
                    f"No activity in {label} for {DEAD_ZONE_WINDOW_MIN}+ minutes",
                    f"Check camera feed for {zid} — may be obscured or offline",
                    now_utc,
                )
            )
    return out


async def _detect_stale_cameras(
    conn, store_id: str, layout: dict, now_utc: datetime
) -> list[dict]:
    if not _in_open_hours(layout, now_utc):
        return []

    last_stmt = (
        select(events.c.camera_id, func.max(events.c.timestamp).label("last_ts"))
        .where(events.c.store_id == store_id)
        .group_by(events.c.camera_id)
    )
    rows = (await conn.execute(last_stmt)).all()
    last_by_cam: dict[str, datetime] = {}
    for r in rows:
        if r[0] and r[1]:
            last_by_cam[r[0]] = _parse_iso(r[1])

    open_start, _ = _open_hour_bounds_utc(layout, now_utc)
    out: list[dict] = []
    for cam in layout.get("cameras", []):
        cam_id = cam["camera_id"]
        last = last_by_cam.get(cam_id)
        # Cold-start grace: if the camera has never reported, measure lag from
        # today's store-open time, not from epoch. Avoids spurious alerts at
        # first minutes after opening.
        effective_last = last if last is not None else open_start
        lag_minutes = (now_utc - effective_last).total_seconds() / 60.0
        if lag_minutes > STALE_CAMERA_WINDOW_MIN:
            out.append(
                _mk(
                    "STALE_CAMERA",
                    "CRITICAL",
                    f"No events from {cam_id} for {lag_minutes:.0f} minutes",
                    f"Check camera connectivity for {cam_id}",
                    now_utc,
                )
            )
    return out


# ---------- endpoint ----------

DetectorFn = Callable[[Any, str, dict, datetime], Awaitable[list[dict]]]

_DETECTORS: list[tuple[str, DetectorFn]] = [
    ("BILLING_QUEUE_SPIKE", _detect_queue_spike),
    ("CONVERSION_DROP", _detect_conversion_drop),
    ("DEAD_ZONE", _detect_dead_zones),
    ("STALE_CAMERA", _detect_stale_cameras),
]


@router.get(
    "/stores/{store_id}/anomalies",
    response_model=AnomaliesResponse,
    summary="Active store anomalies (INFO / WARN / CRITICAL)",
    description=(
        "Runs 4 detectors (queue spike, conversion drop, dead zone, stale "
        "camera). Each detector is isolated: a failure in one is logged and "
        "does not prevent the others from running. Returns an empty list when "
        "no anomalies are active — never null."
    ),
)
async def get_store_anomalies(store_id: str, request: Request):
    layout = _layout_for(request, store_id)
    if layout is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Store not found", "store_id": store_id},
        )

    now_utc = datetime.now(timezone.utc)
    active: list[dict] = []

    async with get_conn() as conn:
        for name, detector in _DETECTORS:
            try:
                active.extend(await detector(conn, store_id, layout, now_utc))
            except Exception as exc:
                logger.exception(
                    "anomaly_detector.error",
                    detector=name,
                    store_id=store_id,
                    error=str(exc),
                )

    return {
        "store_id": store_id,
        "checked_at": _format_z(now_utc),
        "active_anomalies": active,
    }
