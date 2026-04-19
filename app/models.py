from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator

UUID_V4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

EventType = Literal[
    "ENTRY",
    "EXIT",
    "ZONE_ENTER",
    "ZONE_EXIT",
    "ZONE_DWELL",
    "BILLING_QUEUE_JOIN",
    "BILLING_QUEUE_ABANDON",
    "REENTRY",
]


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0


class StoreEvent(BaseModel):
    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float
    metadata: EventMetadata

    @field_validator("event_id")
    @classmethod
    def _validate_event_id(cls, v: str) -> str:
        if not UUID_V4_PATTERN.fullmatch(v):
            raise ValueError(f"event_id {v!r} is not a valid UUID v4")
        return v

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, v: float) -> float:
        if v < 0.0 or v > 1.0:
            raise ValueError(f"confidence {v} must be between 0.0 and 1.0")
        return v

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _validate_zone_id_for_entry_exit(self) -> "StoreEvent":
        if self.event_type in ("ENTRY", "EXIT") and self.zone_id is not None:
            raise ValueError(
                f"zone_id must be None for event_type {self.event_type}"
            )
        return self
