"""GET /health — database + per-store feed freshness.

The oncall-engineer page. Optimised for speed: one connection, one ping, one
MAX(timestamp) per store. Never crashes — every DB call is try/except wrapped.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text

from app.db import events, get_conn

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["health"])

DEFAULT_STALE_FEED_MINUTES = 10


class StoreHealth(BaseModel):
    last_event_at: Optional[str]
    lag_minutes: Optional[float]
    feed_status: Literal["LIVE", "STALE", "CLOSED", "NO_DATA"]


class HealthResponse(BaseModel):
    status: Literal["OK", "DEGRADED"]
    checked_at: str
    database: Literal["connected"]
    stores: dict[str, StoreHealth]


def _format_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    normalised = s[:-1] + "+00:00" if s.endswith("Z") else s
    return datetime.fromisoformat(normalised)


def _in_open_hours(layout: dict, now_utc: datetime) -> bool:
    tz = ZoneInfo(layout.get("timezone", "UTC"))
    oh = layout.get("open_hours") or {"start": "09:00", "end": "21:00"}
    sh, sm = (int(x) for x in oh["start"].split(":"))
    eh, em = (int(x) for x in oh["end"].split(":"))
    now_local = now_utc.astimezone(tz)
    start = sh * 60 + sm
    end = eh * 60 + em
    current = now_local.hour * 60 + now_local.minute
    return start <= current <= end


def _layouts_for_request(request: Request) -> list[dict]:
    layout = getattr(request.app.state, "store_layout", None)
    if isinstance(layout, dict):
        return [layout]
    if isinstance(layout, list):
        return [x for x in layout if isinstance(x, dict)]
    return []


def _stale_minutes() -> float:
    raw = os.environ.get("STALE_FEED_MINUTES")
    if raw is None:
        return float(DEFAULT_STALE_FEED_MINUTES)
    try:
        return float(raw)
    except ValueError:
        return float(DEFAULT_STALE_FEED_MINUTES)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service + per-store feed health",
    description=(
        "Returns 200 when the database is reachable (even if some feeds are "
        "STALE). Returns 503 with {error, trace_id} when the database is "
        "unavailable. Overall status is DEGRADED when any feed is STALE "
        "during open hours; OK otherwise."
    ),
)
async def get_health(request: Request):
    now_utc = datetime.now(timezone.utc)
    trace_id = request.headers.get("x-trace-id") or str(uuid.uuid4())
    stale_threshold_min = _stale_minutes()
    layouts = _layouts_for_request(request)

    stores: dict[str, dict] = {}
    any_stale = False

    try:
        async with get_conn() as conn:
            # DB connectivity probe. Any exception here → 503.
            await conn.execute(text("SELECT 1"))

            for layout in layouts:
                store_id = layout.get("store_id")
                if not store_id:
                    continue
                last_ts: Optional[datetime] = None
                try:
                    stmt = select(func.max(events.c.timestamp)).where(
                        events.c.store_id == store_id
                    )
                    val = (await conn.execute(stmt)).scalar()
                    if val is not None:
                        last_ts = _parse_iso(val)
                except Exception as exc:
                    # Per-store query failed — isolated, other stores still checked.
                    logger.exception(
                        "health.store_query_failed",
                        store_id=store_id,
                        trace_id=trace_id,
                        error=str(exc),
                    )

                open_now = False
                try:
                    open_now = _in_open_hours(layout, now_utc)
                except Exception as exc:
                    logger.exception(
                        "health.open_hours_failed",
                        store_id=store_id,
                        trace_id=trace_id,
                        error=str(exc),
                    )

                if last_ts is None:
                    feed_status = "NO_DATA"
                    lag_minutes: Optional[float] = None
                else:
                    lag_minutes = round(
                        (now_utc - last_ts).total_seconds() / 60.0, 1
                    )
                    if not open_now:
                        feed_status = "CLOSED"
                    elif lag_minutes > stale_threshold_min:
                        feed_status = "STALE"
                        any_stale = True
                    else:
                        feed_status = "LIVE"

                stores[store_id] = {
                    "last_event_at": _format_z(last_ts) if last_ts else None,
                    "lag_minutes": lag_minutes,
                    "feed_status": feed_status,
                }
    except Exception as exc:
        logger.error(
            "health.db_unavailable",
            trace_id=trace_id,
            error=str(exc),
        )
        return JSONResponse(
            status_code=503,
            content={"error": "Database unavailable", "trace_id": trace_id},
        )

    overall = "DEGRADED" if any_stale else "OK"
    return {
        "status": overall,
        "checked_at": _format_z(now_utc),
        "database": "connected",
        "stores": stores,
    }
