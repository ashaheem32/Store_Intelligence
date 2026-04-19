"""FastAPI entrypoint: lifespan, middleware, exception handlers, routers."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pandas as pd
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.anomalies import router as anomalies_router
from app.db import init_db
from app.funnel import router as funnel_router
from app.health import router as health_router
from app.heatmap import router as heatmap_router
from app.ingestion import load_pos_df, router as ingestion_router
from app.metrics import router as metrics_router

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)

DEFAULT_DB_PATH = "./data/store.db"
DEFAULT_LAYOUT_PATH = "./dataset/store_layout.json"
DEFAULT_POS_PATH = "./dataset/pos_transactions.csv"
POS_EMPTY_COLUMNS = ["store_id", "transaction_id", "timestamp", "basket_value_inr"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db_path = os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    await init_db()

    # store_layout.json
    layout_path = os.environ.get("STORE_LAYOUT_PATH", DEFAULT_LAYOUT_PATH)
    layout: dict | None = None
    try:
        with open(layout_path) as f:
            layout = json.load(f)
    except FileNotFoundError:
        logger.warning("layout.missing", path=layout_path)
    except Exception as exc:
        logger.error("layout.load_failed", path=layout_path, error=str(exc))
    app.state.store_layout = layout

    # pos_transactions.csv
    pos_path = os.environ.get("POS_DATA_PATH", DEFAULT_POS_PATH)
    pos_df: pd.DataFrame
    if Path(pos_path).exists():
        try:
            pos_df = load_pos_df(pos_path)
        except Exception as exc:
            logger.warning("pos.load_failed", path=pos_path, error=str(exc))
            pos_df = pd.DataFrame(columns=POS_EMPTY_COLUMNS)
    else:
        logger.warning("pos.missing", path=pos_path)
        pos_df = pd.DataFrame(columns=POS_EMPTY_COLUMNS)
    app.state.pos_df = pos_df

    logger.info(
        "startup.summary",
        db_path=db_path,
        store_id=layout.get("store_id") if layout else None,
        pos_rows=len(pos_df),
        zones=len(layout.get("zones", [])) if layout else 0,
        layout_path=layout_path,
        pos_path=pos_path,
    )

    yield


app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    description="Real-time retail analytics from CCTV footage — Apex Retail",
    lifespan=lifespan,
)

# CORS — needed for the live dashboard (browser origin differs from API).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- structured request logging ----------

@app.middleware("http")
async def log_request(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or str(uuid.uuid4())
    request.state.trace_id = trace_id
    t0 = time.monotonic()

    try:
        response = await call_next(request)
    except Exception:
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        logger.exception(
            "request.error",
            trace_id=trace_id,
            method=request.method,
            endpoint=request.url.path,
            latency_ms=latency_ms,
        )
        raise

    latency_ms = round((time.monotonic() - t0) * 1000, 1)

    route = request.scope.get("route")
    endpoint = route.path if route is not None and hasattr(route, "path") else request.url.path
    store_id = request.path_params.get("store_id") if getattr(request, "path_params", None) else None
    event_count = getattr(request.state, "event_count", None)

    response.headers["X-Trace-ID"] = trace_id

    logger.info(
        "request",
        trace_id=trace_id,
        store_id=store_id,
        endpoint=endpoint,
        method=request.method,
        status_code=response.status_code,
        latency_ms=latency_ms,
        event_count=event_count,
    )
    return response


# ---------- exception handlers ----------

def _trace_id_for(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or str(uuid.uuid4())


async def _db_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = _trace_id_for(request)
    logger.exception(
        "db.error",
        trace_id=trace_id,
        endpoint=request.url.path,
        error=str(exc),
    )
    return JSONResponse(
        status_code=503,
        content={"error": "Database unavailable", "trace_id": trace_id},
    )


async def _generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = _trace_id_for(request)
    logger.exception(
        "unhandled.error",
        trace_id=trace_id,
        endpoint=request.url.path,
        error=str(exc),
    )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "trace_id": trace_id},
    )


app.add_exception_handler(SQLAlchemyError, _db_exception_handler)
app.add_exception_handler(sqlite3.Error, _db_exception_handler)
app.add_exception_handler(Exception, _generic_exception_handler)


# ---------- routes ----------

@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "message": "Store Intelligence API",
        "docs": "/docs",
        "health": "/health",
    }


app.include_router(health_router)
app.include_router(ingestion_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(heatmap_router)
app.include_router(anomalies_router)
