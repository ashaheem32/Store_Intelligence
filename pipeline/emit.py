from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

from app.models import EventMetadata, StoreEvent

logger = structlog.get_logger(__name__)

RETRY_DELAYS_S = (1, 2, 4)


def emit_event(**kwargs: Any) -> StoreEvent:
    if not kwargs.get("event_id"):
        kwargs["event_id"] = str(uuid.uuid4())
    if "metadata" not in kwargs or kwargs["metadata"] is None:
        kwargs["metadata"] = EventMetadata()
    return StoreEvent(**kwargs)


def emit_to_file(event: StoreEvent, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(event.model_dump_json() + "\n")


def emit_to_api(events: list[StoreEvent], api_url: str) -> Optional[httpx.Response]:
    url = f"{api_url.rstrip('/')}/events/ingest"
    payload = [e.model_dump(mode="json") for e in events]

    for attempt in range(len(RETRY_DELAYS_S) + 1):
        try:
            resp = httpx.post(url, json=payload, timeout=10.0)
            resp.raise_for_status()
            logger.info(
                "emit_to_api.success",
                url=url,
                count=len(events),
                status=resp.status_code,
                attempt=attempt,
            )
            return resp
        except httpx.HTTPError as e:
            if attempt < len(RETRY_DELAYS_S):
                wait = RETRY_DELAYS_S[attempt]
                logger.warning(
                    "emit_to_api.retry",
                    url=url,
                    count=len(events),
                    attempt=attempt,
                    wait_s=wait,
                    error=str(e),
                )
                time.sleep(wait)
            else:
                logger.error(
                    "emit_to_api.failed",
                    url=url,
                    count=len(events),
                    attempts=attempt + 1,
                    error=str(e),
                )
    return None


def load_store_layout(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    from datetime import datetime, timezone

    layout = load_store_layout("dataset/store_layout.json")
    logger.info(
        "loaded_layout",
        store_id=layout["store_id"],
        cameras=len(layout["cameras"]),
        zones=len(layout["zones"]),
    )

    entry_event = emit_event(
        store_id=layout["store_id"],
        camera_id="CAM_1",
        visitor_id="VIS_demo01",
        event_type="ENTRY",
        timestamp=datetime.now(timezone.utc),
        confidence=0.92,
    )

    dwell_event = emit_event(
        store_id=layout["store_id"],
        camera_id="CAM_2",
        visitor_id="VIS_demo01",
        event_type="ZONE_DWELL",
        timestamp=datetime.now(timezone.utc),
        zone_id="MAIN_FLOOR",
        dwell_ms=42000,
        confidence=0.88,
        metadata={"sku_zone": "MAIN_FLOOR", "session_seq": 3},
    )

    print(entry_event.model_dump_json(indent=2))
    print(dwell_event.model_dump_json(indent=2))

    out_path = "output/demo_events.jsonl"
    emit_to_file(entry_event, out_path)
    emit_to_file(dwell_event, out_path)
    print(f"wrote 2 events to {out_path}")
