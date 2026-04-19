# PROMPT: "Write pytest-asyncio + httpx.AsyncClient tests for the Store Intelligence
# API: root, /health, /stores/{id}/metrics (zero / staff-only / conversion-rate),
# /events/ingest (idempotency + partial success), /stores/{id}/funnel (session dedup),
# /stores/{id}/heatmap (normalised score), and unknown-store 404. DB fixture provides
# a fresh temp-file SQLite per test."
# CHANGES MADE:
#   - Dropped AI's mock-based conversion_rate setup. Instead, insert events via
#     the real /events/ingest endpoint and then directly UPDATE store_sessions
#     via the DB — exercises the real ingest path AND lets us control converted=1
#     deterministically without depending on POS window arithmetic.
#   - Used `_ev()` helper that produces today-dated timestamps, so the "today"
#     filter in the metrics/funnel/heatmap queries includes the seeded events.
#   - For partial-success, made invalid events fail the confidence validator
#     (clear, specific Pydantic rejection) rather than random garbage.
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.db import get_conn, store_sessions


def _iso_today(seconds_offset: int = 0) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return (now.replace(hour=10, minute=0, second=0) + _td(seconds=seconds_offset)).isoformat()


def _td(seconds: int):
    from datetime import timedelta
    return timedelta(seconds=seconds)


def _ev(
    event_type: str,
    visitor_id: str,
    camera_id: str = "CAM_1",
    zone_id: str | None = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.9,
    queue_depth: int | None = None,
    sku_zone: str | None = None,
    seconds_offset: int = 0,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_001",
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": _iso_today(seconds_offset),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": 1,
        },
    }


async def test_root_endpoint(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "Store Intelligence API" in r.json()["message"]


async def test_health_endpoint(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    assert "database" in body


async def test_metrics_zero_visitors(client):
    r = await client.get("/stores/STORE_001/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] is None


async def test_metrics_excludes_staff(client, seed_events):
    events = [_ev("ENTRY", f"VIS_s{i:02d}", is_staff=True) for i in range(5)]
    r = await seed_events(client, events)
    assert r.status_code == 200

    m = (await client.get("/stores/STORE_001/metrics")).json()
    assert m["unique_visitors"] == 0


async def test_metrics_conversion_rate(client, seed_events):
    # 10 non-staff ENTRY events = 10 sessions.
    events = [_ev("ENTRY", f"VIS_c{i:02d}", seconds_offset=i * 10) for i in range(10)]
    r = await seed_events(client, events)
    assert r.status_code == 200

    # Mark 3 sessions converted directly (avoids POS-window coupling).
    async with get_conn() as conn:
        rows = (await conn.execute(select(store_sessions))).mappings().all()
        for row in list(rows)[:3]:
            stmt = (
                update(store_sessions)
                .where(store_sessions.c.session_id == row["session_id"])
                .values(converted=1)
            )
            await conn.execute(stmt)

    m = (await client.get("/stores/STORE_001/metrics")).json()
    assert m["unique_visitors"] == 10
    assert m["converted_visitors"] == 3
    assert m["conversion_rate"] == 0.3


async def test_ingest_idempotent(client, seed_events):
    events = [_ev("ENTRY", f"VIS_i{i:02d}") for i in range(5)]
    r1 = (await seed_events(client, events)).json()
    assert r1["accepted"] == 5
    assert r1["duplicate"] == 0

    r2 = (await seed_events(client, events)).json()
    assert r2["accepted"] == 0
    assert r2["duplicate"] == 5


async def test_ingest_partial_success(client, seed_events):
    good = [_ev("ENTRY", f"VIS_g{i:02d}") for i in range(7)]
    bad = []
    for i in range(3):
        e = _ev("ENTRY", f"VIS_b{i:02d}")
        e["confidence"] = 1.5  # fails Pydantic validator
        bad.append(e)

    r = (await seed_events(client, good + bad)).json()
    assert r["accepted"] == 7
    assert r["invalid"] == 3
    assert len(r["errors"]) == 3


async def test_funnel_session_deduplication(client, seed_events):
    # One visitor with ENTRY + 20 ZONE_ENTER events on MAIN_FLOOR.
    vid = "VIS_dupe01"
    events = [_ev("ENTRY", vid, seconds_offset=0)]
    for i in range(20):
        events.append(
            _ev(
                "ZONE_ENTER",
                vid,
                camera_id="CAM_2",
                zone_id="MAIN_FLOOR",
                sku_zone="MAIN_FLOOR",
                seconds_offset=i + 1,
            )
        )
    r = await seed_events(client, events)
    assert r.status_code == 200

    f = (await client.get("/stores/STORE_001/funnel")).json()
    stages = {s["stage"]: s["count"] for s in f["stages"]}
    assert stages["ENTRY"] == 1
    assert stages["ZONE_VISIT"] == 1  # not 20


async def test_heatmap_normalisation(client, seed_events):
    # 100 ZONE_ENTERs on MAIN_FLOOR, 50 on FLOOR_SOUTH (distinct visitor_ids).
    events: list[dict] = []
    for i in range(100):
        events.append(
            _ev(
                "ZONE_ENTER",
                f"VIS_ma{i:03d}",
                camera_id="CAM_2",
                zone_id="MAIN_FLOOR",
                sku_zone="MAIN_FLOOR",
                seconds_offset=i,
            )
        )
    for i in range(50):
        events.append(
            _ev(
                "ZONE_ENTER",
                f"VIS_so{i:03d}",
                camera_id="CAM_4",
                zone_id="FLOOR_SOUTH",
                sku_zone="FLOOR_SOUTH",
                seconds_offset=i,
            )
        )

    # Batch size <= 500 per endpoint limit.
    r = await seed_events(client, events)
    assert r.status_code == 200

    h = (await client.get("/stores/STORE_001/heatmap")).json()
    by_zone = {z["zone_id"]: z for z in h["zones"]}
    assert by_zone["MAIN_FLOOR"]["normalised_score"] == 100
    assert by_zone["FLOOR_SOUTH"]["normalised_score"] == 50


async def test_unknown_store_returns_404(client):
    r = await client.get("/stores/STORE_999/metrics")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "Store not found"
    assert body["store_id"] == "STORE_999"
