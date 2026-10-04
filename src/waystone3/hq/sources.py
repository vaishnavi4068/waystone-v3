"""Object listing/reading for the loader: the GCS bucket in production, a local dir in tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ObjectInfo:
    name: str
    uri: str
    generation: int
    size: int
    updated: datetime | None


class ObjectSource(Protocol):
    def list(self, prefix: str) -> list[ObjectInfo]: ...

    def read(self, obj: ObjectInfo) -> bytes: ...


class LocalSource:
    """A directory laid out like the bucket (raw/paper/<code>/..., raw/backtest/...)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list(self, prefix: str) -> list[ObjectInfo]:
        base = self.root / prefix
        if not base.exists():
            return []
        out: list[ObjectInfo] = []
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            stat = path.stat()
            name = path.relative_to(self.root).as_posix()
            out.append(
                ObjectInfo(
                    name=name,
                    uri=f"file://{path}",
                    generation=stat.st_mtime_ns,
                    size=stat.st_size,
                    updated=datetime.fromtimestamp(stat.st_mtime, UTC),
                )
            )
        return out

    def read(self, obj: ObjectInfo) -> bytes:
        return (self.root / obj.name).read_bytes()


class GcsSource:
    def __init__(self, bucket: str, client: Any | None = None) -> None:
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self.bucket_name = bucket
        self.bucket = client.bucket(bucket)
        self._client = client

    def list(self, prefix: str) -> list[ObjectInfo]:
        out: list[ObjectInfo] = []
        for blob in self._client.list_blobs(self.bucket_name, prefix=prefix):
            if blob.name.endswith("/"):
                continue
            out.append(
                ObjectInfo(
                    name=blob.name,
                    uri=f"gs://{self.bucket_name}/{blob.name}",
                    generation=int(blob.generation),
                    size=int(blob.size or 0),
                    updated=blob.updated,
                )
            )
        return out

    def read(self, obj: ObjectInfo) -> bytes:
        blob = self.bucket.blob(obj.name, generation=obj.generation)
        data: bytes = blob.download_as_bytes()
        return data


def open_source(location: str) -> ObjectSource:
    if location.startswith("gs://"):
        return GcsSource(location.removeprefix("gs://").strip("/"))
    return LocalSource(Path(location))
