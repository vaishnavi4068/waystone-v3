"""Object store for a daily dump: GCS, or a local directory with the same keys."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ReportStore(Protocol):
    def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None: ...

    def get(self, key: str) -> bytes | None: ...

    def exists(self, key: str) -> bool: ...

    def list_keys(self, prefix: str) -> list[str]: ...


class LocalFsStore:
    """Mirrors GCS object keys as files under ``root`` (used by tests and --dry-run)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        del content_type
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_keys(self, prefix: str) -> list[str]:
        if not self.root.exists():
            return []
        out: list[str] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if rel.startswith(prefix):
                out.append(rel)
        return sorted(out)


class GcsStore:
    def __init__(self, bucket: str) -> None:
        from google.cloud import storage

        self.bucket_name = bucket
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        blob = self._bucket.blob(key)
        blob.upload_from_string(data, content_type=content_type)

    def get(self, key: str) -> bytes | None:
        blob = self._bucket.blob(key)
        if not blob.exists():
            return None
        payload = blob.download_as_bytes()
        return bytes(payload)

    def exists(self, key: str) -> bool:
        return bool(self._bucket.blob(key).exists())

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(b.name for b in self._client.list_blobs(self.bucket_name, prefix=prefix))


def build_report_store_from_env() -> ReportStore | None:
    local = os.getenv("IBKR_REPORTS_LOCAL_DIR", "").strip()
    store: ReportStore | None
    if local:
        store = LocalFsStore(Path(local))
    else:
        bucket = os.getenv("IBKR_REPORTS_BUCKET", "").strip()
        store = GcsStore(bucket) if bucket else None
    if store is None:
        return None
    flag = os.getenv("IBKR_STAGED", "1").strip().lower()
    if flag in {"0", "false", "no"}:
        return store
    from waystone3.ibkr.staged import OverlayStore, staged_fixture_store

    return OverlayStore(store, staged_fixture_store())
