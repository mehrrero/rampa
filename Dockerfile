# API image for the `rampa` sidewalk-routing service.
#
# Only the application code lives in the image. The runtime inputs — a `config/`
# folder and the `data/` directory (network.duckdb, DEM, graphs) — are NOT baked
# in; mount them as volumes at run time:
#
#   docker build -t rampa-api .
#   docker run --rm -p 8000:8000 \
#     -v "$PWD/config:/app/config" \
#     -v "$PWD/data:/app/data:ro" \
#     rampa-api
#
# On startup the entrypoint seeds config/config.yaml from a baked-in default if
# the folder has none. The app reads /app/config/config.yaml (src/api.py); the DB
# path inside it ("data/network.duckdb") is relative to the workdir, so WORKDIR is
# /app and the data mount must land there.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# pandana's wheel links against OpenMP at runtime; the slim image lacks it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# uv settings: install into a project-local venv from the frozen lockfile,
# byte-compile for faster cold starts, and copy (don't symlink) from the cache.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Resolve dependencies first (cached unless pyproject/uv.lock change), so code
# edits don't bust the dependency layer. --no-install-project: the app isn't a
# package to install, just a source tree we copy in below.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# boto3 for T3 (Tigris) bucket downloads on Railway startup — a deployment-only
# dependency, kept out of pyproject/uv.lock.
RUN uv pip install boto3

# Application source only — the config folder and data/ are mounted at runtime.
COPY src ./src
COPY scripts ./scripts

# Baked-in default config, copied into the config volume on first start by the
# entrypoint if the volume has none. Not the live config — that lives in the
# mounted /app/config folder.
COPY config/config.yaml ./config.default.yaml
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
