"""Load a pickled OSMnx graph and persist it to DuckDB.

Reads its configuration from the project-root `config.yaml`, under the
`initialize:` section. The resulting `nodes` and `edges` tables match
the schema expected by `src.network.Network.__get_network` on reload.
"""

from __future__ import annotations

import logging
import math
import pickle
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml
from shapely.geometry import LineString

from src.tables import city_tables, slugify_city

logger = logging.getLogger(__name__)

# Default config path: project-root config/config.yaml (this file lives in scripts/).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


#############################################
#############################################

def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load YAML config from disk."""
    with open(path, "r") as fh:
        return yaml.safe_load(fh) or {}


##############################################
##############################################


def load_graph(
    graph_path: str | Path,
    db_path: str | Path,
    attributes: list[str] | None = None,
    nodes_table: str = "nodes",
    edges_table: str = "edges",
) -> None:
    """Read a pickled (tile2net-style) graph and write nodes/edges to DuckDB.

    The output schema mirrors what `Network.__get_network` expects on reload:
    `nodes(index, x, y)` and `edges(from, to, distance, alt_distance, ...)`.
    Any names in `attributes` are copied from the edge data dict onto the
    edges table (e.g. ``width``). ``nodes_table`` / ``edges_table`` select the
    destination tables — per-city builds pass ``nodes_<city>`` / ``edges_<city>``.
    """
    attributes = list(attributes or [])
    graph_path = Path(graph_path)

    # Pickle errors come in a few flavours (missing file, truncated file,
    # unreadable payload). Catch them together so the script just logs and
    # exits instead of crashing on the load.
    try:
        with graph_path.open("rb") as f:
            graph = pickle.load(f)
    except (FileNotFoundError, EOFError, pickle.UnpicklingError) as e:
        logger.error("Could not load graph from %s: %s", graph_path, e)
        return

    logger.info(
        "Loaded %s: %d nodes, %d edges (directed=%s)",
        type(graph).__name__,
        graph.number_of_nodes(),
        graph.number_of_edges(),
        graph.is_directed(),
    )

    # tile2net graphs use (x, y) coordinate tuples. Remap each
    # tuple id to a sequential integer; the tuple itself doubles as the
    # node's x/y.
    id_map: dict[Any, int] = {node: i for i, node in enumerate(graph.nodes)}

    nodes = pd.DataFrame(
        {"x": [n[0] for n in id_map], "y": [n[1] for n in id_map]},
        index=pd.Index(list(id_map.values()), name="index"),
    )

    # Edges: remap endpoints, pull `length` as `distance`, persist the
    # source geometry as WKB so visualisation later can use the real
    # sidewalk shape (not a straight segment). Pandana edges are
    # directed, so mirror each edge when the source graph is undirected.
    rows: list[dict[str, Any]] = []
    for u, v, _key, data in graph.edges(keys=True, data=True):
        geom = data.get("geometry")
        row: dict[str, Any] = {
            "from": id_map[u],
            "to": id_map[v],
            "distance": float(data["length"]),
            "geometry": geom.wkb if geom is not None else None,
        }
        for attr in attributes:
            row[attr] = data.get(attr)
        rows.append(row)

        if not graph.is_directed():
            mirror = dict(row)
            mirror["from"], mirror["to"] = row["to"], row["from"]
            # Reverse the coord order so the LineString still flows
            # from the new `from` to the new `to`.
            if geom is not None:
                mirror["geometry"] = LineString(list(geom.coords)[::-1]).wkb
            rows.append(mirror)

    edges = pd.DataFrame(rows)

    nodes = nodes.astype({"x": "float64", "y": "float64"})
    edges = edges.astype({
        "from": "int64",
        "to": "int64",
        "distance": "float64",
    })

    con = duckdb.connect(str(db_path))
    try:
        # reset_index materialises the node id as a column named 'index',
        # which is what __get_network's set_index('index') expects.
        con.register("df_nodes", nodes.reset_index(drop=False))
        con.register("df_edges", edges)
        con.execute(f'CREATE OR REPLACE TABLE "{nodes_table}" AS SELECT * FROM df_nodes')
        con.execute(f'CREATE OR REPLACE TABLE "{edges_table}" AS SELECT * FROM df_edges')
        logger.info(
            "Stored %d nodes (%s) and %d edges (%s) → %s",
            len(nodes), nodes_table, len(edges), edges_table, db_path,
        )
    finally:
        con.close()

def fetch_dem(
    out_path: str | Path,
    bbox: tuple[float, float, float, float],
    bbox_crs: str,
    wcs_url: str = "https://servicios.idee.es/wcs-inspire/mdt",
    coverage_id: str = "Elevacion4258_5",
    coverage_crs: str = "EPSG:4258",
    pad_m: float = 50.0,
    timeout: float = 300.0,
) -> Path:
    """Download a DEM raster covering ``bbox`` from a WCS endpoint.

    Steps:
        1. Pad the bbox by ``pad_m`` metres so edges near the border still have
           valid samples after CRS-projection rounding, then transform from
           ``bbox_crs`` to the coverage's native CRS.
        2. If a non-empty file already exists at ``out_path``, open it and skip
           the fetch only when its bounds actually enclose the requested bbox —
           a stale cache from a different city is otherwise easy to miss.
        3. Plan a tile grid: the IDEE WCS caps each response at 4096 px per
           side, so divide the bbox into sub-tiles small enough to fit. The
           coverage's native resolution (``_5`` → 5 m, ``_25`` → 25 m, …) is
           parsed from the coverage id.
        4. Issue one WCS 2.0.1 ``GetCoverage`` per tile. If the plan is 1×1
           write the response body straight to ``out_path``; otherwise write
           each tile to a temp file and mosaic them with ``rasterio.merge``.

    Default coverage is `Elevacion4258_5` (5 m PNOA-LiDAR-derived MDT) on the
    IDEE INSPIRE WCS, but every relevant param is exposed so other coverages
    or endpoints can be configured via ``config.yaml``.

    The request is built directly with `requests` because owslib (0.35)
    URL-encodes the identifier list as its Python repr, which the server 404s
    on. The same library bug means we also can't rely on its CRS / axis-label
    handling here.
    """
    # Lazy imports — these deps are only needed when the elevation step runs.
    import math
    import re
    import tempfile

    import rasterio
    import requests
    from rasterio.errors import RasterioIOError
    from rasterio.merge import merge as rio_merge
    from rasterio.warp import transform_bounds

    out_path = Path(out_path)

    # ─── 1. bbox: pad in graph CRS, then project to the coverage's CRS ──────
    # Done before the cache check so we can verify the cached raster
    # actually covers what we need (a stale cache from a different city
    # otherwise silently feeds compute_elevation, which then produces zeros).
    minx, miny, maxx, maxy = bbox
    minx -= pad_m
    miny -= pad_m
    maxx += pad_m
    maxy += pad_m
    west, south, east, north = transform_bounds(
        bbox_crs, coverage_crs, minx, miny, maxx, maxy
    )
    logger.info(
        "Fetch bbox: %s [pad=%.0fm] → %s lon[%.5f..%.5f] lat[%.5f..%.5f]",
        bbox_crs, pad_m, coverage_crs, west, east, south, north,
    )

    # ─── 2. cache check ─────────────────────────────────────────────────────
    # Existence is necessary but not sufficient — verify the cached raster's
    # bounds enclose the requested bbox. Otherwise we re-fetch.
    if out_path.exists() and out_path.stat().st_size > 0:
        try:
            with rasterio.open(out_path) as src:
                b = src.bounds
                src_crs = str(src.crs) if src.crs else None
                if (
                    src_crs is not None
                    and src_crs.replace("EPSG:", "") != coverage_crs.replace("EPSG:", "")
                ):
                    c_west, c_south, c_east, c_north = transform_bounds(
                        src_crs, coverage_crs, b.left, b.bottom, b.right, b.top
                    )
                else:
                    c_west, c_south, c_east, c_north = b.left, b.bottom, b.right, b.top
            covers = (
                c_west <= west and c_east >= east
                and c_south <= south and c_north >= north
            )
            if covers:
                logger.info("DEM cache covers requested bbox, skipping fetch: %s", out_path)
                return out_path
            logger.info(
                "Cached DEM at %s doesn't cover requested bbox "
                "(cache lon[%.5f..%.5f] lat[%.5f..%.5f]) — re-fetching.",
                out_path, c_west, c_east, c_south, c_north,
            )
        except RasterioIOError as e:
            logger.warning("Cached DEM at %s unreadable (%s) — re-fetching.", out_path, e)

    # ─── 3. tile plan — server caps each request at 4096 px per side ────────
    # Coverage ids on this WCS end in the native resolution in metres
    # (`Elevacion4258_5` → 5 m, `_25` → 25 m). If the full bbox would
    # exceed MAX_TILE_PX on either axis, split into a grid and mosaic.
    res_match = re.search(r"_(\d+)$", coverage_id)
    res_m = float(res_match.group(1)) if res_match else 5.0
    MAX_TILE_PX = 3800  # margin under the server's MAXSIZE=4096
    # IDEE's EPSG:4258 coverage lays pixels on an isotropic *degree* grid —
    # the server does NOT scale lon by cos(lat). Use the lat factor on both
    # axes or we under-count width and oversize tiles past MAXSIZE=4096.
    deg_per_px = res_m / 111_320.0
    h_px = (north - south) / deg_per_px
    w_px = (east - west) / deg_per_px
    n_rows = max(1, math.ceil(h_px / MAX_TILE_PX))
    n_cols = max(1, math.ceil(w_px / MAX_TILE_PX))
    lat_step = (north - south) / n_rows
    lon_step = (east - west) / n_cols
    logger.info(
        "Tile plan: %dx%d tiles (full ~%.0fx%.0f px at %.1fm res)",
        n_rows, n_cols, h_px, w_px, res_m,
    )

    # ─── 4. build & send WCS 2.0.1 GetCoverage(s) ───────────────────────────
    # WCS 2.0.1 takes one `subset` per axis; requests repeats list values, so
    # passing a 2-list yields `&subset=Lat(...)&subset=Long(...)`. The axis
    # labels MUST be the coverage's native ones — for EPSG:4258 that's
    # Lat/Long, not E/N (which would 404 with InvalidAxisLabel).
    base_params = {
        "service": "WCS",
        "version": "2.0.1",
        "request": "GetCoverage",
        "CoverageID": coverage_id,
        "format": "image/tiff",
        "subsettingcrs": coverage_crs,
    }

    def _fetch_tile(tw: float, ts: float, te: float, tn: float) -> bytes:
        params = {**base_params, "subset": [f"Lat({ts},{tn})", f"Long({tw},{te})"]}
        resp = requests.get(wcs_url, params=params, timeout=timeout)
        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code != 200 or "xml" in ctype or "html" in ctype:
            raise RuntimeError(
                f"WCS GetCoverage failed: HTTP {resp.status_code}, "
                f"Content-Type={ctype}\nURL: {resp.url}\n"
                f"Body (first 800 chars):\n{resp.text[:800]}"
            )
        return resp.content

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if n_rows == 1 and n_cols == 1:
        # Common case — single request, persist the server's bytes as-is.
        logger.info("WCS GetCoverage %s (%s)…", wcs_url, coverage_id)
        out_path.write_bytes(_fetch_tile(west, south, east, north))
    else:
        # Multi-tile: write each tile to a temp dir, then mosaic with
        # rasterio.merge. Adjacent tiles share an exact lon/lat boundary;
        # the WCS server expands each request to its native pixel grid, so
        # neighbouring tiles end up touching or slightly overlapping —
        # rio_merge handles both.
        with tempfile.TemporaryDirectory() as td:
            tile_paths: list[Path] = []
            for r in range(n_rows):
                for c in range(n_cols):
                    tw = west + c * lon_step
                    te = west + (c + 1) * lon_step
                    ts = south + r * lat_step
                    tn = south + (r + 1) * lat_step
                    idx = r * n_cols + c + 1
                    logger.info(
                        "  tile %d/%d: lon[%.5f..%.5f] lat[%.5f..%.5f]",
                        idx, n_rows * n_cols, tw, te, ts, tn,
                    )
                    tp = Path(td) / f"tile_{r}_{c}.tif"
                    tp.write_bytes(_fetch_tile(tw, ts, te, tn))
                    tile_paths.append(tp)

            srcs = [rasterio.open(p) for p in tile_paths]
            try:
                mosaic, transform = rio_merge(srcs)
                meta = srcs[0].meta.copy()
            finally:
                for s in srcs:
                    s.close()
            meta.update({
                "height": mosaic.shape[1],
                "width": mosaic.shape[2],
                "transform": transform,
            })
            with rasterio.open(out_path, "w", **meta) as dst:
                dst.write(mosaic)

    logger.info(
        "Saved %.1f MB → %s", out_path.stat().st_size / 1e6, out_path
    )
    return out_path


def compute_elevation(
    db_path: str | Path,
    dem_path: str | Path,
    crs: str,
    samples_per_edge: int = 8,
    edges_table: str = "edges",
) -> None:
    """Sample a DEM raster along each edge and write elevation columns.

    Algorithm (per edge):

        1. Load the edge's `LineString` from the stored WKB blob.
        2. Generate `samples_per_edge` evenly-spaced points along the line,
           expressed as fractions ``t = 0, 1/(n-1), …, 1`` of the line's
           length. ``t=0`` is the `from` end, ``t=1`` is the `to` end, and
           the intermediate points pick up undulations the straight u→v
           segment would miss (sidewalk dips, kerb ramps, hills).
        3. If the DEM raster is in a different CRS from the graph, project
           each sample point into the raster's CRS before reading from it
           (otherwise the sampling lands outside the raster and silently
           returns nodata).
        4. Read the raster value at each sample point with `rasterio.sample`
           (nearest-neighbour lookup against the underlying pixel grid).
           Replace nodata values with NaN.
        5. Derive six per-edge attributes from the elevation vector ``zs``:

               z_from = zs[0]            elevation at the `from` endpoint
               z_to   = zs[-1]           elevation at the `to`   endpoint
               z_min  = min(zs)          lowest point along the edge
               z_max  = max(zs)          highest point along the edge
               d_elev = z_to - z_from    signed climb from `from` to `to`
               slope  = d_elev / length  fractional grade (m / m)

    Edges with no geometry, or whose entire sample falls on nodata, get NaN
    for every elevation column. The function is idempotent: the columns are
    added with ``IF NOT EXISTS`` and the edges table is rewritten atomically
    via ``CREATE OR REPLACE``.
    """
    # Lazy imports — these deps are only needed when the elevation step runs.
    import numpy as np
    import rasterio
    from pyproj import Transformer
    from shapely import wkb

    dem_path = Path(dem_path)
    elev_cols = ("z_from", "z_to", "z_min", "z_max", "d_elev", "slope")
    logger.info(
        "Computing elevation for edges in %s (DEM=%s, %d samples/edge)",
        db_path, dem_path, samples_per_edge,
    )

    con = duckdb.connect(str(db_path))
    try:
        # ─── 1. prepare the edges table ─────────────────────────────────────
        # Add the six elevation columns if they're not already there. This
        # makes the function idempotent — re-running just overwrites values.
        for col in elev_cols:
            con.execute(
                f'ALTER TABLE "{edges_table}" ADD COLUMN IF NOT EXISTS {col} DOUBLE'
            )

        edges = con.execute(f'SELECT * FROM "{edges_table}"').fetchdf()
        logger.info("Loaded %d edges into memory for sampling", len(edges))

        # ─── 2. open the raster, set up CRS reprojection if needed ──────────
        # `rasterio.sample` reads pixel values at points expressed in the
        # raster's CRS, NOT the graph's. If the two differ, build a pyproj
        # Transformer once and apply it to every sample point. (For example:
        # graph in EPSG:25829, MDT raster in EPSG:4258.)
        with rasterio.open(dem_path) as src:
            nodata = src.nodata
            raster_crs = str(src.crs) if src.crs else None
            need_reproject = (
                raster_crs is not None
                and raster_crs.replace("EPSG:", "") != crs.replace("EPSG:", "")
            )
            to_raster: Transformer | None = (
                Transformer.from_crs(crs, raster_crs, always_xy=True)
                if need_reproject
                else None
            )
            logger.info(
                "Raster opened: crs=%s, shape=%s, nodata=%s | reproject=%s",
                raster_crs, src.shape, nodata, need_reproject,
            )

            # Fractions of edge length at which to sample. n=8 → 0.00, 0.14,
            # 0.29, …, 1.00. Endpoints are always included.
            ts = np.linspace(0.0, 1.0, samples_per_edge)

            # Bucket where every per-edge result is appended in graph order.
            results: dict[str, list[float]] = {c: [] for c in elev_cols}
            n_no_geom = 0   # edges missing a stored geometry
            n_blank = 0     # edges whose every sample was nodata
            log_every = max(1, len(edges) // 10)

            # ─── 3. main loop: one pass per edge ────────────────────────────
            for i, (geom_wkb, length) in enumerate(
                zip(edges["geometry"], edges["distance"])
            ):
                # 3a. No geometry → can't sample; emit NaN for every column.
                if geom_wkb is None:
                    n_no_geom += 1
                    for c in elev_cols:
                        results[c].append(float("nan"))
                    continue

                # 3b. Build sample points along the real (curvy) edge shape.
                # `interpolate(t, normalized=True)` returns the Point at
                # fraction `t` of the line's length.
                line = wkb.loads(bytes(geom_wkb))
                sample_pts = [line.interpolate(t, normalized=True) for t in ts]
                pts = [(p.x, p.y) for p in sample_pts]

                # 3c. Reproject sample points into the raster's CRS if needed.
                if to_raster is not None:
                    pts = [to_raster.transform(x, y) for x, y in pts]

                # 3d. Read elevations at the sample points (nearest pixel).
                # `src.sample` yields a 1-element array per point (band 1).
                zs = np.fromiter(
                    (val[0] for val in src.sample(pts)), dtype="float64"
                )
                if nodata is not None:
                    zs = np.where(zs == nodata, np.nan, zs)

                # 3e. Mask invalid samples and derive per-edge stats.
                valid = zs[~np.isnan(zs)]
                if valid.size == 0:
                    n_blank += 1
                    for c in elev_cols:
                        results[c].append(float("nan"))
                    continue

                # If an endpoint sample itself is nodata, fall back to the
                # nearest valid sample so z_from / z_to stay finite.
                z_from = zs[0] if not np.isnan(zs[0]) else valid[0]
                z_to = zs[-1] if not np.isnan(zs[-1]) else valid[-1]
                results["z_from"].append(float(z_from))
                results["z_to"].append(float(z_to))
                results["z_min"].append(float(valid.min()))
                results["z_max"].append(float(valid.max()))
                results["d_elev"].append(float(z_to - z_from))
                # `length` is the stored edge distance in metres → slope is
                # a dimensionless grade (rise over run).
                results["slope"].append(
                    float((z_to - z_from) / length) if length > 0 else 0.0
                )

                if (i + 1) % log_every == 0:
                    logger.info("  sampled %d/%d edges", i + 1, len(edges))

        # ─── 4. attach the new columns to the DataFrame ─────────────────────
        for c, vals in results.items():
            edges[c] = vals

        # ─── 5. write the enriched edges back to DuckDB atomically ──────────
        # CREATE OR REPLACE swaps the table in a single transaction so the
        # API never sees a half-written edges table mid-run.
        con.register("df_edges", edges)
        con.execute(f'CREATE OR REPLACE TABLE "{edges_table}" AS SELECT * FROM df_edges')

        # Quick sanity summary so the operator can spot empty rasters early.
        finite = edges["z_from"].notna().sum()
        if finite:
            logger.info(
                "Elevation done: %d edges sampled | finite=%d, no_geom=%d, all_nodata=%d "
                "| z range [%.2f .. %.2f] m, slope range [%.3f .. %.3f]",
                len(edges), finite, n_no_geom, n_blank,
                edges["z_min"].min(), edges["z_max"].max(),
                edges["slope"].min(), edges["slope"].max(),
            )
        else:
            logger.warning(
                "Elevation done but every edge is NaN — the raster at %s is "
                "either empty or outside the graph bbox.",
                dem_path,
            )
    finally:
        con.close()


def _arcgis_get(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    """POST an ArcGIS REST query and return the parsed JSON, with retries.

    Uses POST (not GET) so the envelope filter and `resultOffset` pagination
    don't blow past URL-length limits, and retries a few times because the
    Valencia geoportal occasionally drops a connection mid-page.
    """
    import json as _json
    import time
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode(params).encode()
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _json.load(resp)
        except Exception as e:  # noqa: BLE001 — network errors are varied
            last_err = e
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"ArcGIS query failed after retries: {url}") from last_err


def _fetch_arcgis_polylines(
    layer_url: str,
    crs: str,
    bbox: tuple[float, float, float, float] | None = None,
    out_fields: str = "*",
    page: int = 2000,
    timeout: float = 180.0,
):
    """Fetch an ArcGIS REST polyline layer as a GeoDataFrame in `crs`.

    The Valencia geoportal caps each response at `maxRecordCount` (2000)
    features, so this pages through with `resultOffset` until a short page
    comes back. When `bbox` (xmin, ymin, xmax, ymax, in `crs`) is given the
    query is restricted to that envelope server-side — essential here since
    the kerb layer alone is ~50k features city-wide and our graph usually
    covers a small slice. Esri polyline geometries carry one or more `paths`;
    a single path becomes a LineString, several become a MultiLineString.
    """
    import geopandas as gpd
    from shapely.geometry import LineString, MultiLineString

    epsg = str(crs).split(":")[-1]
    base = {
        "where": "1=1",
        "outFields": out_fields,
        "returnGeometry": "true",
        "outSR": epsg,
        "f": "json",
    }
    if bbox is not None:
        xmin, ymin, xmax, ymax = bbox
        base.update({
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": epsg,
            "spatialRel": "esriSpatialRelIntersects",
        })

    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        data = _arcgis_get(
            layer_url + "/query",
            {**base, "resultOffset": offset, "resultRecordCount": page},
            timeout,
        )
        chunk = data.get("features", [])
        for ft in chunk:
            paths = (ft.get("geometry") or {}).get("paths") or []
            lines = [LineString(p) for p in paths if len(p) >= 2]
            if not lines:
                continue
            geom = lines[0] if len(lines) == 1 else MultiLineString(lines)
            rows.append({**ft.get("attributes", {}), "geometry": geom})
        # A page shorter than the limit means we've drained the layer.
        if not chunk or len(chunk) < page:
            break
        offset += len(chunk)

    if not rows:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=crs)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)


def compute_overlay_features(
    db_path: str | Path,
    crs: str,
    kerb_url: str | None = None,
    type_a_url: str | None = None,
    kerb_tol_m: float = 4.0,
    lane_tol_m: float = 5.0,
    timeout: float = 180.0,
    nodes_table: str = "nodes",
    edges_table: str = "edges",
) -> None:
    """Attach an open-data overlay (kerbs + type-A PMV lanes) to an edges table.

    Pulls two ArcGIS REST polyline layers (assumed native to the project CRS)
    and joins them onto each edge by spatial proximity, writing columns:

        kerb         (BOOLEAN) — a kerb line runs within `kerb_tol_m`
        kerb_type    (VARCHAR) — `elemento` of the nearest kerb
                                 (BORDILLO / BORDILLO JARDINERO / ACERA BORDILLO)
        kerb_cross   (INTEGER) — number of kerb lines the edge crosses
        access_veh_a (BOOLEAN) — edge lies within `lane_tol_m` of a lane where
                                 type-A personal-mobility vehicles may circulate

    Sources:
        - cartografia-base-vorades-bordillos  (kerbs / bordillos)
        - vehiculos-movilidad-personal-tipo-a (type-A PMV lanes)

    Each layer is fetched restricted to the graph's bounding box (read from
    the `nodes` table and padded by the join tolerance) to keep the download
    small. The join uses `sjoin_nearest` with a `max_distance` cutoff, so an
    edge with no overlay feature within tolerance simply gets False / NULL.

    Each layer is handled independently and degrades gracefully: if its URL is
    omitted (``None``) or the fetch fails, the build does not abort. For kerbs
    that means every edge is treated as a *flat* kerb (no type, zero crossings),
    so the accessibility step adds no kerb penalty. For type-A lanes the
    ``access_veh_a`` column is simply not written, which leaves
    ``compute_accesibility_distance`` to fall back to ``veh_a_distance =
    alt_distance``. The function is idempotent — it rewrites the edges table
    atomically via CREATE OR REPLACE.
    """
    import geopandas as gpd
    from shapely import wkb

    con = duckdb.connect(str(db_path))
    try:
        edges = con.execute(f'SELECT * FROM "{edges_table}"').fetchdf()
        logger.info("Loaded %d edges (%s) for overlay join", len(edges), edges_table)

        # ─── 1. graph bbox (padded by the join tolerance) for server-side
        #         filtering, so we don't download the whole city. ────────────
        xmin, ymin, xmax, ymax = con.execute(
            f'SELECT min(x), min(y), max(x), max(y) FROM "{nodes_table}"'
        ).fetchone()
        pad = max(kerb_tol_m, lane_tol_m) + 5.0
        bbox = (xmin - pad, ymin - pad, xmax + pad, ymax + pad)

        # ─── 2. edges → GeoDataFrame (positional `_pos` keeps the link back to
        #         the original rows; rows without geometry are dropped). ──────
        geoms = [
            wkb.loads(bytes(g)) if isinstance(g, (bytes, bytearray)) and g else None
            for g in edges["geometry"]
        ]
        e = gpd.GeoDataFrame(
            {"_pos": range(len(edges))},
            geometry=gpd.GeoSeries(geoms, crs=crs),
        )
        e = e[e.geometry.notna()].copy()

        # ─── 3. fetch each overlay within the bbox, independently ───────────
        # A missing URL or a fetch failure returns None and triggers the
        # fallback for that layer instead of aborting the whole city build.
        def _try_fetch(url: str | None, label: str, out_fields: str):
            if not url:
                logger.info("No %s URL for %s — using fallback.", label, edges_table)
                return None
            try:
                layer = _fetch_arcgis_polylines(
                    url, crs, bbox=bbox, out_fields=out_fields, timeout=timeout
                )
                logger.info("Fetched %d %s features in bbox", len(layer), label)
                return layer
            except Exception as ex:  # noqa: BLE001 — any fetch/parse error → fallback
                logger.warning(
                    "%s overlay fetch failed (%s) — using fallback.", label, ex
                )
                return None

        kerbs = _try_fetch(kerb_url, "kerb", "elemento")
        lanes_a = _try_fetch(type_a_url, "type-A lane", "gid")

        # ─── 4. kerbs → type, boolean, and crossing count ───────────────────
        # Kerb *crossings* are the accessibility-relevant signal — a curb the
        # route must mount (transversal), as opposed to a kerb it merely runs
        # parallel to. A full street crossing typically scores 2; along-
        # sidewalk travel scores 0. With no kerb data every edge is a flat kerb
        # (no type, zero crossings) → the accessibility step adds no penalty.
        edges["kerb_type"] = None
        edges["kerb"] = False
        edges["kerb_cross"] = 0
        if kerbs is not None and not kerbs.empty:
            j = gpd.sjoin_nearest(
                e, kerbs[["elemento", "geometry"]],
                how="inner", max_distance=kerb_tol_m, distance_col="_d",
            )
            # An edge can tie to several kerb lines; keep the closest one.
            j = j.sort_values("_d").drop_duplicates("_pos")
            edges["kerb_type"] = edges.index.map(dict(zip(j["_pos"], j["elemento"])))
            edges["kerb"] = edges["kerb_type"].notna()

            jc = gpd.sjoin(e, kerbs[["geometry"]], how="inner", predicate="crosses")
            counts = jc.groupby("_pos").size()
            edges["kerb_cross"] = (
                counts.reindex(edges.index, fill_value=0).astype("int64").to_numpy()
            )

        # ─── 5. type-A PMV lanes: presence within tolerance → boolean ───────
        # Written only when real lane data is available; otherwise the column
        # is left off entirely so compute_accesibility_distance falls back to
        # veh_a_distance = alt_distance.
        def _near(layer) -> set[int]:
            if layer.empty:
                return set()
            j = gpd.sjoin_nearest(
                e, layer[["geometry"]], how="inner", max_distance=lane_tol_m
            )
            return set(j["_pos"].tolist())

        has_lane_data = lanes_a is not None
        if has_lane_data:
            edges["access_veh_a"] = edges.index.isin(_near(lanes_a))

        # ─── 6. write the enriched edges back atomically ────────────────────
        con.register("df_edges", edges)
        con.execute(f'CREATE OR REPLACE TABLE "{edges_table}" AS SELECT * FROM df_edges')
        logger.info(
            "Overlay features written for %s: kerb=%d, kerb_cross>0=%d, "
            "access_veh_a=%s (of %d edges)",
            edges_table,
            int(edges["kerb"].sum()),
            int((edges["kerb_cross"] > 0).sum()),
            int(edges["access_veh_a"].sum()) if has_lane_data
            else "n/a (→ veh_a_distance = alt_distance)",
            len(edges),
        )
    finally:
        con.close()


def compute_accesibility_distance(
    db_path: str | Path,
    attributes: list[str] | None = None,
    threshold: float = 1.50,
    k_up: float = 38.0,
    k_down: float = 23.0,
    slope_cap: float = 0.30,
    k_kerb: float = math.log(10),
    kerb_cross_cap: int = 2,
    edges_table: str = "edges",
) -> None:
    """Compute `accesibility` and `alt_distance` on the edges table.

    The width gate stays as before — an edge gets ``accesibility = 1`` when
    its first accessibility-driving attribute (``attributes[0]``, default
    ``width``) is at least ``threshold``; NULLs score as 0.

    ``alt_distance`` is then the Option-2 asymmetric-exponential cost from
    docs/alt_distance_proposal.pdf:

        C = L · exp(k_up · max(s, 0) + k_down · max(-s, 0)) / 10**accesibility

    where ``L = distance`` and ``s = slope`` (signed grade). Because edges
    are stored directed (initialize.py mirrors undirected source edges),
    the (u→v) and (v→u) rows naturally swap s+ ↔ s− and the asymmetry is
    encoded without per-row branching.

    A crossing-based kerb factor (Option 5 of the proposal) is then folded in
    multiplicatively:

        C ← C · exp(k_kerb · kerb_cross)

    where ``kerb_cross`` (written by ``compute_overlay_features``) counts the
    kerb lines the edge actually crosses — curbs the route must mount. With the
    CTE-anchored default ``k_kerb = ln(10)``, each crossing multiplies the cost
    by 10 (one accessibility tier), so a typical two-kerb street crossing costs
    ×100. If the ``kerb_cross`` column is absent the factor is 1 (no-op).

    ``kerb_cross`` is clamped to ``kerb_cross_cap`` (default 2) before the
    exponential. A real street crossing traverses at most two curbs (one per
    side); larger counts are artifacts of the fragmented kerb layer (median
    ~7 m segments), where a long or skewed edge skims many separate pieces.
    Without the cap those edges reach ``exp(ln(10)·16) = 10**16``, which both
    misrepresents the barrier and gives ``alt_distance`` a ~20-order-of-magnitude
    dynamic range that makes pandana's contraction-hierarchy build pathologically
    slow.

    Defaults are CTE-anchored: ``k_up = ln(10)/0.06 ≈ 38`` (TMA/851
    itinerario peatonal accesible limit) and ``k_down = ln(10)/0.10 ≈ 23``
    (steepest legal short ramp under DB-SUA 1 §4.3). Edges with NULL slope
    (no elevation column, or all-nodata samples) coalesce to s = 0 → the
    cost reduces to the pure width formula.

    ``slope_cap`` clamps |s| before the exponential. The DEM is 5 m
    resolution, so any edge much shorter than that picks up quantisation
    noise (e.g. a 4 cm segment that straddles a 5 m pixel boundary reports
    slope ≈ 20+). Capping at 0.30 (30%) keeps spurious slopes from
    overflowing ``exp(k * s)`` while remaining well above any real
    accessible grade — the capped edge ends up with a uniform large
    penalty rather than a NaN/inf cost.
    """
    col = (attributes or ["width"])[0]

    con = duckdb.connect(str(db_path))
    try:
        # Idempotent so the function can be re-run.
        con.execute(f'ALTER TABLE "{edges_table}" ADD COLUMN IF NOT EXISTS accesibility INTEGER')
        con.execute(f'ALTER TABLE "{edges_table}" ADD COLUMN IF NOT EXISTS alt_distance DOUBLE')

        # COALESCE so missing widths score as inaccessible instead of NULL.
        con.execute(
            f"""
            UPDATE "{edges_table}"
            SET accesibility = CASE
                WHEN COALESCE("{col}", 0) >= {threshold} THEN 1
                ELSE 0
            END
            """
        )

        # If the elevation step never ran, there's no `slope` column — fall
        # back to the pure width formula so the pipeline still produces a
        # usable alt_distance.
        has_slope = bool(con.execute(
            "SELECT 1 FROM information_schema.columns "
            f"WHERE table_name = '{edges_table}' AND column_name = 'slope'"
        ).fetchone())

        if has_slope:
            # `s_clipped` clamps |s| to [-slope_cap, slope_cap] so 5m-DEM
            # noise on near-zero-length edges (e.g. slope=23 on a 4cm segment)
            # can't overflow exp(k*s) to +inf.
            con.execute(
                f"""
                UPDATE "{edges_table}"
                SET alt_distance = distance
                    * EXP({k_up}   * GREATEST(LEAST( COALESCE(slope, 0.0),  {slope_cap}), 0.0)
                        + {k_down} * GREATEST(LEAST(-COALESCE(slope, 0.0),  {slope_cap}), 0.0))
                    / POWER(10, accesibility)
                """
            )
        else:
            logger.warning(
                "No `slope` column on edges — skipping slope penalty, "
                "alt_distance falls back to distance / 10^accesibility."
            )
            con.execute(
                f'UPDATE "{edges_table}" SET alt_distance = distance / POWER(10, accesibility)'
            )

        # Crossing-based kerb penalty (Option 5): fold in a multiplicative
        # factor exp(k_kerb * kerb_cross). Skipped cleanly when the Valencia
        # step never ran and the column is absent.
        has_kerb = bool(con.execute(
            "SELECT 1 FROM information_schema.columns "
            f"WHERE table_name = '{edges_table}' AND column_name = 'kerb_cross'"
        ).fetchone())
        if has_kerb:
            con.execute(
                f'UPDATE "{edges_table}" SET alt_distance = alt_distance '
                f"* EXP({k_kerb} * LEAST(COALESCE(kerb_cross, 0), {kerb_cross_cap}))"
            )

        # Type-A PMV metric: the *same* cost as alt_distance but with the
        # pedestrian width gate (10^accesibility) replaced by the
        # vehicle-accessibility gate (10^access_veh_a). Derived from alt_distance
        # — divide out the width gate, divide in the vehicle gate — so the
        # slope/kerb terms can never drift between the two metrics. An edge on a
        # type-A lane is 10× cheaper; off-lane edges pay full.
        #
        # When no type-A lane data is available (no `access_veh_a` column —
        # because the overlay URL was omitted or its fetch failed),
        # veh_a_distance simply mirrors alt_distance, so type-A routing degrades
        # to the general accessibility route rather than disappearing.
        has_veh_a = bool(con.execute(
            "SELECT 1 FROM information_schema.columns "
            f"WHERE table_name = '{edges_table}' AND column_name = 'access_veh_a'"
        ).fetchone())
        con.execute(f'ALTER TABLE "{edges_table}" ADD COLUMN IF NOT EXISTS veh_a_distance DOUBLE')
        if has_veh_a:
            con.execute(
                f'UPDATE "{edges_table}" SET veh_a_distance = alt_distance '
                "* POWER(10, accesibility) "
                "/ POWER(10, CASE WHEN COALESCE(access_veh_a, FALSE) THEN 1 ELSE 0 END)"
            )
        else:
            con.execute(
                f'UPDATE "{edges_table}" SET veh_a_distance = alt_distance'
            )

        accessible, total = con.execute(
            f'SELECT SUM(accesibility), COUNT(*) FROM "{edges_table}"'
        ).fetchone()
        if has_kerb:
            n_pen, max_x = con.execute(
                "SELECT COUNT(*) FILTER (WHERE COALESCE(kerb_cross, 0) > 0), "
                f'MAX(kerb_cross) FROM "{edges_table}"'
            ).fetchone()
            logger.info(
                "Kerb crossing penalty (k_kerb=%.3f, cap=%d) applied to %d edges "
                "(max %s raw crossings on one edge; penalty capped at ×%.0f)",
                k_kerb, kerb_cross_cap, n_pen, max_x,
                math.exp(k_kerb * kerb_cross_cap),
            )
        if has_slope:
            mean_r, med_r, max_r = con.execute(
                "SELECT AVG(alt_distance / distance), "
                "       quantile_cont(alt_distance / distance, 0.5), "
                "       MAX(alt_distance / distance) "
                f'FROM "{edges_table}" WHERE distance > 0'
            ).fetchone()
            logger.info(
                "Accessibility on %s: %d/%d edges accessible "
                "(col=%r, threshold=%.2f, k_up=%.1f, k_down=%.1f); "
                "alt_distance/distance: mean=%.3f, median=%.3f, max=%.3f",
                db_path, accessible, total, col, threshold, k_up, k_down,
                mean_r, med_r, max_r,
            )
        else:
            logger.info(
                "Accessibility on %s: %d/%d edges accessible "
                "(col=%r, threshold=%.2f) [no slope]",
                db_path, accessible, total, col, threshold,
            )
        if has_veh_a:
            on_lane, veh_mean = con.execute(
                "SELECT SUM(CAST(COALESCE(access_veh_a, FALSE) AS INTEGER)), "
                "       AVG(veh_a_distance / distance) "
                f'FROM "{edges_table}" WHERE distance > 0'
            ).fetchone()
            logger.info(
                "Type-A metric (veh_a_distance) written: %d/%d edges on a type-A "
                "lane; veh_a_distance/distance mean=%.3f",
                on_lane, total, veh_mean,
            )
        else:
            logger.info(
                "No type-A lane data on %s — veh_a_distance mirrors alt_distance.",
                db_path,
            )
    finally:
        con.close()





#############################################
#############################################

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(DEFAULT_CONFIG_PATH)
    section = cfg["initialize"]
    db_path = section["paths"]["db_path"]
    default_crs = cfg.get("crs", "EPSG:25830")
    attributes = section.get("attributes") or []

    # DEM rasters are fetched automatically and cached next to the database,
    # one per city, named `mdt_<city>.tif`.
    dem_dir = Path(db_path).parent

    # Steps below the graph load share their config across all cities; only
    # paths (graph/DEM) and the open-data overlay are per-city.
    elev_cfg = section.get("elevation")
    acc_cfg = section.get("accessibility") or {}

    cities = section.get("cities") or []
    if not cities:
        raise SystemExit(
            "config initialize.cities is empty — nothing to build. "
            "Add at least one city with name/graph_path/dem_path."
        )

    for city in cities:
        name = city["name"]
        nodes_table, edges_table = city_tables(name)
        # Per-city CRS overrides the top-level default; the auto-fetched DEM is
        # cached as data/mdt_<city>.tif (alongside the database).
        crs = city.get("crs", default_crs)
        dem_path = dem_dir / f"mdt_{slugify_city(name)}.tif"
        logger.info(
            "=== Building city %r (crs=%s) → %s / %s ===",
            name, crs, nodes_table, edges_table,
        )

        load_graph(
            graph_path=city["graph_path"],
            db_path=db_path,
            attributes=attributes,
            nodes_table=nodes_table,
            edges_table=edges_table,
        )

        # A failed/empty pickle load leaves no tables behind; skip the rest of
        # this city's steps (they'd error on the missing table) but keep going
        # so one bad source graph doesn't abort the whole multi-city run.
        with duckdb.connect(str(db_path)) as con:
            built = bool(con.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
                [nodes_table],
            ).fetchone())
        if not built:
            logger.error(
                "Skipping city %r — graph load produced no %s table.",
                name, nodes_table,
            )
            continue

        # Elevation step — shared params, per-city DEM raster. Only runs if the
        # `elevation` block exists in config.
        if elev_cfg:
            # Pull the graph bbox from the nodes table we just wrote.
            with duckdb.connect(str(db_path)) as con:
                bbox = con.execute(
                    f'SELECT min(x), min(y), max(x), max(y) FROM "{nodes_table}"'
                ).fetchone()

            fetch_dem(
                out_path=dem_path,
                bbox=bbox,
                bbox_crs=crs,
                wcs_url=elev_cfg.get("wcs_url", "https://servicios.idee.es/wcs-inspire/mdt"),
                coverage_id=elev_cfg.get("coverage_id", "Elevacion4258_5"),
                coverage_crs=elev_cfg.get("coverage_crs", "EPSG:4258"),
                pad_m=elev_cfg.get("bbox_pad_m", 50.0),
            )
            compute_elevation(
                db_path=db_path,
                dem_path=dem_path,
                crs=crs,
                samples_per_edge=elev_cfg.get("samples_per_edge", 8),
                edges_table=edges_table,
            )

        # Open-data overlay (kerbs + PMV-accessible lanes) — per-city and
        # optional. Only runs if this city has an `overlay` block.
        ov_cfg = city.get("overlay")
        if ov_cfg:
            compute_overlay_features(
                db_path=db_path,
                crs=crs,
                kerb_url=ov_cfg.get("kerb_url"),
                type_a_url=ov_cfg.get("type_a_url"),
                kerb_tol_m=ov_cfg.get("kerb_tol_m", 4.0),
                lane_tol_m=ov_cfg.get("lane_tol_m", 5.0),
                timeout=ov_cfg.get("timeout", 180.0),
                nodes_table=nodes_table,
                edges_table=edges_table,
            )

        compute_accesibility_distance(
            db_path=db_path,
            attributes=attributes,
            threshold=acc_cfg.get("width_threshold", 1.50),
            k_up=acc_cfg.get("k_up", 38.0),
            k_down=acc_cfg.get("k_down", 23.0),
            slope_cap=acc_cfg.get("slope_cap", 0.30),
            k_kerb=acc_cfg.get("k_kerb", math.log(10)),
            kerb_cross_cap=acc_cfg.get("kerb_cross_cap", 2),
            edges_table=edges_table,
        )