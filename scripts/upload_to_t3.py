"""Upload pre-built data files to a Tigris (T3) bucket.

Run once locally after ``make initialize`` to push the network database,
graph pickle, and DEM raster to the T3 bucket. The Railway deployment
then downloads them on container startup.

Credentials are passed via environment variables (see README section
for Railway setup). Files are uploaded with paths matching what the
download script expects:

    network.duckdb   — required
    graph.gpickle     — optional (re-initialization only)
    mdt_lidar.tif     — optional (re-initialization only)

Usage:
    T3_KEY_ID=... T3_KEY_SECRET=... T3_BUCKET=... python scripts/upload_to_t3.py
"""

import logging
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

DATA_DIR = Path("data")
FILES = ["network.duckdb", "graph.gpickle", "mdt_lidar.tif"]


def _client():
    endpoint = os.environ.get("T3_ENDPOINT", "https://t3.storageapi.dev")
    key_id = os.environ.get("T3_KEY_ID", "")
    key_secret = os.environ.get("T3_KEY_SECRET", "")
    bucket = os.environ.get("T3_BUCKET", "")

    missing = []
    if not key_id:
        missing.append("T3_KEY_ID")
    if not key_secret:
        missing.append("T3_KEY_SECRET")
    if not bucket:
        missing.append("T3_BUCKET")
    if missing:
        logger.error(
            "Missing environment variables: %s", ", ".join(missing)
        )
        sys.exit(1)

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=key_secret,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    return s3, bucket


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def main():
    s3, bucket = _client()

    for filename in FILES:
        local = DATA_DIR / filename
        if not local.exists() or local.stat().st_size == 0:
            logger.warning(
                "Skipping %s — file not found or empty in %s/", filename, DATA_DIR
            )
            continue

        size = local.stat().st_size
        logger.info(
            "Uploading %s (%s) → s3://%s/%s …", filename, _human_size(size), bucket, filename
        )
        try:
            s3.upload_file(str(local), bucket, filename)
        except Exception as exc:
            logger.error("Upload failed for %s: %s", filename, exc)
            sys.exit(1)
        logger.info("  Done.")

    logger.info("Upload complete.")


if __name__ == "__main__":
    main()
