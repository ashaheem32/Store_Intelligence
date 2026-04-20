# Store Intelligence — Apex Retail

Real-time retail analytics from CCTV footage. A FastAPI service ingests visitor-tracking events from a YOLOv8 + ByteTrack pipeline and exposes live metrics, conversion funnels, zone heatmaps, and anomaly detection.

## Setup (5 commands)

```bash
git clone <https://github.com/ashaheem32/Store_Intelligence.git>
cd Purple
cp .env.example .env
docker compose up -d
docker compose --profile pipeline up pipeline
```

The first four commands clone the repo, seed environment variables, and start the API on port 8000 in the background. The fifth runs the detection pipeline against the bundled `Footage/` clips, POSTing events to the live API.

## API Docs

http://localhost:8000/docs

## Run Tests

```bash
make test
```

## Live Dashboard

```bash
python dashboard/dashboard.py --store_id STORE_001
```

Terminal dashboard (rich) showing live metrics, zone heatmap, and active anomalies. Polls the API at `http://localhost:8000` by default — override with `--api_url`.

## Detection Pipeline (manual)

```bash
bash pipeline/run.sh
```

Runs YOLOv8n + ByteTrack across all five `Footage/CAM_*.mp4` clips, emitting `StoreEvent`s to `output/events_*.jsonl` and (if `--api_url` is set) batch-POSTing them to `/events/ingest`.

## Architecture

Four-stage pipeline: **Footage → Detection → Events → API → Dashboard**. Detection runs YOLOv8n per frame and feeds boxes into ByteTrack to maintain identities; role-specific logic (entry threshold crossings, zone enter/dwell/exit, billing queue depth) becomes a `StoreEvent`. Events land in SQLite via an idempotent `INSERT OR IGNORE` keyed on `event_id`. The API serves derived metrics, anomalies, and a heatmap. Full design rationale, schema reasoning, and AI-assisted decisions are in [docs/DESIGN.md](docs/DESIGN.md); the major engineering trade-offs (model choice, schema shape, storage engine) are in [docs/CHOICES.md](docs/CHOICES.md).
