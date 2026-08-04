"""Artifact storage: MinIO/S3 when configured, local filesystem otherwise."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("storage")
LOCAL_ROOT = Path("./var/artifacts")


class ArtifactStore:
    def __init__(self) -> None:
        self._client: Any | None = None
        self.backend = "filesystem"

    async def connect(self) -> None:
        if not (settings.minio_endpoint and settings.minio_access_key and settings.minio_secret_key):
            LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
            log.info("storage_backend", backend="filesystem", path=str(LOCAL_ROOT))
            return
        try:
            from minio import Minio

            client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
            )
            await asyncio.to_thread(self._ensure_bucket, client)
            self._client = client
            self.backend = "minio"
            log.info("storage_backend", backend="minio", bucket=settings.minio_bucket)
        except Exception as exc:
            LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
            log.warning("minio_unavailable_falling_back", error=str(exc))

    @staticmethod
    def _ensure_bucket(client: Any) -> None:
        if not client.bucket_exists(settings.minio_bucket):
            client.make_bucket(settings.minio_bucket)

    @property
    def healthy(self) -> bool:
        return self._client is not None

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> dict[str, Any]:
        content_type = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
        digest = hashlib.sha256(data).hexdigest()
        if self._client is not None:
            import io

            await asyncio.to_thread(
                self._client.put_object,
                settings.minio_bucket,
                key,
                io.BytesIO(data),
                len(data),
                content_type=content_type,
            )
            uri = f"s3://{settings.minio_bucket}/{key}"
        else:
            path = LOCAL_ROOT / key
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, data)
            uri = f"file://{path.resolve()}"
        return {"uri": uri, "size_bytes": len(data), "sha256": digest, "content_type": content_type}

    async def get(self, key: str) -> bytes:
        if self._client is not None:
            def _read() -> bytes:
                resp = self._client.get_object(settings.minio_bucket, key)
                try:
                    return resp.read()
                finally:
                    resp.close()
                    resp.release_conn()

            return await asyncio.to_thread(_read)
        return await asyncio.to_thread((LOCAL_ROOT / key).read_bytes)

    async def delete(self, key: str) -> None:
        if self._client is not None:
            await asyncio.to_thread(self._client.remove_object, settings.minio_bucket, key)
        else:
            path = LOCAL_ROOT / key
            if path.exists():
                await asyncio.to_thread(path.unlink)

    async def presigned_url(self, key: str, expires_seconds: int = 900) -> str | None:
        if self._client is None:
            return None
        from datetime import timedelta

        return await asyncio.to_thread(
            self._client.presigned_get_object,
            settings.minio_bucket,
            key,
            timedelta(seconds=expires_seconds),
        )


store = ArtifactStore()
