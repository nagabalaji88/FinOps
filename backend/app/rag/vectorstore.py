"""Vector store abstraction.

``QdrantStore`` is used when QDRANT_URL is configured; otherwise ``PostgresStore``
keeps vectors in the ``chunks`` table as float32 buffers and performs exact cosine
search with NumPy. Both are real search paths -- the fallback is exact rather than
approximate, which is slower on very large corpora but never wrong.
"""

from __future__ import annotations

import abc
import struct
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.metrics import vector_search_latency
from app.db.models.knowledge import Chunk, Document

log = get_logger("vectorstore")


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


@dataclass(slots=True)
class VectorMatch:
    chunk_id: str
    document_id: str
    source_key: str
    content: str
    score: float
    heading: str | None = None
    title: str | None = None
    uri: str | None = None
    chunk_index: int = 0
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "source": self.source_key,
            "title": self.title,
            "heading": self.heading,
            "uri": self.uri,
            "chunk_index": self.chunk_index,
            "score": round(self.score, 5),
            "content": self.content,
            "metadata": self.metadata or {},
        }


class VectorStore(abc.ABC):
    backend: str

    @abc.abstractmethod
    async def upsert(self, session: AsyncSession, chunks: list[Chunk]) -> int: ...

    @abc.abstractmethod
    async def search(
        self,
        session: AsyncSession,
        query_vector: list[float],
        *,
        top_k: int = 5,
        source_keys: list[str] | None = None,
        min_score: float = 0.0,
    ) -> list[VectorMatch]: ...

    async def delete_document(self, session: AsyncSession, document_id: str) -> None:  # noqa: B027
        return None

    async def health(self) -> dict[str, Any]:
        return {"backend": self.backend, "status": "healthy"}


class PostgresStore(VectorStore):
    """Exact cosine search over vectors stored alongside their chunks."""

    backend = "database"

    async def upsert(self, session: AsyncSession, chunks: list[Chunk]) -> int:
        # Vectors are written on the Chunk rows themselves by the pipeline.
        return len(chunks)

    async def search(
        self,
        session: AsyncSession,
        query_vector: list[float],
        *,
        top_k: int = 5,
        source_keys: list[str] | None = None,
        min_score: float = 0.0,
    ) -> list[VectorMatch]:
        started = time.perf_counter()
        stmt = select(Chunk, Document).join(Document, Chunk.document_id == Document.id).where(
            Chunk.embedding.is_not(None)
        )
        if source_keys:
            stmt = stmt.where(Chunk.source_key.in_(source_keys))
        rows = (await session.execute(stmt)).all()
        if not rows:
            return []

        query = np.asarray(query_vector, dtype="<f4")
        query_norm = float(np.linalg.norm(query)) or 1.0
        matches: list[VectorMatch] = []
        for chunk, document in rows:
            vector = unpack_vector(chunk.embedding)
            if vector.shape[0] != query.shape[0]:
                continue
            denom = (float(np.linalg.norm(vector)) or 1.0) * query_norm
            score = float(np.dot(vector, query) / denom)
            if score < min_score:
                continue
            matches.append(
                VectorMatch(
                    chunk_id=chunk.id, document_id=chunk.document_id, source_key=chunk.source_key,
                    content=chunk.content, score=score, heading=chunk.heading,
                    title=document.title, uri=document.uri, chunk_index=chunk.chunk_index,
                    metadata={**(chunk.chunk_metadata or {}),
                              "classification": document.classification},
                )
            )
        matches.sort(key=lambda m: m.score, reverse=True)
        vector_search_latency.labels(collection="database").observe(time.perf_counter() - started)
        return matches[:top_k]


class QdrantStore(VectorStore):
    backend = "qdrant"

    def __init__(self) -> None:
        self._client: Any | None = None
        self._dimensions = 0

    async def connect(self, dimensions: int) -> bool:
        try:
            from qdrant_client import AsyncQdrantClient
            from qdrant_client.models import Distance, VectorParams

            client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
            collections = await client.get_collections()
            names = {c.name for c in collections.collections}
            if settings.qdrant_collection not in names:
                await client.create_collection(
                    collection_name=settings.qdrant_collection,
                    vectors_config=VectorParams(size=dimensions, distance=Distance.COSINE),
                )
            self._client = client
            self._dimensions = dimensions
            log.info("qdrant_connected", collection=settings.qdrant_collection, dims=dimensions)
            return True
        except Exception as exc:
            log.warning("qdrant_unavailable", error=str(exc))
            return False

    async def upsert(self, session: AsyncSession, chunks: list[Chunk]) -> int:
        if self._client is None or not chunks:
            return 0
        from qdrant_client.models import PointStruct

        points = []
        for chunk in chunks:
            if not chunk.embedding:
                continue
            points.append(
                PointStruct(
                    id=chunk.id,
                    vector=unpack_vector(chunk.embedding).tolist(),
                    payload={
                        "document_id": chunk.document_id,
                        "source_key": chunk.source_key,
                        "chunk_index": chunk.chunk_index,
                        "heading": chunk.heading,
                        "content": chunk.content,
                        **(chunk.chunk_metadata or {}),
                    },
                )
            )
        if points:
            await self._client.upsert(collection_name=settings.qdrant_collection, points=points)
        return len(points)

    async def search(
        self,
        session: AsyncSession,
        query_vector: list[float],
        *,
        top_k: int = 5,
        source_keys: list[str] | None = None,
        min_score: float = 0.0,
    ) -> list[VectorMatch]:
        if self._client is None:
            return []
        from qdrant_client.models import FieldCondition, Filter, MatchAny

        started = time.perf_counter()
        query_filter = (
            Filter(must=[FieldCondition(key="source_key", match=MatchAny(any=source_keys))])
            if source_keys
            else None
        )
        results = await self._client.search(
            collection_name=settings.qdrant_collection,
            query_vector=query_vector,
            limit=top_k,
            query_filter=query_filter,
            score_threshold=min_score or None,
        )
        vector_search_latency.labels(collection=settings.qdrant_collection).observe(
            time.perf_counter() - started
        )
        matches = []
        for point in results:
            payload = point.payload or {}
            matches.append(
                VectorMatch(
                    chunk_id=str(point.id),
                    document_id=payload.get("document_id", ""),
                    source_key=payload.get("source_key", ""),
                    content=payload.get("content", ""),
                    score=float(point.score),
                    heading=payload.get("heading"),
                    chunk_index=int(payload.get("chunk_index", 0)),
                    metadata=payload,
                )
            )
        doc_ids = {m.document_id for m in matches if m.document_id}
        if doc_ids:
            docs = (await session.execute(select(Document).where(Document.id.in_(doc_ids)))).scalars()
            titles = {d.id: (d.title, d.uri) for d in docs}
            for m in matches:
                if m.document_id in titles:
                    m.title, m.uri = titles[m.document_id]
        return matches

    async def delete_document(self, session: AsyncSession, document_id: str) -> None:
        if self._client is None:
            return
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        await self._client.delete(
            collection_name=settings.qdrant_collection,
            points_selector=Filter(
                must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))]
            ),
        )

    async def health(self) -> dict[str, Any]:
        if self._client is None:
            return {"backend": self.backend, "status": "not_configured"}
        try:
            info = await self._client.get_collection(settings.qdrant_collection)
            return {
                "backend": self.backend,
                "status": "healthy",
                "collection": settings.qdrant_collection,
                "points": info.points_count,
                "dimensions": self._dimensions,
            }
        except Exception as exc:
            return {"backend": self.backend, "status": "unhealthy", "error": str(exc)}


_store: VectorStore = PostgresStore()


async def init_vector_store(dimensions: int = 1536) -> VectorStore:
    global _store
    if settings.qdrant_url:
        qdrant = QdrantStore()
        if await qdrant.connect(dimensions):
            _store = qdrant
            return _store
    _store = PostgresStore()
    return _store


def get_vector_store() -> VectorStore:
    return _store
