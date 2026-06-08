"""Download network data files from Tigris (T3) bucket on container startup.

Used by the docker-entrypoint.sh on Railway to seed /app/data/ before the
FastAPI process starts. Credentials are read from environment variables
set in the Railway dashboard.

Required files (hard fail if unreachable):
    network.duckdb

Optional files (logged warning on failure, API can still serve routes):
    graph.gpickle   — only needed for re-running initialize.py
    mdt_lidar.tif   — only needed for re-running initialize.py
"""

import logging
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

DATA_DIR = Path("/app/data")
REQUIRED = ["network.duckdb"]
OPTIONAL = ["graph.gpickle", "mdt_lidar.tif"]


def _client():
    endpoint = os.environ.get("T3_ENDPOINT", "https://t3.storageapi.dev")
    key_id = os.environ.get("T3_KEY_ID", "")
    key_secret = os.environ.get("T3_KEY_SECRET", "")
    bucket = os.environ.get("T3_BUCKET", "")

    if not all([key_id, key_secret, bucket]):
        logger.error(
            "Missing one or more environment variables: "
            "T3_KEY_ID, T3_KEY_SECRET, T3_BUCKET"
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


def download(client, bucket: str, key: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        logger.info("  %s already exists (%s), skipping.", key, _human_size(dest.stat().st_size))
        return True

    logger.info("  Fetching s3://%s/%s → %s …", bucket, key, dest)
    try:
        client.download_file(bucket, key, str(dest))
    except Exception as exc:
        logger.error("  Failed: %s", exc)
        return False

    size = dest.stat().st_size
    logger.info("  Done (%s).", _human_size(size))
    return True


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    s3, bucket = _client()

    failed = False

    for filename in REQUIRED + OPTIONAL:
        dest = DATA_DIR / filename
        ok = download(s3, bucket, filename, dest)
        if not ok and filename in REQUIRED:
            failed = True

    if failed:
        logger.error(
            "Required files could not be downloaded from T3 bucket '%s'. "
            "The API cannot start without network.duckdb.",
            bucket,
        )
        sys.exit(1)

    logger.info("Data directory ready.")


if __name__ == "__main__":
    main()
