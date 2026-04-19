"""GET /stores/{store_id}/heatmap — normalised zone visit frequency + dwell."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_conn

router = APIRouter(tags=["heatmap"])

DATA_CONFIDENCE_UNIQUE_VISITOR_THRESHOLD = 20


class ZoneHeatmap(BaseModel):
    zone_id: str
    label: str
    visit_count: int
    avg_dwell_seconds: float
    normalised_score: int
    data_confidence: Literal["HIGH", "LOW"]


class HeatmapResponse(BaseModel):
    store_id: str
    window: str
    zones: list[ZoneHeatmap]


# One-roundtrip query: visit_count + unique_visitors per zone, plus avg
# dwell (max dwell per visitor, averaged across visitors). Parameters bound
# via bindparams — no f-string SQL. Zones missing from the result are filled
# in by the Python layer so the response always contains every layout zone.
HEATMAP_SQL = """
WITH
-- visit_count = total ZONE_ENTER events today (is_staff=0) per zone;
-- unique_visitors feeds data_confidence.
visits AS (
  SELECT
    zone_id,
    SUM(CASE WHEN event_type = 'ZONE_ENTER' THEN 1 ELSE 0 END) AS visit_count,
    COUNT(DISTINCT visitor_id) AS unique_visitors
  FROM events
  WHERE store_id = :store_id
    AND is_staff = 0
    AND timestamp >= :today_start
    AND zone_id IS NOT NULL
  GROUP BY zone_id
),
-- per-(visitor, zone) MAX(dwell_ms), then AVG across visitors.
dwells AS (
  SELECT zone_id, AVG(max_dwell) AS avg_dwell_ms
  FROM (
    SELECT visitor_id, zone_id, MAX(dwell_ms) AS max_dwell
    FROM events
    WHERE store_id = :store_id
      AND is_staff = 0
      AND timestamp >= :today_start
      AND event_type IN ('ZONE_DWELL', 'ZONE_EXIT')
      AND zone_id IS NOT NULL
    GROUP BY visitor_id, zone_id
  )
  GROUP BY zone_id
)
SELECT
  v.zone_id                           AS zone_id,
  COALESCE(v.visit_count, 0)          AS visit_count,
  COALESCE(v.unique_visitors, 0)      AS unique_visitors,
  COALESCE(d.avg_dwell_ms, 0.0)       AS avg_dwell_ms
FROM visits v
LEFT JOIN dwells d ON d.zone_id = v.zone_id
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


@router.get(
    "/stores/{store_id}/heatmap",
    response_model=HeatmapResponse,
    summary="Zone visit frequency + dwell, normalised 0–100",
    description=(
        "Returns one entry per zone from store_layout.json (every zone always "
        "appears, even with zero activity). Scoring: max-visit-count zone = 100; "
        "others scale linearly. data_confidence=LOW when fewer than "
        f"{DATA_CONFIDENCE_UNIQUE_VISITOR_THRESHOLD} unique non-staff visitors "
        "contributed. Today window (midnight UTC → now)."
    ),
)
async def get_store_heatmap(store_id: str, request: Request):
    layout = _layout_for(request, store_id)
    if layout is None:
        return JSONResponse(
            status_code=404,
            content={"detail": "Store not found", "store_id": store_id},
        )

    layout_zones = layout.get("zones", [])
    today_start_iso = _today_start_utc().isoformat()

    stmt = text(HEATMAP_SQL).bindparams(
        store_id=store_id,
        today_start=today_start_iso,
    )

    async with get_conn() as conn:
        rows = (await conn.execute(stmt)).mappings().all()

    by_zone = {r["zone_id"]: dict(r) for r in rows if r.get("zone_id")}

    merged = []
    for z in layout_zones:
        zid = z["zone_id"]
        hit = by_zone.get(zid)
        if hit:
            visit_count = int(hit.get("visit_count") or 0)
            unique_visitors = int(hit.get("unique_visitors") or 0)
            avg_dwell_ms = float(hit.get("avg_dwell_ms") or 0.0)
        else:
            visit_count = 0
            unique_visitors = 0
            avg_dwell_ms = 0.0
        merged.append({
            "zone_id": zid,
            "label": z.get("label", zid),
            "visit_count": visit_count,
            "unique_visitors": unique_visitors,
            "avg_dwell_seconds": round(avg_dwell_ms / 1000.0, 1),
        })

    max_visits = max((z["visit_count"] for z in merged), default=0)
    zones_out = []
    for z in merged:
        if max_visits > 0:
            score = round(z["visit_count"] / max_visits * 100)
        else:
            score = 0
        confidence = (
            "HIGH"
            if z["unique_visitors"] >= DATA_CONFIDENCE_UNIQUE_VISITOR_THRESHOLD
            else "LOW"
        )
        zones_out.append({
            "zone_id": z["zone_id"],
            "label": z["label"],
            "visit_count": z["visit_count"],
            "avg_dwell_seconds": z["avg_dwell_seconds"],
            "normalised_score": int(score),
            "data_confidence": confidence,
        })

    return {
        "store_id": store_id,
        "window": "today",
        "zones": zones_out,
    }
