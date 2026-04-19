"""Visitor Re-ID and session management.

SessionTracker is the per-process registry for visitor sessions. It owns:
  - visitor_id assignment (deterministic MD5 hash of store+camera+track_id)
  - re-entry detection via an exit_registry and RE_ENTRY_WINDOW_SECONDS
  - cross-camera dedup via a first_seen map
  - session event history (for session_seq and dwell aggregation)

LIMITATIONS
-----------
1. State is in-memory per process. Independent detect.py runs each construct a
   fresh tracker and do NOT share state. For cross-camera dedup across separate
   CLI invocations, an external persistence layer (file/DB) would be needed.
2. visitor_id is derived from (store_id, camera_id, track_id). The same physical
   person on CAM_1 and CAM_2 therefore gets DIFFERENT visitor_ids unless an
   upstream Re-ID step unifies them. The cross-camera dedup helpers below only
   trigger when visitor_ids match — they rely on that upstream unification.
3. Group entry handling is implicit: ByteTrack assigns distinct track_ids to each
   person crossing together, so each gets a unique visitor_id via this module.
   No merging is performed here.
"""
from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from datetime import datetime
from typing import Optional

import structlog

from app.models import StoreEvent

logger = structlog.get_logger(__name__)

DEFAULT_REENTRY_WINDOW_S = 120.0
CROSS_CAMERA_DEDUP_WINDOW_S = 30.0


def _hash_visitor_id(store_id: str, camera_id: str, track_id: int) -> str:
    key = f"{store_id}{camera_id}{track_id}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return f"VIS_{digest[:6]}"


class SessionTracker:
    session_events: dict[str, list[StoreEvent]]
    exit_registry: dict[str, datetime]
    active_visitors: set[str]
    first_seen: dict[str, datetime]

    def __init__(
        self,
        store_id: Optional[str] = None,
        camera_id: Optional[str] = None,
        reentry_window_seconds: Optional[float] = None,
    ) -> None:
        self._default_store_id = store_id
        self._default_camera_id = camera_id

        if reentry_window_seconds is None:
            reentry_window_seconds = float(
                os.environ.get("RE_ENTRY_WINDOW_SECONDS", DEFAULT_REENTRY_WINDOW_S)
            )
        self.reentry_window_s: float = reentry_window_seconds

        self.session_events = defaultdict(list)
        self.exit_registry = {}
        self.active_visitors = set()
        self.first_seen = {}
        self._track_to_visitor: dict[tuple[str, str, int], str] = {}

    # ---------- visitor_id assignment ----------

    def assign_visitor_id(self, store_id: str, camera_id: str, track_id: int) -> str:
        key = (store_id, camera_id, int(track_id))
        vid = self._track_to_visitor.get(key)
        if vid is None:
            vid = _hash_visitor_id(store_id, camera_id, int(track_id))
            self._track_to_visitor[key] = vid
            logger.debug(
                "visitor_id.assigned",
                store_id=store_id,
                camera_id=camera_id,
                track_id=int(track_id),
                visitor_id=vid,
            )
        return vid

    def get_visitor_id(self, track_id: int, bbox=None, timestamp=None) -> str:
        """Compatibility shim for detect.py.

        Uses the store_id/camera_id passed at construction time. `bbox` and
        `timestamp` are accepted for forward compatibility (future appearance-
        based Re-ID) but ignored by the current MD5 strategy.
        """
        if self._default_store_id is None or self._default_camera_id is None:
            raise RuntimeError(
                "get_visitor_id requires store_id and camera_id on the tracker; "
                "use assign_visitor_id(store_id, camera_id, track_id) instead."
            )
        return self.assign_visitor_id(
            self._default_store_id, self._default_camera_id, track_id
        )

    # ---------- re-entry ----------

    def is_reentry(self, visitor_id: str, current_ts: datetime) -> bool:
        last_exit = self.exit_registry.get(visitor_id)
        if last_exit is None:
            return False
        delta_s = (current_ts - last_exit).total_seconds()
        return 0 <= delta_s <= self.reentry_window_s

    # ---------- cross-camera dedup ----------

    def recently_entered(
        self,
        visitor_id: str,
        current_ts: datetime,
        window_s: float = CROSS_CAMERA_DEDUP_WINDOW_S,
    ) -> bool:
        """True if this visitor_id was first_seen within window_s of current_ts.

        Intended use: a FLOOR camera consults this before emitting ENTRY. If the
        visitor already has a recent ENTRY (from an ENTRY camera), the FLOOR
        camera should emit ZONE_ENTER only, not a duplicate ENTRY.
        """
        first = self.first_seen.get(visitor_id)
        if first is None:
            return False
        delta_s = (current_ts - first).total_seconds()
        return 0 <= delta_s <= window_s

    # ---------- event recording ----------

    def record_event(self, event: StoreEvent) -> None:
        self.session_events[event.visitor_id].append(event)

        if event.event_type in ("ENTRY", "REENTRY"):
            self.active_visitors.add(event.visitor_id)
            self.first_seen.setdefault(event.visitor_id, event.timestamp)
        elif event.event_type == "EXIT":
            self.mark_exit(event.visitor_id, event.timestamp)

    def mark_exit(self, visitor_id: str, ts: datetime) -> None:
        self.exit_registry[visitor_id] = ts
        self.active_visitors.discard(visitor_id)

    # ---------- queries ----------

    def get_session_seq(self, visitor_id: str) -> int:
        return len(self.session_events.get(visitor_id, [])) + 1

    def get_active_sessions(self) -> list[str]:
        return sorted(self.active_visitors)

    def get_dwell_ms(self, visitor_id: str, zone_id: str) -> int:
        """Sum of cumulative dwell_ms across all visits to this zone.

        Walks ZONE_ENTER → (ZONE_DWELL)* → ZONE_EXIT sequences. For a visit
        still in progress (no ZONE_EXIT yet), uses the latest ZONE_DWELL value.
        """
        total = 0
        pending = 0
        for ev in self.session_events.get(visitor_id, []):
            if ev.zone_id != zone_id:
                continue
            if ev.event_type == "ZONE_ENTER":
                pending = 0
            elif ev.event_type == "ZONE_DWELL":
                pending = ev.dwell_ms
            elif ev.event_type == "ZONE_EXIT":
                total += ev.dwell_ms or pending
                pending = 0
        return total + pending
