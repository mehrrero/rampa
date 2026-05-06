"""Batch PNOA-LiDAR tile downloader backed by DuckDB.

Reads its configuration from `lidar.yaml` and downloads PNOA-LiDAR `.laz`
point clouds for the configured bbox into a single DuckDB database,
tile-by-tile, with per-tile corner coordinates.

For Valencia, the data is republished by the Institut Cartogràfic
Valencià (ICV) on a regular 2 km grid in EPSG:25830, with file names
keyed by the south-west corner of each tile expressed in kilometres
(e.g. `..._726-4374.laz` covers x∈[726000, 728000], y∈[4374000, 4376000]).
The download URL is built from a template containing `{x_km}` / `{y_km}`
placeholders, so the same script handles any other PNOA-LiDAR coverage
that follows the same convention by pointing the template at it.

Downloads run in a thread pool — HTTP fetches are I/O-bound so threads
scale well. Saves go through a single shared DuckDB connection guarded
by a lock, which is the recommended pattern for multi-threaded writes.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import duckdb
import requests
import yaml
from tqdm import tqdm

logger = logging.getLogger(__name__)

# Bounding box as (x_min, y_min, x_max, y_max) in CRS units.
BBox = tuple[float, float, float, float]

# Default config path: sibling YAML next to this script.
DEFAULT_CONFIG_PATH = Path(__file__).with_suffix(".yaml")


def _humanise(num_bytes: int) -> str:
    """Format a byte count as a short human-readable string (e.g. '12.3 MB')."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


class LidarTileDownloader:
    """Tile a bbox, fetch each tile from a URL template, persist to DuckDB.

    Each tile is stored as a BLOB alongside its corner coordinates so the
    table can be queried spatially without re-hitting the server. Existing
    `tile_id`s are loaded in bulk per product so interrupted runs resume
    cheaply (no per-tile SELECT). Tiles outside the published coverage
    (HTTP 404) are recorded as misses to avoid retrying them on rerun.
    """

    def __init__(
        self,
        url_template: str,
        db_path: str | Path,
        product: str,
        srs: str = "EPSG:25830",
        tile_size_m: float = 2000.0,
        grid_origin: tuple[float, float] = (0.0, 0.0),
        request_timeout: float = 300.0,
        chunk_size: int = 1024 * 1024,
        max_workers: int = 4,
        user_agent: str = "rampa-lidar/0.1",
    ) -> None:
        self.url_template = url_template
        self.product = product
        self.srs = srs
        self.tile_size_m = tile_size_m
        self.grid_origin = grid_origin
        self.request_timeout = request_timeout
        self.chunk_size = chunk_size
        self.max_workers = max_workers
        self.db_path = str(db_path)

        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # Single persistent connection shared across threads. DuckDB's Python
        # connection isn't fully thread-safe, so all writes go through
        # `_db_lock`.
        self._db = duckdb.connect(self.db_path)
        self._db_lock = threading.Lock()
        self._init_db()

        logger.info(
            "Initialised downloader (product=%s, srs=%s, tile=%.0fm, "
            "workers=%d, db=%s)",
            self.product,
            self.srs,
            self.tile_size_m,
            self.max_workers,
            self.db_path,
        )

    def close(self) -> None:
        """Close the underlying DuckDB connection and HTTP session."""
        with self._db_lock:
            self._db.close()
        self._session.close()

    def __enter__(self) -> "LidarTileDownloader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Database helpers
    # ------------------------------------------------------------------ #
    def _init_db(self) -> None:
        """Create the tiles + misses tables on first use."""
        with self._db_lock:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS tiles (
                    tile_id       VARCHAR PRIMARY KEY,
                    product       VARCHAR,
                    file_name     VARCHAR,
                    source_url    VARCHAR,
                    srs           VARCHAR,
                    x_min         DOUBLE,
                    y_min         DOUBLE,
                    x_max         DOUBLE,
                    y_max         DOUBLE,
                    size_bytes    BIGINT,
                    data          BLOB,
                    downloaded_at TIMESTAMP DEFAULT current_timestamp
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tiles_product ON tiles(product)"
            )
            # Misses are remembered so interrupted runs don't re-issue HTTP
            # requests for tiles that the provider doesn't publish.
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS misses (
                    tile_id       VARCHAR PRIMARY KEY,
                    product       VARCHAR,
                    source_url    VARCHAR,
                    status_code   INTEGER,
                    checked_at    TIMESTAMP DEFAULT current_timestamp
                )
                """
            )

    def _existing_tile_ids(self) -> set[str]:
        """Return all `tile_id`s already stored or known-missing for this product."""
        with self._db_lock:
            tile_rows = self._db.execute(
                "SELECT tile_id FROM tiles WHERE product = ?", [self.product]
            ).fetchall()
            miss_rows = self._db.execute(
                "SELECT tile_id FROM misses WHERE product = ?", [self.product]
            ).fetchall()
        return {row[0] for row in tile_rows} | {row[0] for row in miss_rows}

    def _save(
        self,
        tile_id: str,
        bbox: BBox,
        file_name: str,
        source_url: str,
        data: bytes,
    ) -> None:
        """Persist a fetched tile and its bbox into DuckDB."""
        with self._db_lock:
            self._db.execute(
                """
                INSERT OR IGNORE INTO tiles
                    (tile_id, product, file_name, source_url, srs,
                     x_min, y_min, x_max, y_max, size_bytes, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    tile_id,
                    self.product,
                    file_name,
                    source_url,
                    self.srs,
                    *bbox,
                    len(data),
                    data,
                ],
            )

    def _save_miss(self, tile_id: str, source_url: str, status_code: int) -> None:
        """Record a tile that the server doesn't publish (HTTP 404)."""
        with self._db_lock:
            self._db.execute(
                """
                INSERT OR IGNORE INTO misses
                    (tile_id, product, source_url, status_code)
                VALUES (?, ?, ?, ?)
                """,
                [tile_id, self.product, source_url, status_code],
            )

    # ------------------------------------------------------------------ #
    # Tiling
    # ------------------------------------------------------------------ #
    def _iter_tile_bboxes(self, bbox: BBox) -> Iterator[BBox]:
        """Yield 2 km grid cells covering `bbox`, snapped to `grid_origin`.

        The grid is aligned so cell corners land on
        `grid_origin + k * tile_size_m`, matching ICV's filename convention
        where each LAZ is keyed by its SW corner in kilometres.
        """
        x_min, y_min, x_max, y_max = bbox
        ox, oy = self.grid_origin
        step = self.tile_size_m

        i_min = math.floor((x_min - ox) / step)
        i_max = math.ceil((x_max - ox) / step)
        j_min = math.floor((y_min - oy) / step)
        j_max = math.ceil((y_max - oy) / step)

        for i in range(i_min, i_max):
            tx_min = ox + i * step
            for j in range(j_min, j_max):
                ty_min = oy + j * step
                yield (tx_min, ty_min, tx_min + step, ty_min + step)

    def _tile_url(self, tile_bbox: BBox) -> str:
        """Render the URL template for a tile using its SW corner in km."""
        x_min, y_min, *_ = tile_bbox
        return self.url_template.format(
            x_km=int(round(x_min / 1000.0)),
            y_km=int(round(y_min / 1000.0)),
            x_m=int(round(x_min)),
            y_m=int(round(y_min)),
        )

    @staticmethod
    def _tile_id(product: str, bbox: BBox) -> str:
        """Stable id from the product name + tile corners (mm precision)."""
        return "{}__{:.3f}_{:.3f}_{:.3f}_{:.3f}".format(product, *bbox)

    @staticmethod
    def _file_name(url: str) -> str:
        """Last path segment of a URL — used as the tile's file name."""
        return Path(urlparse(url).path).name

    # ------------------------------------------------------------------ #
    # HTTP fetch
    # ------------------------------------------------------------------ #
    def _fetch_tile(self, url: str) -> tuple[bytes | None, int]:
        """Stream a single tile.

        Returns `(bytes, 200)` on success, `(None, status)` for 404 or
        other 4xx responses (caller records these as misses).
        """
        with self._session.get(
            url, timeout=self.request_timeout, stream=True
        ) as response:
            if 400 <= response.status_code < 500:
                return None, response.status_code
            response.raise_for_status()
            buf = bytearray()
            for chunk in response.iter_content(chunk_size=self.chunk_size):
                if chunk:
                    buf.extend(chunk)
            return bytes(buf), response.status_code

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def download_bbox(self, bbox: BBox) -> int:
        """Download every published tile covering `bbox` in parallel."""
        existing = self._existing_tile_ids()

        all_tiles = list(self._iter_tile_bboxes(bbox))
        todo = [tb for tb in all_tiles if self._tile_id(self.product, tb) not in existing]
        skipped = len(all_tiles) - len(todo)
        logger.info(
            "Product %s: %d tile(s) total, %d to fetch, %d already in db",
            self.product,
            len(all_tiles),
            len(todo),
            skipped,
        )
        if not todo:
            return 0

        downloaded = 0
        misses = 0
        bytes_downloaded = 0

        # Producer/consumer pattern: ThreadPoolExecutor handles HTTP fetches,
        # the main thread drains as_completed and serialises DB writes via
        # `_save` (lock-guarded).
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_to_bbox = {
                pool.submit(self._fetch_tile, self._tile_url(tb)): tb
                for tb in todo
            }
            progress = tqdm(
                as_completed(future_to_bbox),
                total=len(todo),
                desc=self.product,
                unit="tile",
                leave=False,
            )
            for future in progress:
                tile_bbox = future_to_bbox[future]
                tile_id = self._tile_id(self.product, tile_bbox)
                url = self._tile_url(tile_bbox)
                try:
                    data, status = future.result()
                    if data is None:
                        self._save_miss(tile_id, url, status)
                        misses += 1
                    else:
                        self._save(
                            tile_id, tile_bbox, self._file_name(url), url, data
                        )
                        downloaded += 1
                        bytes_downloaded += len(data)
                except Exception:
                    # Log and keep going so one bad tile doesn't abort a long run.
                    logger.exception("failed to download tile %s", url)
                progress.set_postfix(
                    new=downloaded,
                    miss=misses,
                    size=_humanise(bytes_downloaded),
                )

        logger.info(
            "Product %s done: %d new tile(s), %d miss(es), %d skipped, %s downloaded",
            self.product,
            downloaded,
            misses,
            skipped,
            _humanise(bytes_downloaded),
        )
        return downloaded


# ---------------------------------------------------------------------- #
# Config loading
# ---------------------------------------------------------------------- #
def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load YAML config from disk."""
    with open(path, "r") as fh:
        return yaml.safe_load(fh) or {}


def _bbox_from_config(section: Any) -> BBox | None:
    """Convert the `bbox` mapping from YAML into a tuple."""
    if not section:
        return None
    return (
        float(section["x_min"]),
        float(section["y_min"]),
        float(section["x_max"]),
        float(section["y_max"]),
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    cfg = load_config(config_path)

    source_cfg = cfg["source"]
    tiling_cfg = cfg.get("tiling", {})
    output_cfg = cfg["output"]
    parallel_cfg = cfg.get("parallelism", {})
    http_cfg = cfg.get("http", {})

    bbox = _bbox_from_config(cfg.get("bbox"))
    if bbox is None:
        raise SystemExit("config: `bbox` is required")

    grid_origin_cfg = tiling_cfg.get("grid_origin", [0.0, 0.0])

    with LidarTileDownloader(
        url_template=source_cfg["url_template"],
        product=source_cfg["product"],
        db_path=output_cfg["db_path"],
        srs=tiling_cfg.get("srs", "EPSG:25830"),
        tile_size_m=float(tiling_cfg.get("tile_size_m", 2000.0)),
        grid_origin=(float(grid_origin_cfg[0]), float(grid_origin_cfg[1])),
        request_timeout=float(http_cfg.get("timeout", 300.0)),
        chunk_size=int(http_cfg.get("chunk_size", 1024 * 1024)),
        max_workers=int(parallel_cfg.get("max_workers", 4)),
    ) as downloader:
        downloader.download_bbox(bbox)
