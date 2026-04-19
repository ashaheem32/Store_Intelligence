# PROMPT: "Write pytest-asyncio tests for GET /stores/{id}/anomalies covering:
# empty store → []; BILLING_QUEUE_SPIKE WARN at qd=7 and CRITICAL at qd=11;
# no spike at qd=4; DEAD_ZONE fires during open hours when a zone has no
# ZONE_ENTER events; DEAD_ZONE suppressed outside open hours."
# CHANGES MADE:
#   - Don't freeze the clock. For open-hours control, mutate
#     app.state.store_layout.open_hours in the test — the endpoint reads it
#     every request, so this is deterministic and needs no extra deps.
#   - Compute "closed now" dynamically from IST wall-clock so CI doesn't flake
#     depending on what time the suite runs.
#   - For the "empty store" case, force the store CLOSED so the (open-hours-
#     gated) STALE_CAMERA/DEAD_ZONE detectors don't fire false positives —
#     an unseeded DB with an open layout would otherwise produce stale-camera
#     anomalies and make the "no anomalies" assertion impossible.
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _iso_now(offset_seconds: int = 0) -> str:
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return (now + timedelta(seconds=offset_seconds)).isoformat()


def _ev(
    event_type: str,
    visitor_id: str,
    camera_id: str = "CAM_1",
    zone_id: str | None = None,
    is_staff: bool = False,
    confidence: float = 0.9,
    queue_depth: int | None = None,
    sku_zone: str | None = None,
    offset_seconds: int = 0,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_001",
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": _iso_now(offset_seconds),
        "zone_id": zone_id,
        "dwell_ms": 0,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": 1,
        },
    }


def _force_closed_now(layout: dict) -> None:
    """Pin open_hours to a 1-hour window +3h in IST from now — guaranteed to exclude now."""
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    future = (now_ist.hour + 3) % 24
    layout["open_hours"] = {"start": f"{future:02d}:00", "end": f"{future:02d}:59"}


def _force_open_now(layout: dict) -> None:
    layout["open_hours"] = {"start": "00:00", "end": "23:59"}


async def test_no_anomalies_empty_store(client, test_app):
    _force_closed_now(test_app.state.store_layout)
    r = await client.get("/stores/STORE_001/anomalies")
    assert r.status_code == 200
    assert r.json()["active_anomalies"] == []


async def test_billing_queue_spike_warn(client, test_app, seed_events):
    _force_closed_now(test_app.state.store_layout)
    ev = _ev(
        "BILLING_QUEUE_JOIN",
        "VIS_q01",
        camera_id="CAM_3",
        zone_id="BILLING",
        queue_depth=7,
        sku_zone="BILLING",
    )
    await seed_events(client, [ev])

    body = (await client.get("/stores/STORE_001/anomalies")).json()
    spikes = [a for a in body["active_anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
    assert len(spikes) == 1
    assert spikes[0]["severity"] == "WARN"
    assert "7" in spikes[0]["detail"]


async def test_billing_queue_spike_critical(client, test_app, seed_events):
    _force_closed_now(test_app.state.store_layout)
    ev = _ev(
        "BILLING_QUEUE_JOIN",
        "VIS_q02",
        camera_id="CAM_3",
        zone_id="BILLING",
        queue_depth=11,
        sku_zone="BILLING",
    )
    await seed_events(client, [ev])

    body = (await client.get("/stores/STORE_001/anomalies")).json()
    spikes = [a for a in body["active_anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
    assert len(spikes) == 1
    assert spikes[0]["severity"] == "CRITICAL"
    assert "11" in spikes[0]["detail"]


async def test_no_spike_below_threshold(client, test_app, seed_events):
    _force_closed_now(test_app.state.store_layout)
    ev = _ev(
        "BILLING_QUEUE_JOIN",
        "VIS_q03",
        camera_id="CAM_3",
        zone_id="BILLING",
        queue_depth=4,
        sku_zone="BILLING",
    )
    await seed_events(client, [ev])

    body = (await client.get("/stores/STORE_001/anomalies")).json()
    spikes = [a for a in body["active_anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"]
    assert spikes == []


async def test_dead_zone_during_open_hours(client, test_app, seed_events):
    _force_open_now(test_app.state.store_layout)
    # At least one non-staff event today so the "store has visitors" precondition
    # is satisfied — but NOTHING for MAIN_FLOOR, so it should be flagged dead.
    await seed_events(client, [_ev("ENTRY", "VIS_dz01")])

    body = (await client.get("/stores/STORE_001/anomalies")).json()
    dead_zones = [a for a in body["active_anomalies"] if a["type"] == "DEAD_ZONE"]
    assert any("Main Floor" in a["detail"] for a in dead_zones)


async def test_no_dead_zone_outside_hours(client, test_app, seed_events):
    _force_closed_now(test_app.state.store_layout)
    await seed_events(client, [_ev("ENTRY", "VIS_dz02")])

    body = (await client.get("/stores/STORE_001/anomalies")).json()
    dead_zones = [a for a in body["active_anomalies"] if a["type"] == "DEAD_ZONE"]
    assert dead_zones == []
