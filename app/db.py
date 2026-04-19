"""Async SQLite data layer using SQLAlchemy 2.x Core + aiosqlite.

All writes go through `insert_event` / `upsert_session` with parameterised
statements via SQLAlchemy Core — no f-string SQL anywhere in this module.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    func,
    not_,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

DEFAULT_DB_PATH = "./data/store.db"

metadata = MetaData()

events = Table(
    "events",
    metadata,
    Column("event_id", String, primary_key=True),
    Column("store_id", String, nullable=False),
    Column("camera_id", String, nullable=False),
    Column("visitor_id", String, nullable=False),
    Column("event_type", String, nullable=False),
    Column("timestamp", String, nullable=False),
    Column("zone_id", String, nullable=True),
    Column("dwell_ms", Integer, nullable=False, default=0),
    Column("is_staff", Integer, nullable=False, default=0),
    Column("confidence", Float, nullable=False),
    Column("queue_depth", Integer, nullable=True),
    Column("sku_zone", String, nullable=True),
    Column("session_seq", Integer, nullable=False, default=0),
    Column("ingested_at", String, nullable=False),
    Index("ix_events_store_ts", "store_id", "timestamp"),
    Index("ix_events_visitor_ts", "visitor_id", "timestamp"),
    Index("ix_events_store_type_ts", "store_id", "event_type", "timestamp"),
)

store_sessions = Table(
    "store_sessions",
    metadata,
    Column("session_id", String, primary_key=True),
    Column("visitor_id", String, nullable=False),
    Column("store_id", String, nullable=False),
    Column("entry_ts", String, nullable=True),
    Column("exit_ts", String, nullable=True),
    Column("converted", Integer, nullable=False, default=0),
    Column("is_reentry", Integer, nullable=False, default=0),
    Index("ix_sessions_store_entry", "store_id", "entry_ts"),
    Index("ix_sessions_visitor", "visitor_id"),
)


_engine: Optional[AsyncEngine] = None


def _db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def get_engine() -> AsyncEngine:
    """Return the process-wide AsyncEngine, constructing it on first call."""
    global _engine
    if _engine is None:
        path = _db_path()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{path}"
        _engine = create_async_engine(url)
    return _engine


async def dispose_engine() -> None:
    """Dispose the cached engine — mainly for tests that swap DB_PATH."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def init_db() -> None:
    """Create tables + indexes if they do not already exist."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


@asynccontextmanager
async def get_conn() -> AsyncIterator[AsyncConnection]:
    """Async context manager yielding an AsyncConnection wrapped in a transaction.

    The transaction commits on clean exit and rolls back on exception.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        yield conn


# ---------- helpers (no SQL string-building) ----------

def _to_iso(ts: Any) -> str:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return ts.astimezone(timezone.utc).isoformat()
    if isinstance(ts, str):
        normalised = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return dt.astimezone(timezone.utc).isoformat()
    raise TypeError(f"unsupported timestamp type: {type(ts).__name__}")


def _parse_iso(s: str) -> datetime:
    normalised = s[:-1] + "+00:00" if s.endswith("Z") else s
    return datetime.fromisoformat(normalised)


def _bool_to_int(v: Any) -> int:
    return 1 if v else 0


def _today_start_utc() -> datetime:
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


# ---------- writes ----------

async def insert_event(conn: AsyncConnection, event_dict: dict) -> bool:
    """INSERT OR IGNORE one event. Returns True if a new row was inserted."""
    meta = event_dict.get("metadata") or {}
    if hasattr(meta, "model_dump"):
        meta = meta.model_dump()

    row = {
        "event_id": event_dict["event_id"],
        "store_id": event_dict["store_id"],
        "camera_id": event_dict["camera_id"],
        "visitor_id": event_dict["visitor_id"],
        "event_type": event_dict["event_type"],
        "timestamp": _to_iso(event_dict["timestamp"]),
        "zone_id": event_dict.get("zone_id"),
        "dwell_ms": int(event_dict.get("dwell_ms") or 0),
        "is_staff": _bool_to_int(event_dict.get("is_staff")),
        "confidence": float(event_dict["confidence"]),
        "queue_depth": meta.get("queue_depth"),
        "sku_zone": meta.get("sku_zone"),
        "session_seq": int(meta.get("session_seq") or 0),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    stmt = (
        sqlite_insert(events)
        .values(**row)
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    result = await conn.execute(stmt)
    return (result.rowcount or 0) > 0


async def upsert_session(conn: AsyncConnection, session_dict: dict) -> None:
    """INSERT or UPDATE a session row by session_id."""
    row = {
        "session_id": session_dict["session_id"],
        "visitor_id": session_dict.get("visitor_id", ""),
        "store_id": session_dict.get("store_id", ""),
        "entry_ts": _to_iso(session_dict["entry_ts"]) if session_dict.get("entry_ts") else None,
        "exit_ts": _to_iso(session_dict["exit_ts"]) if session_dict.get("exit_ts") else None,
        "converted": _bool_to_int(session_dict.get("converted", 0)),
        "is_reentry": _bool_to_int(session_dict.get("is_reentry", 0)),
    }
    stmt = sqlite_insert(store_sessions).values(**row)
    update_cols = {k: getattr(stmt.excluded, k) for k in row if k != "session_id"}
    stmt = stmt.on_conflict_do_update(
        index_elements=["session_id"],
        set_=update_cols,
    )
    await conn.execute(stmt)


# ---------- reads ----------

async def get_events_by_store(store_id: str, since: datetime) -> list[dict]:
    since_iso = _to_iso(since)
    stmt = (
        select(events)
        .where(
            and_(
                events.c.store_id == store_id,
                events.c.timestamp >= since_iso,
            )
        )
        .order_by(events.c.timestamp.asc())
    )
    async with get_conn() as conn:
        result = await conn.execute(stmt)
        return [dict(m) for m in result.mappings().all()]


async def get_sessions_by_store(store_id: str, since: datetime) -> list[dict]:
    since_iso = _to_iso(since)
    stmt = (
        select(store_sessions)
        .where(
            and_(
                store_sessions.c.store_id == store_id,
                store_sessions.c.entry_ts >= since_iso,
            )
        )
        .order_by(store_sessions.c.entry_ts.asc())
    )
    async with get_conn() as conn:
        result = await conn.execute(stmt)
        return [dict(m) for m in result.mappings().all()]


async def get_latest_event_ts(store_id: str) -> Optional[datetime]:
    stmt = select(func.max(events.c.timestamp)).where(events.c.store_id == store_id)
    async with get_conn() as conn:
        val = (await conn.execute(stmt)).scalar()
    if val is None:
        return None
    return _parse_iso(val)


async def get_active_visitor_count(store_id: str) -> int:
    """Distinct non-staff visitors with an ENTRY today and no later EXIT."""
    start_iso = _today_start_utc().isoformat()

    e1 = events.alias("e1")
    e2 = events.alias("e2")

    exit_exists = (
        select(1)
        .where(
            and_(
                e2.c.visitor_id == e1.c.visitor_id,
                e2.c.store_id == e1.c.store_id,
                e2.c.event_type == "EXIT",
                e2.c.timestamp > e1.c.timestamp,
            )
        )
        .exists()
    )

    stmt = (
        select(func.count(func.distinct(e1.c.visitor_id)))
        .select_from(e1)
        .where(
            and_(
                e1.c.store_id == store_id,
                e1.c.event_type == "ENTRY",
                e1.c.is_staff == 0,
                e1.c.timestamp >= start_iso,
                not_(exit_exists),
            )
        )
    )
    async with get_conn() as conn:
        val = (await conn.execute(stmt)).scalar()
    return int(val or 0)
