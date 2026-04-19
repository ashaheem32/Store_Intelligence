"""Shared test fixtures: temp-DB FastAPI app + httpx.AsyncClient + seed helper.

# PROMPT: "Write pytest fixtures for a FastAPI + aiosqlite app. Each test gets
# a fresh temp-file SQLite DB via DB_PATH env override, loads store_layout.json
# and pos_transactions.csv into app.state (bypassing lifespan so httpx ASGITransport
# works without asgi-lifespan), and provides an httpx.AsyncClient bound to the app."
# CHANGES MADE:
#   - Use tmp_path (fresh file per test) instead of :memory: — avoids the
#     SQLite per-connection scope issue without adding StaticPool config.
#   - Added dispose_engine() before/after each test so the db.py singleton
#     reflects the new DB_PATH (otherwise tests would share the first test's DB).
#   - Manually populate app.state.store_layout and app.state.pos_df here because
#     httpx's ASGITransport does NOT run the FastAPI lifespan; asgi-lifespan is
#     not in project deps.
#   - Use pytest_asyncio.fixture explicitly (works under asyncio_mode=auto and
#     without it; safer across pytest-asyncio versions).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAYOUT_PATH = PROJECT_ROOT / "dataset" / "store_layout.json"
POS_PATH = PROJECT_ROOT / "dataset" / "pos_transactions.csv"


@pytest_asyncio.fixture
async def test_app(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("STORE_LAYOUT_PATH", str(LAYOUT_PATH))
    monkeypatch.setenv("POS_DATA_PATH", str(POS_PATH))

    from app import db as db_module
    await db_module.dispose_engine()
    await db_module.init_db()

    from app.main import app

    with open(LAYOUT_PATH) as f:
        app.state.store_layout = json.load(f)

    if POS_PATH.exists():
        df = pd.read_csv(POS_PATH)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        app.state.pos_df = df
    else:
        app.state.pos_df = pd.DataFrame(
            columns=["store_id", "transaction_id", "timestamp", "basket_value_inr"]
        )

    yield app

    await db_module.dispose_engine()


@pytest_asyncio.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def seed_events():
    """Return an async helper that POSTs a list of event dicts to /events/ingest."""
    async def _seed(client: AsyncClient, events: list[dict]):
        return await client.post("/events/ingest", json=events)
    return _seed
