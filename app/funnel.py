"""GET /stores/{store_id}/funnel — session-unit conversion funnel."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_conn

router = APIRouter(tags=["funnel"])

STAGE_NAMES = ("ENTRY", "ZONE_VISIT", "BILLING_QUEUE", "PURCHASE")


class FunnelStage(BaseModel):
    stage: str
    count: int
    pct_of_entry: Optional[float]
    pct_from_prev: Optional[float]


class DropOffAnalysis(BaseModel):
    biggest_drop_stage: Optional[str]
    drop_pct: Optional[float]


class FunnelResponse(BaseModel):
    store_id: str
    window: str
    stages: list[FunnelStage]
    drop_off_analysis: DropOffAnalysis


# Single-roundtrip CTE query. Parameters bound via bindparams — no f-string SQL.
# Dedup semantics baked into each CTE; see inline comments.
FUNNEL_SQL = """
WITH
-- Eligible visitor set: DISTINCT visitor_ids with a session today.
-- Rule 1 (re-entry dedup): DISTINCT visitor_id collapses ENTRY + REENTRY
--   rows in store_sessions to a single entry per visitor.
-- Rule 3 (today only): s.entry_ts >= today_start param.
-- Staff exclusion: visitor must have at least one event with is_staff=0.
--   (our detect pipeline sets is_staff consistently per visitor, so
--    "any non-staff event" is equivalent to "non-staff visitor".)
eligible AS (
  SELECT DISTINCT s.visitor_id
  FROM store_sessions s
  WHERE s.store_id = :store_id
    AND s.entry_ts >= :today_start
    AND EXISTS (
      SELECT 1
      FROM events e
      WHERE e.visitor_id = s.visitor_id
        AND e.store_id = s.store_id
        AND e.is_staff = 0
    )
),

-- Stage 1 — ENTRY: every eligible visitor counts once.
stage_entry AS (
  SELECT visitor_id FROM eligible
),

-- Stage 2 — ZONE_VISIT: eligible visitors with at least one ZONE_ENTER
-- on a shopping floor today. Rule 2 dedup: DISTINCT visitor_id means a
-- visitor who entered 15 zones counts as 1.
stage_zone AS (
  SELECT DISTINCT se.visitor_id
  FROM stage_entry se
  JOIN events e ON e.visitor_id = se.visitor_id
  WHERE e.store_id = :store_id
    AND e.event_type = 'ZONE_ENTER'
    AND e.zone_id IN ('MAIN_FLOOR', 'FLOOR_SOUTH')
    AND e.is_staff = 0
    AND e.timestamp >= :today_start
),

-- Stage 3 — BILLING_QUEUE: subset of stage_zone who also joined the queue.
-- JOIN against stage_zone (not eligible) enforces the funnel subset property.
stage_billing AS (
  SELECT DISTINCT sz.visitor_id
  FROM stage_zone sz
  JOIN events e ON e.visitor_id = sz.visitor_id
  WHERE e.store_id = :store_id
    AND e.event_type = 'BILLING_QUEUE_JOIN'
    AND e.is_staff = 0
    AND e.timestamp >= :today_start
),

-- Stage 4 — PURCHASE: subset of stage_billing whose session is converted.
-- Conversion is authoritative on store_sessions.converted (set by
-- POS-correlation in /events/ingest).
stage_purchase AS (
  SELECT DISTINCT sb.visitor_id
  FROM stage_billing sb
  JOIN store_sessions ss ON ss.visitor_id = sb.visitor_id
  WHERE ss.store_id = :store_id
    AND ss.converted = 1
    AND ss.entry_ts >= :today_start
)

SELECT
  (SELECT COUNT(*) FROM stage_entry)    AS c_entry,
  (SELECT COUNT(*) FROM stage_zone)     AS c_zone,
  (SELECT COUNT(*) FROM stage_billing)  AS c_billing,
  (SELECT COUNT(*) FROM stage_purchase) AS c_purchase
"""


def _today_start_utc() -> datetime:
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _layout_for(request: Request, store_id: str) -> Optional[dict]:
    layout = getattr(request.app.state, "store_layout", None)
    if layout and layout.get("store_id") == store_id:
        return layout
    return None


def _build_stages(counts: list[int]) -> list[dict]:
    c_entry = counts[0]
    if c_entry == 0:
        return [
            {
                "stage": name,
                "count": 0,
                "pct_of_entry": None,
                "pct_from_prev": None,
            }
            for name in STAGE_NAMES
        ]

    stages: list[dict] = []
    prev_count: Optional[int] = None
    for name, cnt in zip(STAGE_NAMES, counts):
        pct_of_entry = round(cnt / c_entry * 100, 1)
        if prev_count is None:
            pct_from_prev: Optional[float] = None
        elif prev_count == 0:
            pct_from_prev = None
        else:
            pct_from_prev = round(cnt / prev_count * 100, 1)
        stages.append({
            "stage": name,
            "count": cnt,
            "pct_of_entry": pct_of_entry,
            "pct_from_prev": pct_from_prev,
        })
        prev_count = cnt
    return stages


def _drop_off(counts: list[int]) -> dict:
    if counts[0] == 0:
        return {"biggest_drop_stage": None, "drop_pct": None}

    biggest_name: Optional[str] = None
    biggest_pct: Optional[float] = None
    for i in range(1, len(counts)):
        prev, curr = counts[i - 1], counts[i]
        if prev == 0:
            continue
        drop_pct = round((prev - curr) / prev * 100, 1)
        key = f"{STAGE_NAMES[i - 1]}_TO_{STAGE_NAMES[i]}"
        if biggest_pct is None or drop_pct > biggest_pct:
            biggest_name = key
            biggest_pct = drop_pct
    return {"biggest_drop_stage": biggest_name, "drop_pct": biggest_pct}


@router.get(
    "/stores/{store_id}/funnel",
    response_model=FunnelResponse,
    summary="Session-unit conversion funnel",
    description=(
        "Funnel: ENTRY → ZONE_VISIT (MAIN_FLOOR/FLOOR_SOUTH) → BILLING_QUEUE → "
        "PURCHASE. Session is the unit — re-entries do not double-count; "
        "multiple zone visits collapse to one. Today window (midnight UTC → now)."
    ),
)
async def get_store_funnel(store_id: str, request: Request):
    if _layout_for(request, store_id) is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Store not found", "store_id": store_id},
        )

    today_start_iso = _today_start_utc().isoformat()
    params = {"store_id": store_id, "today_start": today_start_iso}

    async with get_conn() as conn:
        row = (await conn.execute(text(FUNNEL_SQL), params)).first()

    counts = [
        int(row.c_entry or 0),
        int(row.c_zone or 0),
        int(row.c_billing or 0),
        int(row.c_purchase or 0),
    ] if row else [0, 0, 0, 0]

    return {
        "store_id": store_id,
        "window": "today",
        "stages": _build_stages(counts),
        "drop_off_analysis": _drop_off(counts),
    }
