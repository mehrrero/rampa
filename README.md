# rampa

Pedestrian routing over a sidewalk network, with optional accessibility
weighting (wider sidewalks preferred). The graph comes from a
[tile2net](https://github.com/VIDA-NYU/tile2net) export, is persisted in
DuckDB, and is served through a small FastAPI endpoint.

All coordinates throughout the project are in the CRS declared in
[`config.yaml`](config.yaml) — currently **EPSG:25830** (ETRS89 / UTM
30N, meters).


## Install

```bash
uv sync
```

(Requires Python 3.12 and [`uv`](https://docs.astral.sh/uv/).)


## One-time setup: build the DuckDB

The repo ships with a pickled tile2net graph at `data/graph.gpickle`.
Turn it into the routable DuckDB used by the rest of the code:

```bash
uv run python scripts/initialize.py
```

That runs two steps:

1. **`load_graph`** reads the gpickle and writes `nodes` and `edges`
   tables. Tile2net node ids are coordinate tuples and the graph is
   undirected; the script remaps ids to integers, mirrors each edge so
   pandana can route in both directions, and persists every edge's
   geometry as WKB.
2. **`compute_accesibility_distance`** adds two columns to `edges`:
   - `accesibility = 1` if `width >= threshold` (default 1.50 m),
     else 0.
   - `alt_distance = distance / 10**accesibility` — accessible edges
     become 10x cheaper, so shortest-path on `alt_distance` prefers
     them.

Inputs come from `config.yaml`:

```yaml
initialize:
  paths:
    graph_path: "data/graph.gpickle"
    db_path: "data/network.duckdb"
  attributes:
    - width
```


## Use it from Python

```python
import yaml
from src.network import Network

with open("config.yaml") as fh:
    cfg = yaml.safe_load(fh)

net = Network(cfg["initialize"]["paths"]["db_path"])

origin      = (699200.0, 4824000.0)   # (x, y) in EPSG:25830
destination = (700000.0, 4823000.0)

# Node-id sequence
nodes = net.route(origin, destination)

# GeoDataFrame of the route edges
route = net.route_gdf(origin, destination)

# Accessibility-weighted alternative
alt_route = net.route_gdf(origin, destination, alternate=True)
```

`route_gdf` returns `None` when an endpoint is farther than
`max_snap_distance` (default 500 m) from any node or when the endpoints
are in disconnected components. See [`example.ipynb`](example.ipynb) for
a full walkthrough including plotting and a primary-vs-alternate
comparison.


## Run the API

```bash
uv run uvicorn src.api:app --reload --port 8000
```

Then:

- Interactive docs: <http://localhost:8000/docs>
- Direct call:
  ```bash
  curl "http://localhost:8000/ruta?x1=699200&y1=4824000&x2=700000&y2=4823000"
  ```

Query parameters:

| name         | required | default | description                                       |
| ------------ | -------- | ------- | ------------------------------------------------- |
| `x1`, `y1`   | yes      | —       | Origin (x, y) in the network CRS                  |
| `x2`, `y2`   | yes      | —       | Destination (x, y) in the network CRS             |
| `max_snap_m` | no       | 500     | Max snap distance to nearest node; ≤ 0 disables   |

The response is `{"ruta": <GeoJSON>, "ruta_alt": <GeoJSON>}`. A 404 is
returned when no route can be produced.


## Project layout

```
.
├── config.yaml           # Shared project config (CRS + per-script sections)
├── data/
│   ├── graph.gpickle     # tile2net source graph
│   └── network.duckdb    # built by scripts/initialize.py
├── scripts/
│   └── initialize.py     # gpickle -> DuckDB pipeline
├── src/
│   ├── network.py        # Network class: routing on top of the DuckDB
│   └── api.py            # FastAPI app exposing /ruta
└── example.ipynb         # Demo notebook
```


## Notes on the data

- Node coordinates are stored *as-is* in EPSG:25830, so `distance` and
  `alt_distance` are in meters.
- tile2net graphs can be heavily fragmented (many small connected
  components — crosswalks and curb cuts that didn't get stitched
  upstream). Check connectivity before routing across the whole bbox;
  see the `pick-endpoints` cell in `example.ipynb` for an example using
  `networkx.connected_components`.
