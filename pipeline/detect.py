#!/usr/bin/env python3
"""CCTV detection pipeline — YOLOv8 + ByteTrack + camera-role-specific event emission."""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import structlog
import supervision as sv
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)
from ultralytics import YOLO

from app.models import StoreEvent
from pipeline.emit import emit_event, emit_to_api, emit_to_file, load_store_layout
from pipeline.tracker import SessionTracker

logger = structlog.get_logger(__name__)
console = Console()

PERSON_CLASS_ID = 0
CONF_THRESHOLD = 0.3
LOW_CONF_WARN = 0.4
LINE_Y_RATIO = 0.45
CROSSING_DEBOUNCE_S = 2.0
ZONE_EXIT_ABSENCE_S = 3.0
DWELL_EMIT_INTERVAL_S = 30.0
STAFF_THRESHOLD_S = 300.0
API_FLUSH_SIZE = 50
PROGRESS_UPDATE_EVERY_N_FRAMES = 5


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="CCTV detection pipeline: clip → events.jsonl",
    )
    p.add_argument("--clip", required=True,
                   help='path to video clip, e.g. "Footage/CAM 1.mp4" (spaces OK)')
    p.add_argument("--camera_id", required=True,
                   help="camera_id from store_layout.json (e.g. CAM_1)")
    p.add_argument("--layout", default="./dataset/store_layout.json")
    p.add_argument("--out", default=None,
                   help="output .jsonl path (default: ./output/events_{camera_id}.jsonl)")
    p.add_argument("--model", default="yolov8n.pt")
    p.add_argument("--api_url", default=None,
                   help="if set, batch-POST events to {api_url}/events/ingest")
    return p.parse_args()


def resolve_camera(layout: dict, camera_id: str) -> dict:
    for cam in layout["cameras"]:
        if cam["camera_id"] == camera_id:
            return cam
    raise ValueError(f"camera_id {camera_id!r} not found in layout")


def camera_kind(role: str) -> str:
    if role in ("ENTRY", "ENTRY_2"):
        return "threshold"
    if role == "BILLING":
        return "billing"
    return "zone"


def main() -> int:
    args = parse_args()

    layout = load_store_layout(args.layout)
    store_id = layout["store_id"]
    cam_cfg = resolve_camera(layout, args.camera_id)
    role = cam_cfg["role"]
    zone_id = cam_cfg["zone_id"]
    kind = camera_kind(role)

    out_path = args.out or f"./output/events_{args.camera_id}.jsonl"

    logger.info(
        "detect.start",
        clip=args.clip,
        camera_id=args.camera_id,
        role=role,
        kind=kind,
        zone_id=zone_id,
        out=out_path,
        api_url=args.api_url,
    )

    clip_path = args.clip
    if not Path(clip_path).exists():
        raise FileNotFoundError(f"clip not found: {clip_path}")

    logger.info("loading_model", model=args.model)
    model = YOLO(args.model)

    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        raise RuntimeError(f"failed to open clip: {clip_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    line_y = frame_h * LINE_Y_RATIO
    clip_start = datetime.now(timezone.utc)

    byte_tracker = sv.ByteTrack()
    session = SessionTracker(store_id=store_id, camera_id=args.camera_id)

    track_first_seen: dict[int, datetime] = {}
    track_last_seen: dict[int, datetime] = {}
    track_last_cy: dict[int, float] = {}
    track_last_cross_ts: dict[int, datetime] = {}
    track_last_dwell_ts: dict[int, datetime] = {}
    track_last_conf: dict[int, float] = {}
    track_visitor_id: dict[int, str] = {}
    in_zone: set[int] = set()
    joined_queue: set[int] = set()
    staff: set[int] = set()

    api_buffer: list[StoreEvent] = []
    event_counter: Counter[str] = Counter()
    events_emitted = 0

    def flush_api(force: bool = False) -> None:
        if not args.api_url or not api_buffer:
            return
        if force or len(api_buffer) >= API_FLUSH_SIZE:
            emit_to_api(list(api_buffer), args.api_url)
            api_buffer.clear()

    def emit(ev_kwargs: dict) -> StoreEvent:
        nonlocal events_emitted
        ev = emit_event(store_id=store_id, camera_id=args.camera_id, **ev_kwargs)
        emit_to_file(ev, out_path)
        event_counter[ev.event_type] += 1
        events_emitted += 1
        if ev.confidence < LOW_CONF_WARN:
            logger.warning(
                "low_confidence_event",
                event_type=ev.event_type,
                confidence=ev.confidence,
                visitor_id=ev.visitor_id,
            )
        if args.api_url:
            api_buffer.append(ev)
            flush_api()
        return ev

    with Progress(
        TextColumn("[cyan]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("fps={task.fields[fps]:.1f} events={task.fields[events]}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        clip_name = Path(clip_path).name
        task_total = total_frames if total_frames > 0 else None
        task = progress.add_task(clip_name, total=task_total, fps=0.0, events=0)

        frame_idx = 0
        wall_t0 = time.time()

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1
            now = clip_start + timedelta(seconds=frame_idx / fps)

            results = model(
                frame,
                classes=[PERSON_CLASS_ID],
                conf=CONF_THRESHOLD,
                verbose=False,
            )[0]
            detections = sv.Detections.from_ultralytics(results)
            detections = byte_tracker.update_with_detections(detections)

            seen_now: set[int] = set()

            if detections.tracker_id is not None and len(detections.tracker_id) > 0:
                for i in range(len(detections.tracker_id)):
                    tid_raw = detections.tracker_id[i]
                    if tid_raw is None:
                        continue
                    tid = int(tid_raw)
                    seen_now.add(tid)

                    conf_f = (
                        float(detections.confidence[i])
                        if detections.confidence is not None
                        else 0.0
                    )
                    x1, y1, x2, y2 = (float(v) for v in detections.xyxy[i])
                    cy = (y1 + y2) / 2.0

                    if tid not in track_first_seen:
                        track_first_seen[tid] = now
                    track_last_seen[tid] = now
                    track_last_conf[tid] = conf_f

                    continuous_s = (now - track_first_seen[tid]).total_seconds()
                    if continuous_s > STAFF_THRESHOLD_S and tid not in staff:
                        staff.add(tid)
                        logger.warning(
                            "staff_detected",
                            track_id=tid,
                            continuous_s=round(continuous_s, 1),
                            camera_id=args.camera_id,
                            note="heuristic — TODO: replace with VLM uniform detection",
                        )
                    is_staff = tid in staff

                    if tid not in track_visitor_id:
                        track_visitor_id[tid] = session.get_visitor_id(
                            track_id=tid,
                            bbox=(x1, y1, x2, y2),
                            timestamp=now,
                        )
                    vid = track_visitor_id[tid]

                    if kind == "threshold":
                        prev_cy = track_last_cy.get(tid)
                        if prev_cy is not None:
                            crossed_down = prev_cy < line_y <= cy
                            crossed_up = prev_cy > line_y >= cy
                            if crossed_down or crossed_up:
                                last_cross = track_last_cross_ts.get(tid)
                                debounced = (
                                    last_cross is not None
                                    and (now - last_cross).total_seconds() < CROSSING_DEBOUNCE_S
                                )
                                if not debounced:
                                    et = "ENTRY" if crossed_down else "EXIT"
                                    emit({
                                        "visitor_id": vid,
                                        "event_type": et,
                                        "timestamp": now,
                                        "zone_id": None,
                                        "dwell_ms": 0,
                                        "is_staff": is_staff,
                                        "confidence": conf_f,
                                    })
                                    track_last_cross_ts[tid] = now
                        track_last_cy[tid] = cy

                    else:  # zone or billing
                        if tid not in in_zone:
                            in_zone.add(tid)
                            track_last_dwell_ts[tid] = now
                            emit({
                                "visitor_id": vid,
                                "event_type": "ZONE_ENTER",
                                "timestamp": now,
                                "zone_id": zone_id,
                                "dwell_ms": 0,
                                "is_staff": is_staff,
                                "confidence": conf_f,
                                "metadata": {"sku_zone": zone_id},
                            })
                            if kind == "billing":
                                others = len(in_zone) - 1
                                if others > 0:
                                    emit({
                                        "visitor_id": vid,
                                        "event_type": "BILLING_QUEUE_JOIN",
                                        "timestamp": now,
                                        "zone_id": zone_id,
                                        "dwell_ms": 0,
                                        "is_staff": is_staff,
                                        "confidence": conf_f,
                                        "metadata": {
                                            "queue_depth": len(in_zone),
                                            "sku_zone": zone_id,
                                        },
                                    })
                                    joined_queue.add(tid)
                        else:
                            last_dwell = track_last_dwell_ts.get(tid, track_first_seen[tid])
                            if (now - last_dwell).total_seconds() >= DWELL_EMIT_INTERVAL_S:
                                dwell_ms = int(
                                    (now - track_first_seen[tid]).total_seconds() * 1000
                                )
                                emit({
                                    "visitor_id": vid,
                                    "event_type": "ZONE_DWELL",
                                    "timestamp": now,
                                    "zone_id": zone_id,
                                    "dwell_ms": dwell_ms,
                                    "is_staff": is_staff,
                                    "confidence": conf_f,
                                    "metadata": {"sku_zone": zone_id},
                                })
                                track_last_dwell_ts[tid] = now

            if kind in ("zone", "billing"):
                to_remove: list[int] = []
                for tid in list(in_zone):
                    if tid in seen_now:
                        continue
                    last_seen = track_last_seen.get(tid)
                    if last_seen is None:
                        continue
                    if (now - last_seen).total_seconds() > ZONE_EXIT_ABSENCE_S:
                        vid = track_visitor_id.get(tid)
                        if vid is None:
                            to_remove.append(tid)
                            continue
                        dwell_ms = int(
                            (last_seen - track_first_seen[tid]).total_seconds() * 1000
                        )
                        is_staff = tid in staff
                        conf_last = track_last_conf.get(tid, 0.5)
                        emit({
                            "visitor_id": vid,
                            "event_type": "ZONE_EXIT",
                            "timestamp": now,
                            "zone_id": zone_id,
                            "dwell_ms": dwell_ms,
                            "is_staff": is_staff,
                            "confidence": conf_last,
                            "metadata": {"sku_zone": zone_id},
                        })
                        if kind == "billing" and tid in joined_queue:
                            emit({
                                "visitor_id": vid,
                                "event_type": "BILLING_QUEUE_ABANDON",
                                "timestamp": now,
                                "zone_id": zone_id,
                                "dwell_ms": dwell_ms,
                                "is_staff": is_staff,
                                "confidence": conf_last,
                                "metadata": {"sku_zone": zone_id},
                            })
                            joined_queue.discard(tid)
                        to_remove.append(tid)
                for tid in to_remove:
                    in_zone.discard(tid)

            if frame_idx % PROGRESS_UPDATE_EVERY_N_FRAMES == 0 or (
                task_total and frame_idx == task_total
            ):
                wall_elapsed = time.time() - wall_t0
                fps_curr = frame_idx / wall_elapsed if wall_elapsed > 0 else 0.0
                progress.update(task, completed=frame_idx, fps=fps_curr, events=events_emitted)

        progress.update(task, completed=frame_idx, events=events_emitted)

    cap.release()
    flush_api(force=True)

    duration_s = frame_idx / fps if fps > 0 else 0.0
    console.rule("[bold green]Detection complete")
    console.print(f"[bold]Clip:[/bold]     {clip_path}")
    console.print(
        f"[bold]Camera:[/bold]   {args.camera_id}  "
        f"(role={role}, kind={kind}, zone={zone_id})"
    )
    console.print(f"[bold]Output:[/bold]   {out_path}")
    frames_str = f"{frame_idx}" + (f" / {total_frames}" if total_frames else "")
    console.print(f"[bold]Frames:[/bold]   {frames_str}")
    console.print(
        f"[bold]Duration:[/bold] {duration_s:.1f}s ({duration_s/60:.1f}min)"
    )
    console.print(f"[bold]Total events:[/bold] {events_emitted}")
    if event_counter:
        console.print("[bold]By type:[/bold]")
        for et, n in event_counter.most_common():
            console.print(f"  {et:<24} {n}")
    else:
        console.print("[dim]No events emitted.[/dim]")

    logger.info(
        "detect.complete",
        camera_id=args.camera_id,
        frames=frame_idx,
        duration_s=round(duration_s, 1),
        events=events_emitted,
        by_type=dict(event_counter),
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
