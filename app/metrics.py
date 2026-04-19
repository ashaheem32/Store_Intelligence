"""GET /stores/{store_id}/metrics — live, today-window store metrics."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import and_, distinct, func, select

from app.db import events, get_conn, store_sessions

router = APIRouter(tags=["metrics"])

DATA_CONFIDENCE_SESSION_THRESHOLD = 20


class MetricsResponse(BaseModel):
    store_id: str
    window: str
    as_of: str
    unique_visitors: int
    converted_visitors: int
    conversion_rate: Optional[float]
    avg_dwell_seconds: dict[str, float]
    current_queue_depth: int
    abandonment_rate: Optional[float]
    data_confidence: Literal["HIGH", "LOW"]


def _today_start_utc() -> datetime:
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _format_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _layout_for(request: Request, store_id: str) -> Optional[dict]:
    layout = getattr(request.app.state, "store_layout", None)
    if layout and layout.get("store_id") == store_id:
        return layout
    return None


async def _compute_metrics(conn, store_id: str, zones: list[str]) -> dict:
    start_iso = _today_start_utc().isoformat()

    # unique visitors today (non-staff)
    stmt = select(func.count(distinct(events.c.visitor_id))).where(
        and_(
            events.c.store_id == store_id,
            events.c.is_staff == 0,
            events.c.timestamp >= start_iso,
        )
    )
    unique_visitors = int((await conn.execute(stmt)).scalar() or 0)

    # converted visitors (from store_sessions today)
    stmt = select(func.count(distinct(store_sessions.c.visitor_id))).where(
        and_(
            store_sessions.c.store_id == store_id,
            store_sessions.c.converted == 1,
            store_sessions.c.entry_ts >= start_iso,
        )
    )
    converted_visitors = int((await conn.execute(stmt)).scalar() or 0)

    conversion_rate = (
        round(converted_visitors / unique_visitors, 3)
        if unique_visitors > 0
        else None
    )

    # avg dwell per zone: max dwell per (visitor, zone), then avg across visitors
    inner = (
        select(
            events.c.visitor_id.label("vid"),
            events.c.zone_id.label("zid"),
            func.max(events.c.dwell_ms).label("max_dwell"),
        )
        .where(
            and_(
                events.c.store_id == store_id,
                events.c.is_staff == 0,
                events.c.timestamp >= start_iso,
                events.c.event_type.in_(["ZONE_DWELL", "ZONE_EXIT"]),
                events.c.zone_id.is_not(None),
            )
        )
        .group_by(events.c.visitor_id, events.c.zone_id)
        .subquery()
    )
    stmt = select(
        inner.c.zid,
        func.avg(inner.c.max_dwell).label("avg_dwell_ms"),
    ).group_by(inner.c.zid)
    rows = (await conn.execute(stmt)).all()
    dwell_by_zone = {
        r.zid: round(float(r.avg_dwell_ms) / 1000.0, 1)
        for r in rows
        if r.zid is not None
    }
    avg_dwell_seconds = {z: dwell_by_zone.get(z, 0.0) for z in zones}

    # current queue depth: latest BILLING_QUEUE_JOIN today
    stmt = (
        select(events.c.queue_depth)
        .where(
            and_(
                events.c.store_id == store_id,
                events.c.event_type == "BILLING_QUEUE_JOIN",
                events.c.timestamp >= start_iso,
            )
        )
        .order_by(events.c.timestamp.desc())
        .limit(1)
    )
    row = (await conn.execute(stmt)).first()
    current_queue_depth = int(row[0]) if row and row[0] is not None else 0

    # abandonment: count JOIN vs ABANDON today (non-staff)
    stmt = (
        select(events.c.event_type, func.count())
        .where(
            and_(
                events.c.store_id == store_id,
                events.c.is_staff == 0,
                events.c.timestamp >= start_iso,
                events.c.event_type.in_(["BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON"]),
            )
        )
        .group_by(events.c.event_type)
    )
    counts = {r[0]: int(r[1]) for r in (await conn.execute(stmt)).all()}
    joins = counts.get("BILLING_QUEUE_JOIN", 0)
    abandons = counts.get("BILLING_QUEUE_ABANDON", 0)
    abandonment_rate = round(abandons / joins, 3) if joins > 0 else None

    # data confidence: by number of sessions today
    stmt = select(func.count(store_sessions.c.session_id)).where(
        and_(
            store_sessions.c.store_id == store_id,
            store_sessions.c.entry_ts >= start_iso,
        )
    )
    sessions_today = int((await conn.execute(stmt)).scalar() or 0)
    data_confidence = (
        "HIGH" if sessions_today >= DATA_CONFIDENCE_SESSION_THRESHOLD else "LOW"
    )

    return {
        "store_id": store_id,
        "window": "today",
        "as_of": _format_z(datetime.now(timezone.utc)),
        "unique_visitors": unique_visitors,
        "converted_visitors": converted_visitors,
        "conversion_rate": conversion_rate,
        "avg_dwell_seconds": avg_dwell_seconds,
        "current_queue_depth": current_queue_depth,
        "abandonment_rate": abandonment_rate,
        "data_confidence": data_confidence,
    }


@router.get(
    "/stores/{store_id}/metrics",
    response_model=MetricsResponse,
    summary="Live store metrics (today, non-staff only)",
    description=(
        "Window: midnight UTC → now. Excludes is_staff=1 events. "
        "All zones from store_layout.json are included in avg_dwell_seconds "
        "(zero if no dwell events). "
        "conversion_rate is null when unique_visitors=0; abandonment_rate is "
        "null when no BILLING_QUEUE_JOIN events exist today. "
        "data_confidence is LOW when fewer than "
        f"{DATA_CONFIDENCE_SESSION_THRESHOLD} sessions today."
    ),
)
async def get_store_metrics(store_id: str, request: Request):
    layout = _layout_for(request, store_id)
    if layout is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Store not found", "store_id": store_id},
        )

    zones = [z["zone_id"] for z in layout.get("zones", [])]

    async with get_conn() as conn:
        metrics = await _compute_metrics(conn, store_id, zones)

    return metrics
