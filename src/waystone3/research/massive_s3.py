"""Massive / Polygon flat-file S3 (used only after NSDQ250 misses)."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENDPOINT = "https://files.massive.com"
DEFAULT_BUCKET = "flatfiles"


def s3_config() -> dict[str, str] | None:
    access = (
        os.getenv("MASSIVE_S3_ACCESS_KEY_ID")
        or os.getenv("POLYGON_S3_ACCESS_KEY_ID")
        or ""
    ).strip()
    secret = (
        os.getenv("MASSIVE_S3_SECRET_ACCESS_KEY")
        or os.getenv("POLYGON_S3_SECRET_ACCESS_KEY")
        or os.getenv("MASSIVE_API_KEY")
        or os.getenv("POLYGON_API_KEY")
        or ""
    ).strip()
    if not access or not secret:
        return None
    return {
        "endpoint": os.getenv("MASSIVE_S3_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/"),
        "bucket": os.getenv("MASSIVE_S3_BUCKET", DEFAULT_BUCKET),
        "access_key": access,
        "secret_key": secret,
    }


def _client(cfg: dict[str, str]):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
        config=Config(signature_version="s3v4"),
    )


def sync_prefix(prefix: str, dest: Path, *, max_keys: int = 200) -> list[Path]:
    """Download objects under ``s3://flatfiles/<prefix>`` to ``dest``."""
    cfg = s3_config()
    if cfg is None:
        return []
    dest.mkdir(parents=True, exist_ok=True)
    client = _client(cfg)
    paginator = client.get_paginator("list_objects_v2")
    written: list[Path] = []
    seen = 0
    for page in paginator.paginate(Bucket=cfg["bucket"], Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = str(obj["Key"])
            if key.endswith("/"):
                continue
            seen += 1
            if seen > max_keys:
                return written
            rel = Path(key).name
            path = dest / rel
            if path.is_file() and path.stat().st_size == int(obj.get("Size") or 0):
                written.append(path)
                continue
            client.download_file(cfg["bucket"], key, str(path))
            written.append(path)
    return written
