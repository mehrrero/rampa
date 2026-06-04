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


# Project-root config/config.yaml (this file lives in src/).
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
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

DB_PATH = cfg["initialize"]["paths"]["db_path"]
CITY_NAMES = [c["name"] for c in cfg["initialize"].get("cities", [])]

# One Network per configured city, built eagerly at import. Each reads its own
# `nodes_<city>` / `edges_<city>` tables produced by scripts/initialize.py.
# The first city in config is the default when a request omits `city`.
networks: dict[str, Network] = {
    name: Network(DB_PATH, city=name) for name in CITY_NAMES
}
DEFAULT_CITY = CITY_NAMES[0] if CITY_NAMES else None


def _network_for_city(city: str) -> Network:
    """Resolve a `city` query value to its Network, or raise HTTP 404."""
    net = networks.get(city)
    if net is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown city {city!r}; available: {sorted(networks)}",
        )
    return net


# Travel speeds (m/s) used to estimate traversal time per route.
TRAVEL_SPEEDS_MS = cfg["api"]["travel_speeds"]
WALKING_SPEED_MS = TRAVEL_SPEEDS_MS["walking"]
WHEELCHAIR_SPEED_MS = TRAVEL_SPEEDS_MS["wheelchair"]


def _format_duration(seconds: float) -> str:
    """Format a duration as ``"X h Y min"``, or ``"Y min"`` when X is 0."""
    total_minutes = round(seconds / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def _route_metadata(ruta) -> dict:
    """Per-route summary: total length plus total ascent/descent in metres.

    Ascent/descent are derived from the per-edge ``d_elev`` (signed climb,
    ``z_to - z_from``, already oriented in travel direction). They are only
    included when the elevation step has populated ``d_elev``; NaN edges (no
    geometry / all-nodata DEM samples) are dropped so a single gap doesn't
    void the whole total.

    Traversal time is estimated from the total length at fixed walking and
    wheelchair speeds, formatted as ``"X h Y min"`` (or ``"Y min"``).
    """
    total_length = float(ruta["distance"].sum())
    meta = {
        "total_length": total_length,
        "walking_time": _format_duration(total_length / WALKING_SPEED_MS),
        "wheelchair_time": _format_duration(total_length / WHEELCHAIR_SPEED_MS),
    }
    if "d_elev" in ruta.columns:
        d_elev = ruta["d_elev"].dropna()
        meta["total_ascent"] = float(d_elev[d_elev > 0].sum())
        meta["total_descent"] = float(-d_elev[d_elev < 0].sum())
    return meta


@app.get("/health")
async def healthcheck():
    return {"status": "ok"}


@app.get("/cities")
async def list_cities():
    """Available city networks and the default used when `city` is omitted."""
    return {"cities": sorted(networks), "default": DEFAULT_CITY}


@app.get("/ruta")
async def create_route(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    city: str = Query(
        DEFAULT_CITY,
        description=(
            "Which city's network to route on. Defaults to the first city in "
            "config; see GET /cities for the available names."
        ),
        examples=["valencia", "madrid"],
    ),
    mode: str = Query(
        "alt",
        description=(
            "Which accessibility route to return alongside the primary "
            "(distance) route: 'alt' (accessibility-weighted) or 'veh_a' "
            "(type-A personal-mobility-vehicle lanes)."
        ),
    ),
    max_snap_m: float = Query(
        500.0,
        description=(
            "Max distance (in meters) between an input coordinate and the "
            "nearest network node. Pass 0 or a negative value to disable "
            "the check (any snap distance allowed)."
        ),
    ),
):
    """Return the primary route plus one accessibility-weighted route.

    Coordinates are in the network CRS (EPSG:25830 / UTM 30N, meters), so they
    differ per city. Example queries:

    - Valencia (default city)::

        GET /ruta?city=valencia&x1=726538.273048&y1=4369685.099138
                 &x2=725471.941018&y2=4371464.411097

    - Madrid::

        GET /ruta?city=madrid&x1=441936.50143&y1=4474738.053088
                 &x2=438726.727451&y2=4474983.775073

    Args:
        x1, y1: Origin coordinate in the network's CRS
            (currently EPSG:25830 / UTM 30N, meters).
        x2, y2: Destination coordinate in the same CRS.
        city: Which city's network to route on (see ``GET /cities``); defaults
            to the first configured city.
        mode: Which secondary route to compute — ``"alt"`` (accessibility,
            ``alt_distance``) or ``"veh_a"`` (type-A PMV lanes,
            ``veh_a_distance``).
        max_snap_m: Snap-distance cutoff in meters; non-positive disables it.

    Returns:
        dict: ``{"ruta": <GeoJSON>, "ruta_alt"|"ruta_veh_a": <GeoJSON>,
        "mode": <str>, "metadata": {...}}`` — the primary route (weighted by
        distance) and the requested accessibility route, keyed by the selected
        mode. Per-route metadata includes each route's total length in meters,
        estimated walking and wheelchair traversal times, and — when elevation
        data is present — its total ascent and descent in meters.

    Raises:
        HTTPException(400): if ``mode`` is unknown, or ``"veh_a"`` is requested
            but the graph has no ``veh_a_distance`` column.
        HTTPException(404): if ``city`` is unknown, either endpoint is farther
            than ``max_snap_m`` from any node, or the endpoints are in
            disconnected components.
    """
    coord1 = (x1, y1)
    coord2 = (x2, y2)
    snap = max_snap_m if max_snap_m > 0 else None

    network = _network_for_city(city)

    if mode not in ("alt", "veh_a"):
        raise HTTPException(
            status_code=400,
            detail=f"unknown mode {mode!r}; expected 'alt' or 'veh_a'.",
        )
    if mode == "veh_a" and network.veh_a_network is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "mode 'veh_a' unavailable: the graph has no veh_a_distance "
                "column (run the Valencia + accessibility steps)."
            ),
        )

    ruta = network.route_gdf(coord1, coord2, mode="distance", max_snap_distance=snap)
    ruta_alt = network.route_gdf(coord1, coord2, mode=mode, max_snap_distance=snap)

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
    # serialises a real JSON object rather than a stringified one. The
    # secondary route is keyed by its mode (`ruta_alt` / `ruta_veh_a`).
    alt_key = f"ruta_{mode}"
    return {
        "ruta": json.loads(ruta.to_json()),
        alt_key: json.loads(ruta_alt.to_json()),
        "city": city,
        "mode": mode,
        "metadata": {
            "ruta": _route_metadata(ruta),
            alt_key: _route_metadata(ruta_alt),
        },
    }