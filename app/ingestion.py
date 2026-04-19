"""POST /events/ingest — validate, deduplicate, session-track, POS-correlate."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Optional

import pandas as pd
import structlog
from fastapi import APIRouter, Body, HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, select

from app.db import get_conn, insert_event, store_sessions, upsert_session
from app.models import StoreEvent

logger = structlog.get_logger(__name__)

MAX_BATCH = 500
POS_WINDOW_MINUTES = 5

router = APIRouter(tags=["ingest"])


# ---------- response models ----------

class IngestError(BaseModel):
    index: int
    event_id: Optional[str] = None
    reason: str


class IngestResponse(BaseModel):
    accepted: int
    duplicate: int
    invalid: int
    errors: list[IngestError]


# ---------- POS data loading ----------

def load_pos_df(path: str) -> pd.DataFrame:
    """Load POS transactions CSV. Called once at app startup; stored on app.state."""
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


# ---------- OpenAPI example (subset of dataset/sample_events.jsonl) ----------

EXAMPLE_REQUEST_BODY = [
    {
        "event_id": "a3e8f1c0-5b7d-4e2a-8f1b-2c9d7e3a1b4f",
        "store_id": "STORE_001",
        "camera_id": "CAM_1",
        "visitor_id": "VIS_a1b2c3",
        "event_type": "ENTRY",
        "timestamp": "2026-04-19T09:05:12Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.94,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    },
    {
        "event_id": "f4a7b2c5-9d1e-42f3-8b6c-4d9a1e5f2c8b",
        "store_id": "STORE_001",
        "camera_id": "CAM_3",
        "visitor_id": "VIS_a1b2c3",
        "event_type": "BILLING_QUEUE_JOIN",
        "timestamp": "2026-04-19T09:12:30Z",
        "zone_id": "BILLING",
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.88,
        "metadata": {"queue_depth": 2, "sku_zone": "BILLING", "session_seq": 6},
    },
]


# ---------- helpers ----------

def _format_validation_error(e: ValidationError) -> str:
    parts: list[str] = []
    for err in e.errors()[:3]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = err.get("msg", "?")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "validation error"


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat()


async def _find_open_session(conn, store_id: str, visitor_id: str) -> Optional[dict]:
    stmt = (
        select(store_sessions)
        .where(
            and_(
                store_sessions.c.store_id == store_id,
                store_sessions.c.visitor_id == visitor_id,
                store_sessions.c.exit_ts.is_(None),
            )
        )
        .order_by(store_sessions.c.entry_ts.desc())
        .limit(1)
    )
    row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


async def _find_session_at(
    conn, store_id: str, visitor_id: str, ts: datetime
) -> Optional[dict]:
    """Latest session for this visitor whose entry_ts <= ts."""
    stmt = (
        select(store_sessions)
        .where(
            and_(
                store_sessions.c.store_id == store_id,
                store_sessions.c.visitor_id == visitor_id,
                store_sessions.c.entry_ts <= _iso(ts),
            )
        )
        .order_by(store_sessions.c.entry_ts.desc())
        .limit(1)
    )
    row = (await conn.execute(stmt)).mappings().first()
    return dict(row) if row else None


async def _apply_session_side_effect(conn, ev: StoreEvent) -> None:
    if ev.event_type in ("ENTRY", "REENTRY"):
        await upsert_session(conn, {
            "session_id": str(uuid.uuid4()),
            "visitor_id": ev.visitor_id,
            "store_id": ev.store_id,
            "entry_ts": ev.timestamp,
            "exit_ts": None,
            "converted": 0,
            "is_reentry": 1 if ev.event_type == "REENTRY" else 0,
        })
    elif ev.event_type == "EXIT":
        open_sess = await _find_open_session(conn, ev.store_id, ev.visitor_id)
        if open_sess is None:
            return
        merged = dict(open_sess)
        merged["exit_ts"] = ev.timestamp
        await upsert_session(conn, merged)


def _pos_match(pos_df: Optional[pd.DataFrame], store_id: str, event_ts: datetime) -> bool:
    if pos_df is None or pos_df.empty:
        return False
    window_end = event_ts + timedelta(minutes=POS_WINDOW_MINUTES)
    mask = (
        (pos_df["store_id"] == store_id)
        & (pos_df["timestamp"] > event_ts)
        & (pos_df["timestamp"] <= window_end)
    )
    return bool(mask.any())


async def _correlate_pos(
    conn, new_events: list[StoreEvent], pos_df: Optional[pd.DataFrame]
) -> int:
    if pos_df is None or pos_df.empty:
        return 0
    updated = 0
    for ev in new_events:
        if ev.event_type not in ("BILLING_QUEUE_JOIN", "EXIT"):
            continue
        if ev.is_staff:
            continue
        if not _pos_match(pos_df, ev.store_id, ev.timestamp):
            continue
        sess = await _find_session_at(conn, ev.store_id, ev.visitor_id, ev.timestamp)
        if sess is None or sess.get("converted"):
            continue
        merged = dict(sess)
        merged["converted"] = 1
        await upsert_session(conn, merged)
        updated += 1
    return updated


# ---------- endpoint ----------

@router.post(
    "/events/ingest",
    response_model=IngestResponse,
    summary="Ingest store events",
    description=(
        "Idempotent by event_id. Partial success on malformed events. "
        "Each event is validated independently; invalid entries are listed in "
        "`errors` and do not abort the batch. Max 500 events per request."
    ),
)
async def ingest_events(
    request: Request,
    body: Annotated[
        list[dict[str, Any]],
        Body(
            description="Batch of up to 500 StoreEvent dicts.",
            examples=[EXAMPLE_REQUEST_BODY],
        ),
    ],
) -> IngestResponse:
    t0 = time.monotonic()
    trace_id = getattr(request.state, "trace_id", None) or str(uuid.uuid4())
    # Surface event_count to the logging middleware.
    request.state.event_count = len(body)

    if len(body) > MAX_BATCH:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"batch size {len(body)} exceeds max {MAX_BATCH}",
        )

    pos_df: Optional[pd.DataFrame] = getattr(request.app.state, "pos_df", None)

    accepted = 0
    duplicate = 0
    invalid = 0
    errors: list[IngestError] = []
    new_events: list[StoreEvent] = []
    store_id_seen: Optional[str] = None

    async with get_conn() as conn:
        for i, raw in enumerate(body):
            raw_event_id = (
                raw.get("event_id") if isinstance(raw, dict) else None
            )
            try:
                ev = (
                    StoreEvent(**raw)
                    if isinstance(raw, dict)
                    else StoreEvent.model_validate(raw)
                )
            except ValidationError as e:
                invalid += 1
                errors.append(IngestError(
                    index=i,
                    event_id=raw_event_id if isinstance(raw_event_id, str) else None,
                    reason=_format_validation_error(e),
                ))
                continue
            except (TypeError, ValueError) as e:
                invalid += 1
                errors.append(IngestError(
                    index=i,
                    event_id=raw_event_id if isinstance(raw_event_id, str) else None,
                    reason=str(e),
                ))
                continue

            if store_id_seen is None:
                store_id_seen = ev.store_id

            is_new = await insert_event(conn, ev.model_dump(mode="python"))
            if not is_new:
                duplicate += 1
                continue

            accepted += 1
            new_events.append(ev)
            await _apply_session_side_effect(conn, ev)

        if new_events:
            await _correlate_pos(conn, new_events, pos_df)

    latency_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "events.ingest",
        trace_id=trace_id,
        store_id=store_id_seen,
        endpoint="/events/ingest",
        event_count=len(body),
        accepted=accepted,
        duplicate=duplicate,
        invalid=invalid,
        errors=len(errors),
        status_code=200,
        latency_ms=latency_ms,
    )

    return IngestResponse(
        accepted=accepted,
        duplicate=duplicate,
        invalid=invalid,
        errors=errors,
    )
