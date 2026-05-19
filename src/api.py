from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
import json
import math

import duckdb
import geopandas as gpd
import pandas as pd
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from src.network import Network


# Project-root config.yaml (this file lives in src/).
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
with open(CONFIG_PATH) as fh:
    cfg = yaml.safe_load(fh)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("App is starting…")
    yield


app = FastAPI(lifespan=lifespan)

# Permissive CORS for development; restrict `allow_origins` to known
# frontend origins before deploying. `allow_credentials=True` + "*" is
# rejected by browsers per the CORS spec, so credentials stay off here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

network = Network(cfg["initialize"]["paths"]["db_path"])


@app.get("/health")
async def healthcheck():
    return {"status": "ok"}


@app.get("/ruta")
async def create_route(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    max_snap_m: float = Query(
        500.0,
        description=(
            "Max distance (in meters) between an input coordinate and the "
            "nearest network node. Pass 0 or a negative value to disable "
            "the check (any snap distance allowed)."
        ),
    ),
):
    """Return the primary and accessibility-weighted routes between two points.

    Args:
        x1, y1: Origin coordinate in the network's CRS
            (currently EPSG:25830 / UTM 30N, meters).
        x2, y2: Destination coordinate in the same CRS.
        max_snap_m: Snap-distance cutoff in meters; non-positive disables it.

    Returns:
        dict: ``{"ruta": <GeoJSON>, "ruta_alt": <GeoJSON>}`` — the primary
        route (weighted by distance) and the alternate route (weighted by
        ``alt_distance``, which prefers accessible edges).

    Raises:
        HTTPException(404): if either endpoint is farther than ``max_snap_m``
            from any node, or the endpoints are in disconnected components.
    """
    coord1 = (x1, y1)
    coord2 = (x2, y2)
    snap = max_snap_m if max_snap_m > 0 else None

    ruta = network.route_gdf(coord1, coord2, alternate=False, max_snap_distance=snap)
    ruta_alt = network.route_gdf(coord1, coord2, alternate=True, max_snap_distance=snap)

    if ruta is None or ruta_alt is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No route found. Either an endpoint is too far from the "
                "network (snap > max_snap_m) or the endpoints are in "
                "disconnected components."
            ),
        )

    # `to_json` returns a string; round-trip through `loads` so FastAPI
    # serialises a real JSON object rather than a stringified one.
    return {
        "ruta": json.loads(ruta.to_json()),
        "ruta_alt": json.loads(ruta_alt.to_json()),
    }