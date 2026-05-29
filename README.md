# rampa

**Pedestrian sidewalk routing with accessibility weighting.**

Given a sidewalk network — originally exported from [tile2net](https://github.com/VIDA-NYU/tile2net) — `rampa` computes shortest-path routes that optionally prefer wider, flatter sidewalks and account for kerb crossings and type-A personal-mobility-vehicle (PMV) lanes. The cost model follows Spanish accessibility regulations (CTE / DB-SUA / Orden TMA/851).

The network is persisted in [DuckDB](https://duckdb.org/) and served through a small [FastAPI](https://fastapi.tiangolo.com/) endpoint.

All coordinates are in **EPSG:25830** (ETRS89 / UTM 30N, meters) — see [`config.yaml`](config.yaml).

---

## Quick start

### Local (no Docker)

Requirements: Python 3.12 + [uv](https://docs.astral.sh/uv/).

```bash
# 1. Install dependencies
make install

# 2. One-time: build the DuckDB from the pickle graph
make initialize

# 3. Run the API with hot-reload
make api
```

The API is then available at `http://localhost:8000`.

---

## Docker

### docker-compose (recommended)

```bash
docker compose up --build
```

This builds the image, mounts `./config` and `./data` from the host, and serves the API on port `8000`.

### Manual docker run

```bash
docker build -t rampa-api .
docker run --rm -p 8000:8000 \
  -v "$PWD/config:/app/config" \
  -v "$PWD/data:/app/data:ro" \
  rampa-api
```

**Volume notes:**

| Volume | Mount | Writable? | Purpose |
|--------|-------|-----------|---------|
| `./config` | `/app/config` | Yes | `config.yaml` — seeded from defaults on first start |
| `./data` | `/app/data` | No (ro) | `network.duckdb`, `graph.gpickle`, DEM raster |

> The database at `data/network.duckdb` is **already checked into the repo** for Valencia. If you're starting from scratch, run `make initialize` locally first (or copy a built database into `./data/`).

---

## API endpoints

### `GET /health`

Health check.

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok"}
```

---

### `GET /ruta`

Compute a route between two coordinates.

```bash
curl "http://localhost:8000/ruta?x1=726550.22584&y1=4369304.840704&x2=725639.632903&y2=4374012.487261"
```

#### Query parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `x1`, `y1` | float | yes | — | Origin (x, y) in the network CRS (EPSG:25830) |
| `x2`, `y2` | float | yes | — | Destination (x, y) in the network CRS |
| `mode` | string | no | `alt` | Accessibility route: `"alt"` (general accessibility) or `"veh_a"` (type-A PMV lanes) |
| `max_snap_m` | float | no | `500` | Max snap distance (meters) from input coordinate to nearest network node. `≤ 0` disables the check |

#### Response format

```json
{
  "ruta": { /* GeoJSON FeatureCollection */ },
  "ruta_alt": { /* GeoJSON FeatureCollection */ },
  "mode": "alt",
  "metadata": {
    "ruta": { "total_length": 1250.7 },
    "ruta_alt": { "total_length": 1342.3 }
  }
}
```

- **`ruta`** — shortest path by raw distance (GeoJSON FeatureCollection).
- **`ruta_alt`** or **`ruta_veh_a`** — shortest path weighted by the accessibility model (key depends on `mode`).
- **`metadata`** — per-route total length in meters.

Each feature in the GeoJSON `FeatureCollection` represents a sidewalk edge with the following `properties`:

| Property | Type | Description |
|----------|------|-------------|
| `from` | int | Source node ID |
| `to` | int | Target node ID |
| `distance` | float | Edge length (m) |
| `width` | float | Sidewalk width (m) |
| `z_from`, `z_to` | int | Elevation level at endpoints |
| `z_min`, `z_max` | int | Min / max elevation level along the edge |
| `d_elev` | float | Elevation difference (m) |
| `slope` | float | Slope (m/m) |
| `kerb_type` | string or null | Kerb type description (e.g. `"ACERA BORDILLO"`) |
| `kerb` | bool | Whether a kerb is present |
| `kerb_cross` | int | Number of kerb crossings |
| `access_veh_a` | bool | Whether a type-A PMV lane is accessible |
| `accesibility` | int | Accessibility flag (1 = accessible, 0 = not) |
| `alt_distance` | float | Accessibility-weighted distance (m) |
| `veh_a_distance` | float | Type-A PMV weighted distance (m) |

#### Error responses

| Status | Meaning |
|--------|---------|
| `400` | Unknown `mode` or `veh_a` unavailable (DB lacks the column) |
| `404` | Endpoint too far from network or endpoints in disconnected components |

---

## Frontend integration

### JavaScript / TypeScript (fetch)

```js
const params = new URLSearchParams({
  x1: "699200", y1: "4824000",
  x2: "700000", y2: "4823000",
  mode: "alt",
});

const res = await fetch(`http://localhost:8000/ruta?${params}`);
const data = await res.json();

// data.ruta              — primary route (GeoJSON FeatureCollection)
// data.ruta_alt          — accessibility route (GeoJSON FeatureCollection)
// data.metadata.ruta.total_length         — primary route length (meters)
// data.metadata.ruta_alt.total_length     — accessibility route length (meters)
```

### React + MapLibre/Leaflet example

```tsx
import { useEffect, useState } from "react";
import type { FeatureCollection } from "geojson";

interface RouteResponse {
  ruta: FeatureCollection;
  ruta_alt: FeatureCollection;
  mode: "alt" | "veh_a";
  metadata: Record<string, { total_length: number }>;
}

function RouteMap() {
  const [route, setRoute] = useState<RouteResponse | null>(null);

  useEffect(() => {
    const params = new URLSearchParams({
      x1: "699200", y1: "4824000",
      x2: "700000", y2: "4823000",
      mode: "alt",
    });

    fetch(`http://localhost:8000/ruta?${params}`)
      .then((r) => r.json())
      .then(setRoute);
  }, []);

  // Render geojson on your map library of choice
}
```

> **CORS:** The API ships with `allow_origins=["*"]` for development. Restrict to your frontend origin before deploying.

### OpenAPI / Swagger

Interactive API docs at `http://localhost:8000/docs` (auto-generated by FastAPI).

---

## Local development

### One-time setup

```bash
make install
make initialize
```

`make initialize` runs `scripts/initialize.py`, which:

1. **`load_graph`** — reads `data/graph.gpickle`, remaps coordinate-tuple node IDs to integers, mirrors undirected edges for pandana, and writes `nodes` / `edges` tables to DuckDB.
2. **`fetch_dem` + `compute_elevation`** — downloads a LiDAR DEM (IDEE WCS), samples elevation per edge, writes slope.
3. **`compute_valencia_features`** — fetches kerb and type-A PMV lane polylines from Valencia's open-data API, joins spatially onto edges.
4. **`compute_accesibility_distance`** — applies the accessibility cost model.

### Run the API

```bash
make api
```

Or directly:

```bash
uv run uvicorn src.api:app --reload --port 8000
```

### Demo notebook

[`example.ipynb`](example.ipynb) contains a full walkthrough with plots, network statistics, accessibility maps, and route comparisons.

---

## Configuration

All settings live in [`config.yaml`](config.yaml):

```yaml
crs: "EPSG:25830"                        # Coordinate reference system

initialize:
  paths:
    graph_path: "data/graph.gpickle"     # tile2net source graph
    db_path: "data/network.duckdb"       # built DuckDB
    dem_path: "data/mdt_lidar.tif"       # LiDAR DEM
  elevation:                             # Per-edge slope computation
    samples_per_edge: 8
    wcs_url: "https://servicios.idee.es/wcs-inspire/mdt"
    coverage_id: "Elevacion4258_5"
  valencia:                              # Open-data overlays
    kerb_url: "…"                        # ArcGIS REST: kerbs (bordillos)
    type_a_url: "…"                      # ArcGIS REST: type-A PMV lanes
    kerb_tol_m: 4.0
    lane_tol_m: 5.0
  accessibility:                         # Cost model parameters
    width_threshold: 1.50
    k_up: 38.0
    k_down: 23.0
    slope_cap: 0.30
    k_kerb: 2.302585092994046
    kerb_cross_cap: 2
```

Comment out the `elevation` or `valencia` blocks to skip those steps during `make initialize`.

---

## Project layout

```
.
├── config.yaml                 # Shared configuration (CRS, paths, cost model)
├── docker-compose.yml          # Docker orchestration
├── Dockerfile                  # Multi-stage API image
├── docker-entrypoint.sh        # Container entrypoint (seeds config)
├── Makefile                    # Convenience targets: install, initialize, api
├── pyproject.toml              # Python dependencies (uv)
├── uv.lock                     # Locked dependency tree
├── data/
│   ├── graph.gpickle           # tile2net source graph (OSMnx)
│   ├── network.duckdb          # Built DuckDB (nodes, edges, accessibility)
│   └── mdt_lidar.tif           # Digital Elevation Model (LiDAR)
├── scripts/
│   └── initialize.py           # Data pipeline: gpickle → DuckDB
├── src/
│   ├── api.py                  # FastAPI application (/ruta, /health)
│   └── network.py              # Network class (pandana routing engine)
├── docs/
│   ├── alt_distance_proposal.pdf
│   └── alt_distance_proposal.tex
└── example.ipynb               # Demo notebook with plots
```
