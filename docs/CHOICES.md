# Engineering Choices

Three load-bearing decisions, the alternatives considered, and what AI-assisted reasoning shaped the final pick.

---

## CHOICE 1 — Detection Model: YOLOv8n

**Options considered**

| Option | Accuracy (COCO mAP) | Speed (Mac M-series CPU) | Notes |
|---|---|---|---|
| YOLOv8n (nano) | 37.3 | ~15–25 fps | Smallest weights (~6 MB), CPU-friendly |
| YOLOv8m (medium) | 50.2 | ~3–6 fps | Better at occluded/distant people, ~50 MB weights |
| RT-DETR-L | 53.0 | ~2–4 fps | Transformer-based, no NMS; great accuracy but heavy |
| MediaPipe Pose | n/a (single-person bias) | ~30+ fps | Built for single-person pose; weak at multi-person tracking |

**Trade-offs**

The decision space collapses around a single constraint: this runs on a Mac with no CUDA GPU, on five concurrent CCTV feeds. YOLOv8m is more accurate but its frame rate on CPU drops below the rate at which ByteTrack can maintain stable identity. RT-DETR has the best accuracy but is the slowest of the four and has no real CPU path. MediaPipe is fast but optimised for a single foregrounded subject — it falls apart on a busy retail floor with overlapping people.

**What AI suggested**

Claude's read was: "for a 48-hour take-home with no GPU and a goal of demonstrating end-to-end behaviour rather than maximising precision, YOLOv8n is the right floor. The retail use case mostly cares about presence/absence and crossing detection, not fine-grained classification — so the accuracy gap to YOLOv8m doesn't translate into a metric the dashboard cares about."

**What I chose and why**

YOLOv8n with `conf=0.3`. The threshold is set deliberately low: I'd rather emit a borderline detection with `confidence=0.32` and let the API/dashboard decide what to do with it than suppress it. A `LOW_CONF_WARN=0.4` threshold logs (but doesn't drop) low-confidence events so the operator can audit later. If this were going to a real store with a GPU box, I'd swap to YOLOv8m and re-test the BILLING crossing detector first, since that's the most accuracy-sensitive spot.

---

## CHOICE 2 — Event Schema Design

The schema is the single most load-bearing artefact in the system: every other component has to either produce it or consume it. Three sub-decisions deserve calling out.

**Why `visitor_id` is a deterministic hash, not an auto-increment DB ID**

Auto-increment requires the detector to round-trip to the DB *before* it can emit an event with a stable identity. That's a synchronous coupling that breaks offline replay (write to JSONL, ingest later) and breaks idempotency on retry (the same physical track gets two different IDs across runs). The hash `MD5(store_id || camera_id || track_id)` is computed locally, deterministically, and survives detection restarts on the same clip — re-running detection produces the same `visitor_id`s, which combined with `INSERT OR IGNORE` makes the whole pipeline truly idempotent.

The trade-off: the same physical person on CAM_1 and CAM_2 gets two different `visitor_id`s. That's documented as a known limitation in `pipeline/tracker.py:9`, and a future cross-camera Re-ID step would unify them upstream.

**Why `confidence` is emitted raw and never suppressed**

The detector knows the confidence; the API doesn't. If the detector silently drops anything below 0.3, every downstream metric is biased in a way that no reader can recover. By emitting raw confidence, the API can still choose to filter at query time, the dashboard can colour-code uncertainty, and a future calibration pass can re-weight events. The cost is a few extra rows in SQLite — trivial compared to losing the ability to audit.

**Why `session_seq` lives in `metadata`, not as a top-level field**

`session_seq` is a property of the visitor's *session*, not the event itself. Some emitters (offline replay from a JSONL file) won't compute it; some (live detection with a `SessionTracker`) will. Putting it at the top level forces every emitter to either compute it or send `null`, which is noise. Inside `metadata` it's optional-by-convention and the schema stays honest about which fields are universal vs. emitter-specific. The same logic applies to `queue_depth` (only meaningful for billing events) and `sku_zone`.

**What AI suggested about schema, what I kept, what I changed**

Claude proposed making `metadata` a flexible `dict[str, Any]` for forward extensibility. I rejected that — typing `metadata` as a closed Pydantic model (`EventMetadata`) means a typo in `queue_depth` is caught at ingest, not at the dashboard rendering step. The API also proposed a `revoked_at` field for soft-deleting events; I dropped it because nothing in the current system needs it, and adding it now would be a feature without a caller.

---

## CHOICE 3 — SQLite as Storage Engine

**Options considered**

- **PostgreSQL** — full RDBMS, concurrent writers, time-series extensions (TimescaleDB).
- **Redis** — in-memory, perfect for live queue-depth counters, but loses the analytical query story.
- **SQLite** — single file, zero-ops, async-capable via aiosqlite.
- **ClickHouse** — purpose-built for event analytics, excellent at the queries this system runs.

**Why SQLite is the right choice for a 48-hour take-home with one store**

Three reasons. (1) Zero operational overhead — no separate process, no auth, no Docker network plumbing; the DB is a file in `./data/` and the API holds a single async engine. (2) The write rate is small and bursty — five cameras × ~1 event/second/camera worst case, well below where SQLite contention starts to matter. (3) Every query in the system is "filter by `store_id` + time window, group by something" — a pattern that SQLite's btree indexes serve perfectly well at this volume. The `ix_events_store_ts` and `ix_events_store_type_ts` indexes (defined in `app/db.py:55`) cover every read path.

**What AI suggested**

Claude's framing: "the question is whether the storage choice is on the critical path of the demo. If it isn't, picking the boring/zero-ops option frees attention for the parts that *are* on the critical path — the detection pipeline, the API contract, and the dashboard." That matched my read. I also asked specifically about ClickHouse, since this is fundamentally a time-series-events workload — Claude's response was that ClickHouse would be the right answer at scale (multiple stores, weeks of retained data) but is overkill for one store in a take-home, and the operational learning curve isn't free.

**What I chose, and when I'd change it**

SQLite. I'd swap to PostgreSQL the moment a second concurrent writer (e.g. a second pipeline instance, or a real-time event consumer) starts contending for the file lock. I'd swap to ClickHouse the moment we add multi-store rollups or multi-week retention — at that point the columnar storage + `MergeTree` indexing dominate. Until then, the boring choice is the correct choice.
