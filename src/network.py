import os
import sys
import logging
from pathlib import Path
import pandas as pd
import geopandas as gpd
import pandana
import osmnx as ox
from shapely.ops import unary_union
from shapely.validation import explain_validity
import duckdb
from shapely import LineString, wkb
import numpy as np
import requests
import yaml
from pyproj import Transformer
import json
from shapely.geometry import LineString, mapping

logger = logging.getLogger(__name__)

# Project-root config.yaml (this file lives in src/).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"



class Network:
    """Pedestrian street network loaded from DuckDB, with routing on top.

    Reads `nodes` and `edges` from a DuckDB database produced by
    `scripts/initialize.py` and exposes two `pandana.Network` instances:

    - ``network``     — shortest-path by raw distance.
    - ``alt_network`` — shortest-path by ``alt_distance`` (accessibility-weighted).

    Coordinates are stored in the project CRS (read from ``config.yaml``,
    currently EPSG:25830 / UTM 30N meters). All routing inputs must be in
    the same CRS.

    Attributes:
        crs (str): Project CRS string, e.g. ``"EPSG:25830"``.
        db_connection (duckdb.DuckDBPyConnection): Open DuckDB connection.
        nodes (DataFrame): Network nodes indexed by id with ``x`` / ``y`` columns.
        edges (DataFrame): Edges with ``from`` / ``to`` / ``distance`` /
            ``alt_distance`` and optionally a ``geometry`` (WKB) column.
        network (pandana.Network): Pandana network weighted by ``distance``.
        alt_network (pandana.Network): Pandana network weighted by ``alt_distance``.
    """

    def __init__(self, db_path, cols: list = None, config_path=DEFAULT_CONFIG_PATH):
        """Open `db_path`, load nodes/edges, and build the pandana networks.

        Args:
            db_path: Path to the DuckDB file produced by
                ``scripts/initialize.py``.
            cols: Reserved for forthcoming attribute selection; unused today.
            config_path: Path to the shared project config. Only ``crs`` is
                read at the moment.
        """
        # Load shared project settings (currently just the CRS).
        with open(config_path, "r") as fh:
            cfg = yaml.safe_load(fh) or {}
        self.crs = cfg.get("crs", "EPSG:25830")

        # `__init__` only ever runs once per instance, so the hasattr guard
        # is defensive — leave it in case a subclass calls `__init__` again.
        if hasattr(self, "db_connection") and self.db_connection is not None:
            self.db_connection.close()
        self.db_connection = duckdb.connect(db_path)

        self.__get_network()


    def __get_network(self):
        """Read `nodes` / `edges` from DuckDB and build the two pandana networks.

        On read failure the method logs and returns without populating
        ``self.network`` / ``self.alt_network`` — any later routing call will
        therefore raise ``AttributeError``, which is intentional: routing on a
        half-initialised Network should fail loudly.
        """
        # DuckDB round-trips can change numeric dtypes (e.g. int → object on
        # nullable columns); pandana is strict about types, so recast every
        # column it cares about before constructing the networks.
        try:
            logger.info("Loading network from database")
            self.nodes = self.db_connection.execute("SELECT * FROM nodes").fetchdf()
            self.edges = self.db_connection.execute("SELECT * FROM edges").fetchdf()
            self.nodes.set_index('index', inplace=True)
            self.nodes['x'] = self.nodes['x'].astype('float64')
            self.nodes['y'] = self.nodes['y'].astype('float64')
            self.edges['from'] = self.edges['from'].astype('int64')
            self.edges['to'] = self.edges['to'].astype('int64')
            self.edges['distance'] = self.edges['distance'].astype('float64')
            self.edges['alt_distance'] = self.edges['alt_distance'].astype('float64')
        except Exception as e:
            logger.error("Error loading network from database: %s", e)
            return

        # Build two parallel pandana networks over the same node/edge tables,
        # differing only in which weight column drives shortest-path. Keeping
        # both materialised lets `route()` switch between them per call
        # without rebuilding the graph.
        self.alt_network = pandana.Network(
            self.nodes['x'],
            self.nodes['y'],
            self.edges['from'],
            self.edges['to'],
            self.edges[['alt_distance']],
        )

        self.network = pandana.Network(
            self.nodes['x'],
            self.nodes['y'],
            self.edges['from'],
            self.edges['to'],
            self.edges[['distance']],
        )

    def route(self, coord1, coord2, alternate=False, max_snap_distance=500.0):
        """Shortest-path node sequence between two points.

        Args:
            coord1, coord2: (x, y) in the same CRS as the network nodes
                (currently EPSG:25830 / UTM 30N, meters).
            alternate: use the accessibility-weighted network when True.
            max_snap_distance: if either input point is farther than this
                (in CRS units) from the nearest network node, return None.
                Set to None to disable the check.

        Returns:
            Array of node ids along the shortest path, or None if either
            endpoint is too far from the network or no route exists.
        """
        x1, y1 = coord1
        x2, y2 = coord2
        xs = pd.Series([x1, x2])
        ys = pd.Series([y1, y2])

        net = self.alt_network if alternate else self.network
        ids = net.get_node_ids(xs, ys)

        # Pandana's get_node_ids always snaps to the nearest node, even if it's
        # hundreds of km away — guard against callers passing coords in the
        # wrong CRS (e.g. lon/lat) or outside the network bbox.
        if max_snap_distance is not None:
            snapped = self.nodes.loc[list(ids)]
            dx = snapped['x'].to_numpy() - xs.to_numpy()
            dy = snapped['y'].to_numpy() - ys.to_numpy()
            if (np.hypot(dx, dy) > max_snap_distance).any():
                return None

        path = net.shortest_path(ids[0], ids[1])
        return path if len(path) else None

    def get_linestring(self, row):
        """Edge geometry in the network's CRS.

        Returns the persisted WKB geometry when available; falls back to a
        straight LineString between the `from`/`to` node coordinates if the
        edge has no stored geometry.
        """
        wkb_bytes = row.get('geometry')
        if isinstance(wkb_bytes, (bytes, bytearray)) and wkb_bytes:
            # DuckDB returns BLOBs as bytearray; shapely's from_wkb does
            # np.asarray(..., dtype=object) which iterates a bytearray into
            # ints. Wrapping with bytes() forces scalar handling.
            return wkb.loads(bytes(wkb_bytes))

        f = self.nodes.loc[row['from']]
        t = self.nodes.loc[row['to']]
        return LineString([(f['x'], f['y']), (t['x'], t['y'])])
    
    
    def path(self, node_list):
        """GeoDataFrame of edges along `node_list` (in the network's CRS)."""
        if len(node_list) < 2:
            return gpd.GeoDataFrame(geometry=[], crs=self.crs)

        pairs = [(node_list[i], node_list[i + 1]) for i in range(len(node_list) - 1)]
        ed = pd.concat(
            [self.edges[(self.edges['from'] == u) & (self.edges['to'] == v)]
             for u, v in pairs]
        ).copy()
        ed['geometry'] = ed.apply(self.get_linestring, axis=1)
        return gpd.GeoDataFrame(ed, geometry='geometry', crs=self.crs)



    def route_gdf(self, coord1, coord2, alternate=False, max_snap_distance=500.0):
        """GeoDataFrame of the route between two (x, y) points in the network's CRS."""
        pat = self.route(
            coord1, coord2,
            alternate=alternate,
            max_snap_distance=max_snap_distance,
        )
        if pat is None:
            return None
        return self.path(pat)