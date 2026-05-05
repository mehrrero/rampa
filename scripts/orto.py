"""Batch orthophoto tile downloader backed by DuckDB.

Reads its configuration from `orto.yaml` and downloads the WMS catalogue
(every layer, intersected with an optional clip bbox) into a single DuckDB
database, tile-by-tile, with per-tile corner coordinates.

Downloads run in a thread pool — WMS fetches are I/O-bound so threads scale
well. Saves go through a single shared DuckDB connection guarded by a lock,
which is the recommended pattern for multi-threaded writes.
"""

from __future__ import annotations

import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator

import duckdb
import yaml
from owslib.wms import WebMapService
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


class OrthoTileDownloader:
    """Tile a bbox, fetch each tile from a WMS in parallel, persist to DuckDB.

    Each tile is stored as a BLOB alongside its corner coordinates so the
    table can be queried spatially without re-hitting the WMS. Existing
    `tile_id`s are loaded in bulk per layer so interrupted runs resume
    cheaply (no per-tile SELECT).
    """

    def __init__(
        self,
        wms_url: str,
        db_path: str | Path,
        srs: str = "EPSG:25830",
        tile_size_m: float = 1024.0,
        pixels_per_tile: int = 4096,
        image_format: str = "image/tiff",
        wms_version: str = "1.3.0",
        max_workers: int = 8,
    ) -> None:
        self.wms = WebMapService(wms_url, version=wms_version)
        self.srs = srs
        self.tile_size_m = tile_size_m
        self.pixels_per_tile = pixels_per_tile
        self.image_format = image_format
        self.db_path = str(db_path)
        self.max_workers = max_workers

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # Single persistent connection shared across threads. DuckDB's Python
        # connection isn't fully thread-safe, so all writes go through
        # `_db_lock`.
        self._db = duckdb.connect(self.db_path)
        self._db_lock = threading.Lock()
        self._init_db()

        logger.info(
            "Initialised downloader (srs=%s, tile=%.1fm@%dpx, workers=%d, db=%s)",
            self.srs,
            self.tile_size_m,
            self.pixels_per_tile,
            self.max_workers,
            self.db_path,
        )

    def close(self) -> None:
        """Close the underlying DuckDB connection."""
        with self._db_lock:
            self._db.close()

    def __enter__(self) -> "OrthoTileDownloader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Database helpers
    # ------------------------------------------------------------------ #
    def _init_db(self) -> None:
        """Create the tiles table on first use."""
        with self._db_lock:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS tiles (
                    tile_id       VARCHAR PRIMARY KEY,
                    layer         VARCHAR,
                    srs           VARCHAR,
                    x_min         DOUBLE,
                    y_min         DOUBLE,
                    x_max         DOUBLE,
                    y_max         DOUBLE,
                    width_px      INTEGER,
                    height_px     INTEGER,
                    format        VARCHAR,
                    image         BLOB,
                    downloaded_at TIMESTAMP DEFAULT current_timestamp
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tiles_layer ON tiles(layer)"
            )

    def _existing_tile_ids(self, layer: str) -> set[str]:
        """Return all `tile_id`s already stored for `layer`."""
        with self._db_lock:
            rows = self._db.execute(
                "SELECT tile_id FROM tiles WHERE layer = ?", [layer]
            ).fetchall()
        return {row[0] for row in rows}

    def _save(
        self,
        tile_id: str,
        layer: str,
        bbox: BBox,
        width: int,
        height: int,
        data: bytes,
    ) -> None:
        """Persist a fetched tile and its bbox into DuckDB."""
        with self._db_lock:
            self._db.execute(
                """
                INSERT OR IGNORE INTO tiles
                    (tile_id, layer, srs, x_min, y_min, x_max, y_max,
                     width_px, height_px, format, image)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    tile_id,
                    layer,
                    self.srs,
                    *bbox,
                    width,
                    height,
                    self.image_format,
                    data,
                ],
            )

    # ------------------------------------------------------------------ #
    # Tiling
    # ------------------------------------------------------------------ #
    def _iter_tile_bboxes(self, bbox: BBox) -> Iterator[BBox]:
        """Yield sub-bboxes covering `bbox` in row-major order.

        Edge tiles are clipped to the input bbox, so the last column/row may
        be narrower than `tile_size_m`.
        """
        x_min, y_min, x_max, y_max = bbox
        step = self.tile_size_m
        x = x_min
        while x < x_max:
            tx_max = min(x + step, x_max)
            y = y_min
            while y < y_max:
                ty_max = min(y + step, y_max)
                yield (x, y, tx_max, ty_max)
                y = ty_max
            x = tx_max

    @staticmethod
    def _tile_id(layer: str, bbox: BBox) -> str:
        """Stable id from the layer name + tile corners (mm precision)."""
        return "{}__{:.3f}_{:.3f}_{:.3f}_{:.3f}".format(layer, *bbox)

    # ------------------------------------------------------------------ #
    # WMS introspection
    # ------------------------------------------------------------------ #
    def list_layers(self) -> list[str]:
        """Return every named layer advertised by the WMS."""
        return list(self.wms.contents)

    def _layer_native_bbox(self, layer_name: str) -> BBox | None:
        """Return the layer's bbox in `self.srs`, or None if unavailable."""
        layer = self.wms.contents[layer_name]

        for bb in getattr(layer, "boundingBoxes", []) or []:
            if isinstance(bb, dict) and bb.get("crs") == self.srs:
                x1, y1, x2, y2 = bb["bbox"]
                return (float(x1), float(y1), float(x2), float(y2))

        bb = getattr(layer, "boundingBox", None)
        if bb and len(bb) >= 5 and bb[4] == self.srs:
            return (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))

        if self.srs.upper() in {"EPSG:4326", "CRS:84"}:
            wgs = getattr(layer, "boundingBoxWGS84", None)
            if wgs:
                return (float(wgs[0]), float(wgs[1]), float(wgs[2]), float(wgs[3]))

        return None

    @staticmethod
    def _intersect(a: BBox, b: BBox) -> BBox | None:
        """Return the bbox intersection, or None if they don't overlap."""
        x1 = max(a[0], b[0])
        y1 = max(a[1], b[1])
        x2 = min(a[2], b[2])
        y2 = min(a[3], b[3])
        if x1 >= x2 or y1 >= y2:
            return None
        return (x1, y1, x2, y2)

    # ------------------------------------------------------------------ #
    # WMS fetch
    # ------------------------------------------------------------------ #
    def _fetch_tile(self, layer: str, bbox: BBox) -> tuple[bytes, int, int]:
        """Request a single tile from the WMS and return (bytes, w, h)."""
        x_min, y_min, x_max, y_max = bbox
        # Scale pixel size proportionally for clipped edge tiles so the
        # ground resolution stays constant across the whole bbox.
        width = max(1, round((x_max - x_min) / self.tile_size_m * self.pixels_per_tile))
        height = max(1, round((y_max - y_min) / self.tile_size_m * self.pixels_per_tile))

        response = self.wms.getmap(
            layers=[layer],
            srs=self.srs,
            bbox=bbox,
            size=(width, height),
            format=self.image_format,
        )
        return response.read(), width, height

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def download_layer(self, layer: str, bbox: BBox) -> int:
        """Download every tile covering `bbox` for one layer in parallel."""
        existing = self._existing_tile_ids(layer)

        all_tiles = list(self._iter_tile_bboxes(bbox))
        todo = [tb for tb in all_tiles if self._tile_id(layer, tb) not in existing]
        skipped = len(all_tiles) - len(todo)
        logger.info(
            "Layer %s: %d tile(s) total, %d to fetch, %d already in db",
            layer,
            len(all_tiles),
            len(todo),
            skipped,
        )
        if not todo:
            return 0

        downloaded = 0
        bytes_downloaded = 0

        # Producer/consumer pattern: ThreadPoolExecutor handles HTTP fetches,
        # the main thread drains as_completed and serialises DB writes via
        # `_save` (lock-guarded).
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_to_bbox = {
                pool.submit(self._fetch_tile, layer, tb): tb for tb in todo
            }
            progress = tqdm(
                as_completed(future_to_bbox),
                total=len(todo),
                desc=layer,
                unit="tile",
                leave=False,
            )
            for future in progress:
                tile_bbox = future_to_bbox[future]
                tile_id = self._tile_id(layer, tile_bbox)
                try:
                    data, width, height = future.result()
                    self._save(tile_id, layer, tile_bbox, width, height, data)
                    downloaded += 1
                    bytes_downloaded += len(data)
                except Exception:
                    # Log and keep going so one bad tile doesn't abort a long run.
                    logger.exception("failed to download tile %s", tile_id)
                progress.set_postfix(
                    new=downloaded, size=_humanise(bytes_downloaded)
                )

        logger.info(
            "Layer %s done: %d new tile(s), %d skipped, %s downloaded",
            layer,
            downloaded,
            skipped,
            _humanise(bytes_downloaded),
        )
        return downloaded

    def download_catalogue(
        self,
        layers: list[str] | None = None,
        clip_bbox: BBox | None = None,
    ) -> int:
        """Download every requested layer over its declared extent.

        Args:
            layers: Explicit layer names to fetch. If None or empty, every
                layer in the WMS capabilities is used.
            clip_bbox: Optional bbox (in `self.srs`) to intersect every
                layer's native extent with.
        """
        layer_names = layers or self.list_layers()
        logger.info("Downloading %d layer(s) from WMS", len(layer_names))

        total_downloaded = 0
        for layer in layer_names:
            native_bbox = self._layer_native_bbox(layer)
            if native_bbox is None:
                logger.warning(
                    "Layer %s: no bbox in %s in capabilities, skipping",
                    layer,
                    self.srs,
                )
                continue

            target_bbox: BBox | None = native_bbox
            if clip_bbox is not None:
                target_bbox = self._intersect(native_bbox, clip_bbox)
                if target_bbox is None:
                    logger.info(
                        "Layer %s: clip bbox does not intersect, skipping", layer
                    )
                    continue

            total_downloaded += self.download_layer(layer, target_bbox)

        logger.info(
            "Catalogue done: %d new tile(s) saved to %s",
            total_downloaded,
            self.db_path,
        )
        return total_downloaded


# ---------------------------------------------------------------------- #
# Config loading
# ---------------------------------------------------------------------- #
def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load YAML config from disk."""
    with open(path, "r") as fh:
        return yaml.safe_load(fh) or {}


def _bbox_from_config(section: Any) -> BBox | None:
    """Convert the optional `bbox` mapping from YAML into a tuple."""
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

    wms_cfg = cfg["wms"]
    tiling_cfg = cfg.get("tiling", {})
    output_cfg = cfg["output"]
    parallel_cfg = cfg.get("parallelism", {})

    with OrthoTileDownloader(
        wms_url=wms_cfg["url"],
        wms_version=wms_cfg.get("version", "1.3.0"),
        db_path=output_cfg["db_path"],
        srs=tiling_cfg.get("srs", "EPSG:25830"),
        tile_size_m=float(tiling_cfg.get("tile_size_m", 1024.0)),
        pixels_per_tile=int(tiling_cfg.get("pixels_per_tile", 4096)),
        image_format=tiling_cfg.get("image_format", "image/tiff"),
        max_workers=int(parallel_cfg.get("max_workers", 8)),
    ) as downloader:
        downloader.download_catalogue(
            layers=cfg.get("layers") or None,
            clip_bbox=_bbox_from_config(cfg.get("bbox")),
        )
