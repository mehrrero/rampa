#!/bin/sh
# Seed the mounted config folder on first start, then launch the API.
#
# The app reads /app/config/config.yaml (resolved from the src/ tree). We keep
# the file in the mounted /app/config volume so it survives the container and
# is editable on the host. If the volume has no config.yaml yet (first run /
# empty mount), copy in the baked-in default.
set -e

CONFIG_DIR="/app/config"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
DEFAULT_CONFIG="/app/config.default.yaml"

mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "No config.yaml in $CONFIG_DIR — seeding default."
    cp "$DEFAULT_CONFIG" "$CONFIG_FILE"
fi

# ─── 2. Download data from T3 bucket ───────────────────────────────────
# Skips files that already exist in /app/data/ so the download is
# idempotent. network.duckdb is required (hard fail); graph.gpickle and
# mdt_lidar.tif are optional (API can serve routes without them — they
# are only needed for re-running initialize.py).
DATA_DIR="/app/data"
mkdir -p "$DATA_DIR"

if [ -n "${T3_KEY_ID:-}" ] && [ -n "${T3_KEY_SECRET:-}" ] && [ -n "${T3_BUCKET:-}" ]; then
    echo "T3 credentials detected — downloading network data..."
    python /app/scripts/t3_download.py
else
    echo "T3 credentials not set — will rely on pre-existing /app/data/ contents."
fi

# ─── 3. Launch the API ────────────────────────────────────────────────
# Railway sets $PORT dynamically; default to 8000 for local/Docker runs.
PORT="${PORT:-8000}"
echo "Starting uvicorn on 0.0.0.0:$PORT …"
exec uvicorn src.api:app --host 0.0.0.0 --port "$PORT"
