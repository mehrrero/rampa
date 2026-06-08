from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
import asyncio
import json
import logging
import math
import time

import duckdb
import geopandas as gpd
import pandas as pd
import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from src.network import Network
from src.tables import slugify_city

logger = logging.getLogger(__name__)


# Project-root config/config.yaml (this file lives in src/).
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
with open(CONFIG_PATH) as fh:
    cfg = yaml.safe_load(fh)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("App is starting…")
    # `network_cache` is module-level but defined further down — resolved at
    # call time, i.e. once the whole module (and the cache) is loaded.
    eviction_task = asyncio.create_task(network_cache._eviction_loop())
    try:
        yield
    finally:
        eviction_task.cancel()


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
DEFAULT_CITY = CITY_NAMES[0] if CITY_NAMES else None

# How long an unused city Network stays in memory before being released.
# Configurable via `api.network_idle_ttl_seconds`; any `/ruta` request for a
# city resets its timer, and `GET /health` (re)loads every configured city.
NETWORK_IDLE_TTL_S = cfg["api"].get("network_idle_ttl_seconds", 300)
# Check for idle networks at roughly half the TTL (so an expired entry is
# noticed within ~1.5x the TTL), capped to keep the loop responsive for short
# TTLs without polling needlessly often for long ones (e.g. the 300s default).
_EVICTION_INTERVAL_S = min(30.0, max(NETWORK_IDLE_TTL_S / 2.0, 1.0))


class NetworkCache:
    """Lazily builds per-city `Network`s and releases them after idle TTL.

    Building a `Network` means constructing its pandana routing graph(s) —
    real CPU/RAM work even with the binary CH cache from
    `scripts/initialize.py`. Loading every configured city eagerly at import
    (the previous behaviour) means an idle server holds all of them in memory
    forever. Instead, a city's `Network` is built on first use — or warmed by
    `GET /health`, which loads every configured city — and dropped again once
    it hasn't been touched for `ttl_seconds`; a background loop checks for
    expired entries every `_EVICTION_INTERVAL_S` seconds.
    """

    def __init__(self, db_path: str, city_names: list[str], ttl_seconds: float):
        self._db_path = db_path
        self._city_names = set(city_names)
        self._ttl = ttl_seconds
        self._networks: dict[str, Network] = {}
        self._last_access: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def get(self, city: str) -> Optional[Network]:
        """Return `city`'s Network — building it on first use — or `None` if
        `city` isn't configured. Resets the city's idle timer either way."""
        if city not in self._city_names:
            return None
        async with self._lock:
            net = self._networks.get(city)
            if net is None:
                logger.info("Loading network for city %r…", city)
                t0 = time.monotonic()
                loop = asyncio.get_running_loop()
                net = await loop.run_in_executor(None, Network, self._db_path, city)
                self._networks[city] = net
                logger.info(
                    "Loaded network for city %r in %.2fs (now in memory: %s)",
                    city, time.monotonic() - t0, sorted(self._networks),
                )
            self._last_access[city] = time.monotonic()
            return net

    async def warm_all(self) -> None:
        """Load (or refresh the timer on) every configured city's Network."""
        for name in self._city_names:
            await self.get(name)

    async def evict_idle(self) -> None:
        """Drop Networks that have sat unused longer than the idle TTL."""
        now = time.monotonic()
        async with self._lock:
            expired = [
                city for city, last in self._last_access.items()
                if now - last >= self._ttl
            ]
            for city in expired:
                self._networks.pop(city, None)
                self._last_access.pop(city, None)
                logger.info(
                    "Unloaded idle network for city %r (idle ≥ %.0fs; still in "
                    "memory: %s)",
                    city, self._ttl, sorted(self._networks),
                )

    async def _eviction_loop(self) -> None:
        while True:
            await asyncio.sleep(_EVICTION_INTERVAL_S)
            try:
                await self.evict_idle()
            except Exception:
                logger.exception("Network eviction pass failed")


network_cache = NetworkCache(DB_PATH, CITY_NAMES, NETWORK_IDLE_TTL_S)


async def _network_for_city(city: str) -> Network:
    """Resolve a `city` query value to its Network, or raise HTTP 404."""
    net = await network_cache.get(city)
    if net is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown city {city!r}; available: {sorted(CITY_NAMES)}",
        )

    net = networks.get(key)
    if net is None:
        net = Network(DB_PATH, city=resolved_city)
        networks[key] = net
    return net


# Travel speeds (m/s) used to estimate traversal time per route. Defaults keep
# older mounted Railway configs working if they predate the `api:` section.
TRAVEL_SPEEDS_MS = (cfg.get("api") or {}).get("travel_speeds") or {}
WALKING_SPEED_MS = TRAVEL_SPEEDS_MS.get("walking", 1.24)
WHEELCHAIR_SPEED_MS = TRAVEL_SPEEDS_MS.get("wheelchair", 0.7)


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
    """Liveness probe — also (re)warms every configured city's Network.

    Networks are loaded lazily and released after `network_idle_ttl_seconds`
    of inactivity (see `NetworkCache`); hitting `/health` builds any that
    aren't currently loaded and resets every city's idle timer, so a
    monitoring probe doubles as a way to keep the cache warm.
    """
    await network_cache.warm_all()
    return {"status": "ok"}


@app.get("/cities")
async def list_cities():
    """Configured city networks and the default used when `city` is omitted.

    Listing doesn't require loading a city's Network — that happens lazily on
    `/ruta` (or eagerly via `/health`).
    """
    return {"cities": sorted(CITY_NAMES), "default": DEFAULT_CITY}


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

    network = await _network_for_city(city)

    if mode not in ("alt", "veh_a"):
        raise HTTPException(
            status_code=400,
            detail=f"unknown mode {mode!r}; expected 'alt' or 'veh_a'.",
        )
    if mode == "veh_a" and not network.has_mode("veh_a"):
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
