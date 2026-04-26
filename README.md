# Store Intelligence — Apex Retail

Real-time retail analytics from CCTV footage. A FastAPI service ingests visitor-tracking events from a YOLOv8 + ByteTrack pipeline and exposes live metrics, conversion funnels, zone heatmaps, and anomaly detection.

## Architecture

Five-stage pipeline: **Footage → Detection → Events → API → Dashboard**

```
Footage/CAM_*.mp4
    ↓
YOLOv8n + ByteTrack (pipeline/detect.py)
    ↓  role-specific events (ENTRY/EXIT, ZONE_ENTER/DWELL/EXIT, BILLING_QUEUE)
JSONL + HTTP batch → /events/ingest
    ↓  idempotent INSERT OR IGNORE on event_id
SQLite (events + store_sessions)
    ↓
FastAPI (/metrics, /funnel, /heatmap, /anomalies)
    ↓
Rich TUI dashboard (dashboard/dashboard.py)
```

Detection runs YOLOv8n per frame and feeds bounding boxes into ByteTrack to maintain per-track identity within a camera. Role-specific logic (entry threshold crossings, zone dwell, billing queue depth) converts tracks into `StoreEvent`s. Each camera maps to a role defined in `dataset/store_layout.json` (ENTRY, FLOOR, BILLING, FLOOR_2, ENTRY_2). A visitor's identity across the store is a deterministic MD5 hash of `store_id + camera_id + track_id`, which makes ingestion idempotent on re-run.

Design rationale and schema reasoning are in [docs/DESIGN.md](docs/DESIGN.md). Engineering trade-offs (model choice, schema shape, storage engine) are in [docs/CHOICES.md](docs/CHOICES.md).

---

## Setup

### Docker (recommended)

```bash
git clone https://github.com/ashaheem32/Store_Intelligence
cd Purple
cp .env.example .env
docker compose up -d                              # starts API on :8000
docker compose --profile pipeline up pipeline    # runs detection pipeline
```

The API starts on port 8000. The pipeline service reads from `Footage/` and batch-POSTs events to the API.

### Local dev

```bash
cp .env.example .env
pip install -r requirements.txt
uvicorn app.main:app --reload    # API on :8000
```

Run the pipeline manually:

```bash
bash pipeline/run.sh
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `./data/store.db` | SQLite file path |
| `LOG_LEVEL` | `INFO` | Uvicorn log level |
| `STALE_FEED_MINUTES` | `10` | Minutes of inactivity before feed marked STALE |
| `RE_ENTRY_WINDOW_SECONDS` | `120` | Window to collapse a visitor re-entry |
| `STORE_LAYOUT_PATH` | `./dataset/store_layout.json` | Camera/zone config |
| `POS_DATA_PATH` | `./dataset/pos_transactions.csv` | POS transactions for conversion matching |
| `FOOTAGE_PATH` | `./Footage` | Input clips directory |
| `API_URL` | `http://localhost:8000` | API base URL used by the pipeline |

---

## API

Interactive docs: http://localhost:8000/docs

### POST `/events/ingest`

Accepts a batch of up to 500 `StoreEvent` objects. Validation is per-event — one invalid event does not abort the batch. Writes are idempotent (`INSERT OR IGNORE` on `event_id`).

```jsonc
// Request body (array)
[{
  "event_id": "a3e8f1c0-5b7d-4e2a-8f1b-2c9d7e3a1b4f",  // UUID v4
  "store_id": "STORE_001",
  "camera_id": "CAM_1",
  "visitor_id": "VIS_a1b2c3",
  "event_type": "ENTRY",  // ENTRY | EXIT | ZONE_ENTER | ZONE_EXIT | ZONE_DWELL |
                           // BILLING_QUEUE_JOIN | BILLING_QUEUE_ABANDON | REENTRY
  "timestamp": "2026-04-19T09:05:12Z",  // must be timezone-aware
  "zone_id": null,         // must be null for ENTRY/EXIT
  "dwell_ms": 0,
  "is_staff": false,
  "confidence": 0.94,      // [0.0, 1.0]
  "metadata": { "queue_depth": null, "sku_zone": null, "session_seq": 1 }
}]

// Response
{ "accepted": 1, "duplicate": 0, "invalid": 0, "errors": [] }
```

### GET `/health`

Returns API status and per-store feed freshness.

```jsonc
{
  "status": "OK",            // OK | DEGRADED
  "checked_at": "...",
  "database": "connected",
  "stores": {
    "STORE_001": { "last_event_at": "...", "lag_minutes": 2.1, "feed_status": "LIVE" }
    // feed_status: LIVE | STALE | CLOSED | NO_DATA
  }
}
```

### GET `/stores/{store_id}/metrics`

Today's aggregate metrics, non-staff visitors only.

```jsonc
{
  "store_id": "STORE_001",
  "window": "today",
  "as_of": "...",
  "unique_visitors": 42,
  "converted_visitors": 13,
  "conversion_rate": 0.31,          // null if no visitors
  "avg_dwell_seconds": { "MAIN_FLOOR": 45.3, "BILLING": 12.7 },
  "current_queue_depth": 3,
  "abandonment_rate": 0.15,         // null if no billing events
  "data_confidence": "HIGH"         // HIGH if ≥20 sessions today
}
```

### GET `/stores/{store_id}/funnel`

Session-unit conversion funnel with drop-off analysis.

```jsonc
{
  "store_id": "STORE_001",
  "window": "today",
  "stages": [
    { "stage": "ENTRY", "count": 42, "pct_of_entry": 1.0, "pct_from_prev": null },
    { "stage": "ZONE_VISIT", "count": 35, "pct_of_entry": 0.83, "pct_from_prev": 0.83 },
    { "stage": "BILLING_QUEUE", "count": 18, "pct_of_entry": 0.43, "pct_from_prev": 0.51 },
    { "stage": "PURCHASE", "count": 13, "pct_of_entry": 0.31, "pct_from_prev": 0.72 }
  ],
  "drop_off_analysis": { "biggest_drop_stage": "ZONE_VISIT", "drop_pct": 0.17 }
}
```

### GET `/stores/{store_id}/heatmap`

Zone visit frequency and dwell, normalised to 0–100. Every zone always appears even with zero activity.

```jsonc
{
  "zones": [
    { "zone_id": "MAIN_FLOOR", "label": "Main Floor", "visit_count": 31,
      "avg_dwell_seconds": 45.3, "normalised_score": 100, "data_confidence": "HIGH" },
    { "zone_id": "BILLING", "label": "Billing Area", "visit_count": 18,
      "avg_dwell_seconds": 12.7, "normalised_score": 58, "data_confidence": "HIGH" }
  ]
}
```

### GET `/stores/{store_id}/anomalies`

Active anomalies from four isolated detectors (a failing detector does not break the others).

```jsonc
{
  "active_anomalies": [
    {
      "anomaly_id": "...",
      "type": "BILLING_QUEUE_SPIKE",   // BILLING_QUEUE_SPIKE | CONVERSION_DROP |
                                        // DEAD_ZONE | STALE_CAMERA
      "severity": "CRITICAL",           // CRITICAL | WARN | INFO
      "detail": "Queue depth 11 exceeds critical threshold of 10",
      "suggested_action": "Open additional billing counter",
      "detected_at": "..."
    }
  ]
}
```

Anomaly thresholds:

| Detector | Trigger |
|---|---|
| `BILLING_QUEUE_SPIKE` | WARN at queue depth ≥ 5, CRITICAL at ≥ 10 |
| `CONVERSION_DROP` | Today's rate < 80% of 7-day average (min 10 sessions) |
| `DEAD_ZONE` | No `ZONE_ENTER` for 30+ min during open hours |
| `STALE_CAMERA` | No events from a camera for 10+ min |

---

## Live Dashboard

```bash
python dashboard/dashboard.py --store_id STORE_001
# --api_url http://localhost:8000  (default)
```

Rich TUI with four panels, polling the API asynchronously:

| Panel | Content | Refresh |
|---|---|---|
| Top bar | Store ID, time, feed status (LIVE / STALE / UNREACHABLE) | 10s |
| Metrics | Visitors, conversion rate, queue depth, abandonment rate | 3s |
| Heatmap | Zones sorted by normalised score (bar chart) | 5s |
| Anomalies | Severity-sorted list (CRITICAL / WARN / INFO) | 5s |

---

## Run Tests

```bash
make test
```

18 tests across three modules. Tests use a real SQLite DB per test (not mocks), hit the actual FastAPI endpoints via ASGITransport, and cover: zero-visitor edge cases, staff exclusion, idempotent ingestion, anomaly thresholds (WARN and CRITICAL), re-entry window logic, and the staff 300s heuristic.

---

## Store Layout

Configured in `dataset/store_layout.json`. Default: five cameras covering two entry points, two floor zones, and one billing area for `STORE_001` (Apex Retail, Asia/Kolkata, 09:00–21:00).

| Camera | Role | Zone |
|---|---|---|
| CAM_1 | ENTRY | ENTRY_ZONE (Main Entry) |
| CAM_2 | FLOOR | MAIN_FLOOR (Main Floor) |
| CAM_3 | BILLING | BILLING (Billing Area) |
| CAM_4 | FLOOR_2 | FLOOR_SOUTH (South Floor) |
| CAM_5 | ENTRY_2 | ENTRY_SOUTH (South Entry) |
