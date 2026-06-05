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
| `./data` | `/app/data` | No (ro) | pre-built `network.duckdb` read by the API |

> Docker/Railway runtime does not run `make initialize`. Build or update `data/network.duckdb` outside the container, then make that file available at `/app/data/network.duckdb`.

### Railway

Railway starts from the uploaded DuckDB, not from graph initialization:

1. Build or update `data/network.duckdb` locally.
2. Upload it to the T3/S3-compatible bucket:

```bash
T3_KEY_ID=... T3_KEY_SECRET=... T3_BUCKET=... python scripts/upload_to_t3.py
```

3. Deploy the Docker image with `T3_KEY_ID`, `T3_KEY_SECRET`, and `T3_BUCKET` set in Railway.

On startup, `docker-entrypoint.sh` downloads only `network.duckdb` into `/app/data` and then starts `uvicorn`. Graph pickle and DEM files are not downloaded or used in deployment.

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

### `GET /cities`

List the available city networks and the default used when `/ruta` omits `city`.

```bash
curl http://localhost:8000/cities
```

```json
{"cities": ["valencia"], "default": "valencia"}
```

---

### `GET /ruta`

Compute a route between two coordinates.

All coordinates are in **EPSG:25830** (ETRS89 / UTM 30N, meters), so they differ
per city. Use `GET /cities` for the available names; omit `city` to use the
default (`valencia`).

**Valencia** (default city — `city` may be omitted):

```bash
curl "http://localhost:8000/ruta?city=valencia&x1=726538.273048&y1=4369685.099138&x2=725471.941018&y2=4371464.411097"
```

**Madrid:**

```bash
curl "http://localhost:8000/ruta?city=madrid&x1=441936.50143&y1=4474738.053088&x2=438726.727451&y2=4474983.775073"
```

Add `&mode=veh_a` to either query to weight the secondary route by type-A
PMV lanes instead of general accessibility (returned under `ruta_veh_a`).

#### Query parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `x1`, `y1` | float | yes | — | Origin (x, y) in the network CRS (EPSG:25830) |
| `x2`, `y2` | float | yes | — | Destination (x, y) in the network CRS |
| `city` | string | no | first configured city | Which city's network to route on. See `GET /cities` for available names |
| `mode` | string | no | `alt` | Accessibility route: `"alt"` (general accessibility) or `"veh_a"` (type-A PMV lanes) |
| `max_snap_m` | float | no | `500` | Max snap distance (meters) from input coordinate to nearest network node. `≤ 0` disables the check |

#### Response format

```json
{
  "ruta": { /* GeoJSON FeatureCollection */ },
  "ruta_alt": { /* GeoJSON FeatureCollection */ },
  "city": "valencia",
  "mode": "alt",
  "metadata": {
    "ruta": {
      "total_length": 1250.7,
      "walking_time": "16 min",
      "wheelchair_time": "29 min",
      "total_ascent": 18.4,
      "total_descent": 12.1
    },
    "ruta_alt": {
      "total_length": 1342.3,
      "walking_time": "18 min",
      "wheelchair_time": "31 min",
      "total_ascent": 9.2,
      "total_descent": 2.9
    }
  }
}
```

- **`ruta`** — shortest path by raw distance (GeoJSON FeatureCollection).
- **`ruta_alt`** or **`ruta_veh_a`** — shortest path weighted by the accessibility model (key depends on `mode`).
- **`metadata`** — per-route summary:
  - `total_length` — route length in meters.
  - `walking_time` / `wheelchair_time` — estimated traversal time, derived from `total_length` at the speeds configured under `api.travel_speeds` (defaults: 1.24 m/s walking, 0.7 m/s wheelchair). Formatted as `"X h Y min"`, or `"Y min"` when under an hour.
  - `total_ascent` / `total_descent` — cumulative climb / drop in meters (present only when elevation data is available).

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
| `404` | Unknown `city`, endpoint too far from network, or endpoints in disconnected components |

---

## Frontend integration

### JavaScript / TypeScript (fetch)

```js
// Valencia (default city). For Madrid, pass city: "madrid" and Madrid
// EPSG:25830 coordinates, e.g. x1: "441936.50143", y1: "4474738.053088".
const params = new URLSearchParams({
  city: "valencia",
  x1: "726538.273048", y1: "4369685.099138",
  x2: "725471.941018", y2: "4371464.411097",
  mode: "alt",
});

const res = await fetch(`http://localhost:8000/ruta?${params}`);
const data = await res.json();

// data.ruta              — primary route (GeoJSON FeatureCollection)
// data.ruta_alt          — accessibility route (GeoJSON FeatureCollection)
// data.metadata.ruta.total_length         — primary route length (meters)
// data.metadata.ruta.walking_time         — primary route walking time ("X h Y min")
// data.metadata.ruta.wheelchair_time      — primary route wheelchair time ("X h Y min")
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
  metadata: Record<
    string,
    {
      total_length: number;
      walking_time: string;
      wheelchair_time: string;
      total_ascent?: number;
      total_descent?: number;
    }
  >;
}

function RouteMap() {
  const [route, setRoute] = useState<RouteResponse | null>(null);

  useEffect(() => {
    const params = new URLSearchParams({
      city: "valencia", // or "madrid" with Madrid EPSG:25830 coordinates
      x1: "726538.273048", y1: "4369685.099138",
      x2: "725471.941018", y2: "4371464.411097",
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

`make initialize` runs `scripts/initialize.py`, which loops over every city in
`config.yaml`'s `initialize.cities` list and, for each, writes a dedicated
`nodes_<city>` / `edges_<city>` table pair into the single DuckDB. Per city it:

1. **`load_graph`** — reads the city's `graph_path`, remaps coordinate-tuple node IDs to integers, mirrors undirected edges for pandana, and writes the `nodes_<city>` / `edges_<city>` tables.
2. **`fetch_dem` + `compute_elevation`** — downloads a LiDAR DEM (IDEE WCS), cached as `data/mdt_<city>.tif`, samples elevation per edge, writes slope.
3. **`compute_overlay_features`** — if the city has an `overlay` block, fetches kerb and type-A PMV lane polylines from its open-data API and joins them spatially onto edges.
4. **`compute_accesibility_distance`** — applies the accessibility cost model.

The API (`Network` + `/ruta?city=…`) reads these per-city tables back, one
`Network` per configured city.

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
crs: "EPSG:25830"                        # default CRS (per-city override below)

initialize:
  paths:
    db_path: "data/network.duckdb"       # single DuckDB, one table pair per city
  cities:                                # one entry per city to build
    - name: valencia                     # → tables nodes_valencia / edges_valencia
      graph_path: "data/graph.gpickle"   # tile2net source graph
      crs: "EPSG:25830"                  # optional; overrides the top-level CRS
      overlay:                           # open-data overlays (optional, per city)
        kerb_url: "…"                    # ArcGIS REST: kerbs (bordillos)
        type_a_url: "…"                  # ArcGIS REST: type-A PMV lanes
        kerb_tol_m: 4.0
        lane_tol_m: 5.0
  elevation:                             # Per-edge slope computation (shared)
    samples_per_edge: 8
    wcs_url: "https://servicios.idee.es/wcs-inspire/mdt"
    coverage_id: "Elevacion4258_5"
  accessibility:                         # Cost model parameters (shared)
    width_threshold: 1.50
    k_up: 38.0
    k_down: 23.0
    slope_cap: 0.30
    k_kerb: 2.302585092994046
    kerb_cross_cap: 2

api:
  travel_speeds:                         # m/s, used for route time estimates
    walking: 1.24
    wheelchair: 0.7
```

Add more entries under `cities` to build several cities into the same DuckDB;
give a city its own `crs` when its graph uses a different projection. Each
city's LiDAR DEM is fetched automatically and cached next to the database as
`mdt_<city>.tif` — no path to set. Comment out the shared `elevation` block to
skip slope, or omit a city's `overlay` block (or just one of its URLs) to skip
that layer during `make initialize`.

The two overlays degrade gracefully: if a URL is missing **or its fetch fails**,
the build doesn't abort. Kerbs fall back to flat (no crossing penalty), and
`veh_a_distance` falls back to equal `alt_distance` — so type-A routing still
works, it just matches the general accessibility route.

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
│   ├── network.duckdb          # Built DuckDB (nodes_<city> / edges_<city>)
│   └── mdt_<city>.tif          # Auto-fetched DEM, one per city (LiDAR)
├── scripts/
│   └── initialize.py           # Data pipeline: gpickle → DuckDB (per city)
├── src/
│   ├── api.py                  # FastAPI application (/ruta, /cities, /health)
│   ├── network.py              # Network class (pandana routing engine)
│   └── tables.py               # Per-city table-name helpers (shared)
├── docs/
│   ├── alt_distance_proposal.pdf
│   └── alt_distance_proposal.tex
└── example.ipynb               # Demo notebook with plots
```
