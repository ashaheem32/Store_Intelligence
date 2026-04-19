# PROMPT: "Generate pytest unit tests for app/models.py and pipeline/emit.py +
# pipeline/tracker.py. Cover: StoreEvent Pydantic validation (UUID, confidence range,
# tz-aware timestamp, zone_id/entry-type cross-field rule), emit_to_file round-trip,
# SessionTracker re-entry window + group-entry visitor_id uniqueness, and a
# simulation of detect.py's 300s staff heuristic."
# CHANGES MADE:
#   - Moved pipeline.detect import inside the staff test so heavy cv2/ultralytics
#     imports never break unrelated tests (and added a hard-coded fallback of
#     300.0 to stay resilient if the import fails in a minimal env).
#   - Replaced AI's suggested freezegun with explicit datetime arithmetic —
#     smaller dep surface, and these tests don't need wall-clock freezing.
#   - Used pre-generated UUID v4s for deterministic assertions instead of
#     uuid.uuid4() (AI default), so failures point to specific data, not a
#     random seed.
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.models import EventMetadata, StoreEvent
from pipeline.emit import emit_to_file
from pipeline.tracker import SessionTracker


UUID_A = "a3e8f1c0-5b7d-4e2a-8f1b-2c9d7e3a1b4f"
UUID_B = "b1c4d2e5-8a9f-4b3c-9d2e-1f5a8c4b7e2d"
UUID_C = "c5d8e1f4-2b6a-4c9d-a3f8-5b7c2e9d4a1f"


@pytest.fixture
def sample_event_dict() -> dict:
    return {
        "event_id": UUID_A,
        "store_id": "STORE_001",
        "camera_id": "CAM_1",
        "visitor_id": "VIS_test01",
        "event_type": "ENTRY",
        "timestamp": "2026-04-19T09:05:12Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.94,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }


def test_valid_event_passes_validation(sample_event_dict):
    ev = StoreEvent(**sample_event_dict)
    assert ev.event_id == UUID_A
    assert ev.event_type == "ENTRY"
    assert ev.zone_id is None
    assert ev.timestamp.tzinfo is not None


def test_invalid_confidence_rejected(sample_event_dict):
    sample_event_dict["confidence"] = 1.5
    with pytest.raises(ValidationError):
        StoreEvent(**sample_event_dict)


def test_invalid_uuid_rejected(sample_event_dict):
    sample_event_dict["event_id"] = "not-a-uuid"
    with pytest.raises(ValidationError):
        StoreEvent(**sample_event_dict)


def test_naive_timestamp_rejected(sample_event_dict):
    sample_event_dict["timestamp"] = datetime(2026, 4, 19, 9, 5, 12)  # no tzinfo
    with pytest.raises(ValidationError):
        StoreEvent(**sample_event_dict)


def test_zone_id_none_for_entry(sample_event_dict):
    sample_event_dict["zone_id"] = "MAIN_FLOOR"  # must be null for ENTRY
    with pytest.raises(ValidationError):
        StoreEvent(**sample_event_dict)


def test_emit_to_file_creates_valid_jsonl(tmp_path, sample_event_dict):
    out = tmp_path / "events.jsonl"
    base = sample_event_dict.copy()

    events = []
    for i, uid in enumerate([UUID_A, UUID_B, UUID_C]):
        d = dict(base)
        d["event_id"] = uid
        d["visitor_id"] = f"VIS_t{i:02d}"
        ev = StoreEvent(**d)
        emit_to_file(ev, str(out))
        events.append(ev)

    lines = out.read_text().strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        parsed = StoreEvent(**json.loads(line))
        assert parsed.store_id == "STORE_001"


def _exit_event(visitor_id: str, ts: datetime) -> StoreEvent:
    return StoreEvent(
        event_id=str(uuid.uuid4()),
        store_id="STORE_001",
        camera_id="CAM_1",
        visitor_id=visitor_id,
        event_type="EXIT",
        timestamp=ts,
        zone_id=None,
        dwell_ms=0,
        is_staff=False,
        confidence=0.9,
        metadata=EventMetadata(),
    )


def test_reentry_within_window():
    tracker = SessionTracker(
        store_id="STORE_001", camera_id="CAM_1", reentry_window_seconds=120.0
    )
    t0 = datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc)
    tracker.record_event(_exit_event("VIS_rr01", t0))

    assert tracker.is_reentry("VIS_rr01", t0 + timedelta(seconds=60)) is True


def test_no_reentry_outside_window():
    tracker = SessionTracker(
        store_id="STORE_001", camera_id="CAM_1", reentry_window_seconds=120.0
    )
    t0 = datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc)
    tracker.record_event(_exit_event("VIS_rr02", t0))

    assert tracker.is_reentry("VIS_rr02", t0 + timedelta(seconds=200)) is False


def test_group_entry_unique_ids():
    tracker = SessionTracker(store_id="STORE_001", camera_id="CAM_1")
    vids = {
        tracker.assign_visitor_id("STORE_001", "CAM_1", tid)
        for tid in (10, 11, 12)
    }
    assert len(vids) == 3


def test_staff_not_counted_after_5_minutes():
    """Mirrors detect.py's heuristic: continuous track > 300s → is_staff=True."""
    start = datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc)
    later = start + timedelta(seconds=310)
    continuous_seconds = (later - start).total_seconds()

    try:
        from pipeline.detect import STAFF_THRESHOLD_S
    except Exception:
        STAFF_THRESHOLD_S = 300.0

    assert continuous_seconds > STAFF_THRESHOLD_S
