# Store Intelligence — Design

## System Architecture

The system is a four-stage pipeline that turns raw retail CCTV into live operational signals. Each stage has a single responsibility and a narrow contract with the next, so any one stage can be replaced without touching the others.

```
 ┌────────────┐     ┌─────────────────┐     ┌────────────────┐     ┌────────────────┐     ┌──────────────┐
 │  Footage   │ ──▶ │   Detection     │ ──▶ │     Events     │ ──▶ │      API       │ ──▶ │  Dashboard    │
 │ Footage/   │     │ YOLOv8 + Byte   │     │  StoreEvent    │     │  FastAPI +     │     │  rich.Live    │
 │ CAM_*.mp4  │     │ Track per cam   │     │  (JSONL/HTTP)  │     │  SQLite        │     │  terminal UI  │
 └────────────┘     └─────────────────┘     └────────────────┘     └────────────────┘     └──────────────┘
       │                    │                       │                       │                      │
       │ frames @ fps       │ tracker_id            │ event_id (UUIDv4)     │ INSERT OR IGNORE     │
       │                    │ visitor_id (md5)      │ store_id, ts          │ derived metrics      │
       │                    │                       │ confidence (raw)      │ + anomalies          │
       ▼                    ▼                       ▼                       ▼                      ▼
   .mp4 clips          per-frame boxes           events.jsonl           SQLite (data/store.db)   3s/5s/10s polls
```

1. **Footage** — five fixed CCTV clips under `Footage/`, one per camera. Cameras have roles in `dataset/store_layout.json` (ENTRY threshold, FLOOR zone, BILLING zone).
2. **Detection** — `pipeline/detect.py` reads a clip frame-by-frame, runs YOLOv8n person detection, and feeds detections into `supervision.ByteTrack` to maintain identities across frames.
3. **Events** — each role-specific behaviour (line crossing, zone enter/exit, queue join/abandon) is converted into a `StoreEvent` and either written to `output/events_*.jsonl` or POSTed in batches of 50 to `/events/ingest`.
4. **API** — `app/main.py` mounts six routers (health, ingestion, metrics, funnel, heatmap, anomalies). All reads come from SQLite via SQLAlchemy Core async.
5. **Dashboard** — `dashboard/dashboard.py` polls the API on independent intervals (3s/5s/10s) and renders a four-panel `rich.Layout`.

## Detection Layer Design

YOLOv8n runs once per frame in `app/pipeline/detect.py:181`, restricted to `classes=[0]` (person) with `conf=0.3`. The lightweight `n` (nano) variant is the only practical choice on a Mac M-series CPU at interactive frame rates — heavier variants drop below 5 fps.

Detections are passed straight into `supervision.ByteTrack` (`detect.py:188`). ByteTrack maintains identity across frames using IoU + motion cues, and assigns a `tracker_id` that survives short occlusions. Crucially, ByteTrack does **not** re-identify across cameras or across long gaps — that's a known limitation called out in `pipeline/tracker.py:9`.

`visitor_id` is derived deterministically from `(store_id, camera_id, track_id)` via MD5 (`pipeline/tracker.py:40`). The same `track_id` always produces the same `visitor_id`, so re-running detection on the same clip yields the same IDs — which is what makes ingestion idempotent end-to-end.

Threshold cameras (entry) use a horizontal line at `LINE_Y_RATIO=0.45` of the frame height. Each track's bounding-box centre-y is compared frame-to-frame: a transition `prev_cy < line_y <= cy` emits ENTRY; the reverse emits EXIT (`detect.py:233`). A 2-second debounce per track prevents repeated crossings from one person hovering on the line.

Zone cameras maintain an `in_zone` set; first appearance emits `ZONE_ENTER`, then a `ZONE_DWELL` is emitted every 30s with cumulative `dwell_ms`, and absence for >3s emits `ZONE_EXIT`. Billing cameras additionally emit `BILLING_QUEUE_JOIN` (with current depth) and `BILLING_QUEUE_ABANDON` if the visitor leaves without a corresponding POS match.

Staff filtering is a placeholder heuristic: any track continuously present for >300s is reclassified as `is_staff=True` (`detect.py:214`). Real deployment would swap this for uniform/badge VLM detection — the field is a `bool` so the swap is local.

## Event Schema Design

`StoreEvent` (`app/models.py`) is the single contract between detection and API. Fields exist for these reasons:

- `event_id` — UUIDv4, validated by regex. The deduplication key. Without it, retries and pipeline restarts would double-count.
- `store_id`, `camera_id` — denormalised on every event so reads never need a join.
- `visitor_id` — see below; allows per-visitor session reconstruction without a DB lookup at write time.
- `event_type` — typed `Literal` so Pydantic rejects unknown event types at the boundary.
- `timestamp` — must be timezone-aware (`models.py:62`). Naive datetimes are rejected because every downstream window calculation is in UTC.
- `zone_id` — nullable; explicitly required to be `None` for ENTRY/EXIT (`models.py:66`) so analytical SQL can rely on the invariant.
- `dwell_ms` — recorded on dwell/exit events so the API doesn't have to recompute it from a window of events.
- `is_staff` — emitted, not filtered. Excluding staff during emit would lose the ability to retroactively recompute staff classification.
- `confidence` — emitted raw, never thresholded or rewritten. Suppression would silently bias all downstream metrics; the API and dashboard can choose to weight by confidence.
- `metadata.queue_depth` and `metadata.sku_zone` — type-specific; only meaningful for billing/zone events. Keeping them in `metadata` avoids polluting the top-level shape with always-null columns for events that don't use them.
- `metadata.session_seq` — a per-visitor monotonic counter. Lives in `metadata` because it's a property of the *session*, not the event itself, and not all detectors will compute it (offline replay can synthesise it from history).

## API Design

Routers live one-per-domain (`health`, `ingestion`, `metrics`, `funnel`, `heatmap`, `anomalies`) and are wired up in `app/main.py:198`. Each module owns its Pydantic response model, its SQL, and its constants — so a change to the metrics endpoint never touches the heatmap module.

SQLite was chosen for three concrete reasons: zero operational overhead (no separate process, no auth), file-level durability (the DB is a single file under `./data/`), and async compatibility via `aiosqlite`. For one store with bursty CCTV-derived writes, it's well below the contention point where a separate engine would help.

Idempotency is enforced at the database layer. `insert_event` (`app/db.py:152`) uses `INSERT OR IGNORE` keyed on `event_id` (the primary key). The pipeline can retry a failed batch without bookkeeping, and re-running detection on the same clip is a no-op. The endpoint returns `{accepted, duplicate, invalid, errors}` so the caller can distinguish "we've seen this" from "we rejected this".

Validation runs per-event: `_format_validation_error` (`app/ingestion.py:84`) catches Pydantic errors per item and continues, so one bad event in a batch of 500 doesn't abort the whole request.

## Storage Design

Two tables, both defined in `app/db.py`:

- **`events`** — append-only event log (PK `event_id`). Indexes:
  - `ix_events_store_ts (store_id, timestamp)` — every metrics/heatmap/anomaly query filters by store + time.
  - `ix_events_visitor_ts (visitor_id, timestamp)` — for per-visitor session reconstruction and dwell aggregation.
  - `ix_events_store_type_ts (store_id, event_type, timestamp)` — anomaly detectors filter by event_type (BILLING_QUEUE_JOIN, ZONE_ENTER, etc.).

- **`store_sessions`** — derived per-visit row (PK `session_id`). Holds entry/exit timestamps, conversion flag, re-entry flag. Indexes on `(store_id, entry_ts)` and `(visitor_id)`.

The two are deliberately separate because they have different write patterns and lifetimes. Events are immutable and append-only; sessions are *mutated* (a row is updated when EXIT arrives, and again when POS correlates a purchase). Mixing those concerns in one table would force every aggregation query to filter out partial rows. Keeping them apart also lets the conversion-rate calculation join cleanly: count distinct sessions today vs. count where `converted=1`.

## AI-Assisted Decisions

**1. Should the pipeline filter out staff before emitting events, or include and tag them?**
- *Claude suggested:* emit everything with an `is_staff` boolean, filter at the API. Reasoning: filtering at emit destroys data; if the staff heuristic is wrong, you can't recompute.
- *Decision:* kept the suggestion. The 300s heuristic is admittedly weak, and emitting the boolean lets us swap in a VLM uniform classifier later without re-processing footage.

**2. Should `visitor_id` be a database-issued auto-increment, or derived deterministically?**
- *Claude suggested:* deterministic MD5 of `(store_id, camera_id, track_id)`. Reasoning: makes ingestion idempotent without extra bookkeeping, and detection can run offline without round-tripping to the DB.
- *Decision:* adopted with one change. I added a `_track_to_visitor` cache in `SessionTracker` so the hash isn't recomputed every frame, and made the prefix `VIS_` so it's grep-able in logs.

**3. Should anomaly detectors run in parallel inside one request, or sequentially?**
- *Claude suggested:* sequentially within one DB connection, each wrapped in try/except. Reasoning: the detectors share a connection, and parallel execution on SQLite would serialise anyway; isolation matters more than concurrency.
- *Decision:* kept sequential, but added the per-detector exception logging in `app/anomalies.py:351` so a failure in one detector (e.g. conversion drop) doesn't break the others. The endpoint returns whatever succeeded.
