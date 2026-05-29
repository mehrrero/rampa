#!/bin/sh
# Seed the mounted config folder on first start, then launch the API.
#
# The app reads a fixed path (/app/config.yaml). We keep the real file in the
# mounted /app/config volume so it survives the container and is editable on the
# host; /app/config.yaml is just a symlink onto it. If the volume has no
# config.yaml yet (first run / empty mount), copy in the baked-in default.
set -e

CONFIG_DIR="/app/config"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
DEFAULT_CONFIG="/app/config.default.yaml"

mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "No config.yaml in $CONFIG_DIR — seeding default."
    cp "$DEFAULT_CONFIG" "$CONFIG_FILE"
fi

# Point the app's expected path at the volume copy (live, so host edits apply).
ln -sf "$CONFIG_FILE" /app/config.yaml

exec "$@"
