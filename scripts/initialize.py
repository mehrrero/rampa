"""Load a pickled OSMnx graph and persist it to DuckDB.

Reads its configuration from the project-root `config.yaml`, under the
`initialize:` section. The resulting `nodes` and `edges` tables match
the schema expected by `src.network.Network.__get_network` on reload.
"""

from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml
from shapely.geometry import LineString

logger = logging.getLogger(__name__)

# Default config path: project-root config.yaml (this file lives in scripts/).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


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
) -> None:
    """Read a pickled (tile2net-style) graph and write nodes/edges to DuckDB.

    The output schema mirrors what `Network.__get_network` expects on reload:
    `nodes(index, x, y)` and `edges(from, to, distance, alt_distance, ...)`.
    Any names in `attributes` are copied from the edge data dict onto the
    edges table (e.g. ``width``).
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
        con.execute("CREATE OR REPLACE TABLE nodes AS SELECT * FROM df_nodes")
        con.execute("CREATE OR REPLACE TABLE edges AS SELECT * FROM df_edges")
        logger.info(
            "Stored %d nodes and %d edges → %s", len(nodes), len(edges), db_path
        )
    finally:
        con.close()

def compute_accesibility_distance(
    db_path: str | Path,
    attributes: list[str] | None = None,
    threshold: float = 1.50,
) -> None:
    """Compute `accesibility` and `alt_distance` on the edges table.

    An edge is accessible (``accesibility = 1``) if its first
    accessibility-driving attribute (``attributes[0]``, default ``width``)
    is at least ``threshold``; NULLs are treated as 0. ``alt_distance``
    is then ``distance / 10**accesibility``, so accessible edges are 10x
    cheaper when routed via the alt network.
    """
    col = (attributes or ["width"])[0]

    con = duckdb.connect(str(db_path))
    try:
        # Idempotent so the function can be re-run.
        con.execute("ALTER TABLE edges ADD COLUMN IF NOT EXISTS accesibility INTEGER")
        con.execute("ALTER TABLE edges ADD COLUMN IF NOT EXISTS alt_distance DOUBLE")

        # COALESCE so missing widths score as inaccessible instead of NULL.
        con.execute(
            f"""
            UPDATE edges
            SET accesibility = CASE
                WHEN COALESCE("{col}", 0) >= {threshold} THEN 1
                ELSE 0
            END
            """
        )
        con.execute(
            "UPDATE edges SET alt_distance = distance / POWER(10, accesibility)"
        )

        accessible, total = con.execute(
            "SELECT SUM(accesibility), COUNT(*) FROM edges"
        ).fetchone()
        logger.info(
            "Accessibility on %s: %d/%d edges accessible "
            "(col=%r, threshold=%.2f)",
            db_path, accessible, total, col, threshold,
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
    paths_cfg = section["paths"]

    load_graph(
        graph_path=paths_cfg["graph_path"],
        db_path=paths_cfg["db_path"],
        attributes=section.get("attributes") or [],
    )

    compute_accesibility_distance(
    db_path=paths_cfg["db_path"],
    attributes=section.get("attributes") or [],
)