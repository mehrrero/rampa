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

exec "$@"
