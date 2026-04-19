#!/usr/bin/env bash
# pipeline/run.sh — process all 5 CCTV clips, merge events, POST to API.
#
# Camera → clip mapping is hardcoded per dataset/store_layout.json.
# Runs sequentially (no parallelism) to avoid GPU memory pressure on Mac.
#
# Usage:
#   bash pipeline/run.sh
# Optional env vars:
#   API_URL   (default: http://localhost:8000)
#   LAYOUT    (default: dataset/store_layout.json)
#   PYTHON    (default: python)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PYTHON_BIN="${PYTHON:-python}"
API_URL="${API_URL:-http://localhost:8000}"
LAYOUT="${LAYOUT:-dataset/store_layout.json}"
OUT_DIR="output"
LOG_FILE="pipeline.log"
MERGED="${OUT_DIR}/all_events.jsonl"

# Ensure local package imports work regardless of install state.
export PYTHONPATH="${PROJECT_ROOT}:${PYTHONPATH:-}"

echo "[run.sh] project root: ${PROJECT_ROOT}"
echo "[run.sh] python:       ${PYTHON_BIN}"
echo "[run.sh] api url:      ${API_URL}"
echo "[run.sh] layout:       ${LAYOUT}"
echo "[run.sh] output dir:   ${OUT_DIR}"
echo "[run.sh] log file:     ${LOG_FILE}"
echo

# Hardcoded camera → clip mapping (per store_layout.json).
CAMERAS=(CAM_1 CAM_2 CAM_3 CAM_4 CAM_5)
CLIPS=(
  "Footage/CAM 1.mp4"
  "Footage/CAM 2.mp4"
  "Footage/CAM 3.mp4"
  "Footage/CAM 4.mp4"
  "Footage/CAM 5.mp4"
)

mkdir -p "${OUT_DIR}"
: > "${LOG_FILE}"

# ---------- [1/7] clip file check ----------
echo "[1/7] checking clip files..."
MISSING_COUNT=0
for i in "${!CAMERAS[@]}"; do
  clip="${CLIPS[$i]}"
  if [[ ! -f "${clip}" ]]; then
    echo "  WARN: missing clip: ${clip}"
    MISSING_COUNT=$((MISSING_COUNT + 1))
  else
    size=$(stat -f%z "${clip}" 2>/dev/null || stat -c%s "${clip}" 2>/dev/null || echo "?")
    echo "  ok:   ${clip} (${size} bytes)"
  fi
done
echo "  ${MISSING_COUNT} missing of ${#CAMERAS[@]}"
echo

# ---------- [2/7] detection per clip ----------
echo "[2/7] running detection per clip (sequential)..."
T_PIPE_START=$(date +%s)
PROCESSED=0
FAILED=0

for i in "${!CAMERAS[@]}"; do
  cam="${CAMERAS[$i]}"
  clip="${CLIPS[$i]}"
  out_file="${OUT_DIR}/events_${cam}.jsonl"

  if [[ ! -f "${clip}" ]]; then
    echo "  skip ${cam}: clip missing"
    continue
  fi

  : > "${out_file}"
  echo "  ---- ${cam} ----"
  echo "  clip: ${clip}"
  echo "  out:  ${out_file}"

  if "${PYTHON_BIN}" pipeline/detect.py \
        --clip "${clip}" \
        --camera_id "${cam}" \
        --layout "${LAYOUT}" \
        --out "${out_file}" 2>> "${LOG_FILE}"; then
    PROCESSED=$((PROCESSED + 1))
    echo "  ok: ${cam}"
  else
    FAILED=$((FAILED + 1))
    echo "  FAIL: ${cam} — see ${LOG_FILE}"
  fi
  echo
done

T_PIPE_END=$(date +%s)
PIPE_DUR=$((T_PIPE_END - T_PIPE_START))
echo "  processed: ${PROCESSED} / ${#CAMERAS[@]}   failed: ${FAILED}   duration: ${PIPE_DUR}s"
echo

# ---------- [3/7] merge ----------
echo "[3/7] merging event files..."
: > "${MERGED}"
shopt -s nullglob
for f in "${OUT_DIR}"/events_CAM_*.jsonl; do
  [[ "${f}" == "${MERGED}" ]] && continue
  cat "${f}" >> "${MERGED}"
done
shopt -u nullglob
echo "  merged → ${MERGED}"
echo

# ---------- [4/7] count ----------
echo "[4/7] counting events..."
if [[ -f "${MERGED}" ]]; then
  TOTAL=$(wc -l < "${MERGED}" | tr -d ' ')
else
  TOTAL=0
fi
echo "  total: ${TOTAL} events"
echo

# ---------- [5/7] api reachability ----------
echo "[5/7] checking API reachability..."
API_UP=0
if API_URL="${API_URL}" "${PYTHON_BIN}" - <<'PY' 2>/dev/null
import os, sys
try:
    import httpx
    r = httpx.get(os.environ["API_URL"].rstrip("/") + "/health", timeout=3)
    sys.exit(0 if r.status_code < 500 else 1)
except Exception:
    sys.exit(1)
PY
then
  API_UP=1
  echo "  api ok: ${API_URL}"
else
  echo "  WARN: API not reachable at ${API_URL} — skipping ingest (events still on disk)"
fi
echo

# ---------- [6/7] ingest in batches of 500 ----------
INGEST_STATUS="skipped"
if [[ ${API_UP} -eq 1 && ${TOTAL} -gt 0 ]]; then
  echo "[6/7] posting events in batches of 500..."
  if API_URL="${API_URL}" MERGED="${MERGED}" "${PYTHON_BIN}" - <<'PY' 2>&1 | tee -a "${LOG_FILE}"
import json, os, sys
import httpx

api_url = os.environ["API_URL"].rstrip("/")
path = os.environ["MERGED"]

with open(path) as f:
    events = [json.loads(line) for line in f if line.strip()]

batches = [events[i:i + 500] for i in range(0, len(events), 500)]
total_accepted = 0
for i, batch in enumerate(batches):
    try:
        r = httpx.post(f"{api_url}/events/ingest", json=batch, timeout=30)
        try:
            accepted = r.json().get("accepted", 0)
        except Exception:
            accepted = 0
        total_accepted += accepted
        print(f"Batch {i+1}/{len(batches)}: {r.status_code} — {accepted} accepted")
    except Exception as e:
        print(f"Batch {i+1}/{len(batches)}: ERROR — {e}")

print(f"TOTAL accepted: {total_accepted} / {len(events)}")
PY
  then
    INGEST_STATUS="ok"
  else
    INGEST_STATUS="error"
  fi
  echo
else
  echo "[6/7] skipping ingest (api_up=${API_UP}, total=${TOTAL})"
  echo
fi

# ---------- [7/7] summary ----------
echo "[7/7] summary"
echo "============================================================"
echo "Pipeline duration: ${PIPE_DUR}s"
echo "Clips processed:   ${PROCESSED} / ${#CAMERAS[@]}"
echo "Clip failures:     ${FAILED}"
echo "Total events:      ${TOTAL}"
echo "API ingest:        ${INGEST_STATUS}  (${API_URL})"
echo
echo "Events per camera:"
for cam in "${CAMERAS[@]}"; do
  f="${OUT_DIR}/events_${cam}.jsonl"
  if [[ -f "${f}" ]]; then
    n=$(wc -l < "${f}" | tr -d ' ')
    printf "  %-8s %s\n" "${cam}" "${n}"
  else
    printf "  %-8s %s\n" "${cam}" "(no output)"
  fi
done
echo

if [[ ${TOTAL} -gt 0 ]]; then
  echo "Events by type:"
  MERGED="${MERGED}" "${PYTHON_BIN}" - <<'PY'
import json, os
from collections import Counter
path = os.environ["MERGED"]
c = Counter()
with open(path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            c[json.loads(line).get("event_type", "?")] += 1
        except Exception:
            pass
for k, v in c.most_common():
    print(f"  {k:<24} {v}")
PY
fi

echo
echo "done. log: ${LOG_FILE}"
exit 0
