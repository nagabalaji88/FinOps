"""RAG pipeline: ingest -> chunk -> embed -> index, and hybrid retrieval with citations."""

from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.knowledge import Chunk, Document, KnowledgeSource, SearchQueryLog
from app.rag.chunking import chunk_text
from app.rag.embeddings import embed_texts
from app.rag.vectorstore import VectorMatch, get_vector_store, pack_vector

log = get_logger("rag")

WORD_RE = re.compile(r"[a-z0-9']+")
STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "is",
    "are",
    "for",
    "on",
    "with",
    "as",
    "by",
    "at",
    "from",
    "that",
    "this",
    "it",
    "be",
    "we",
    "our",
    "you",
    "your",
    "what",
    "how",
    "which",
    "when",
    "who",
    "can",
    "do",
    "does",
    "if",
    "not",
}


@dataclass(slots=True)
class RetrievedContext:
    matches: list[VectorMatch]
    latency_ms: float
    method: str
    embedding_model: str
    backend: str
    query: str

    def to_citations(self) -> list[dict[str, Any]]:
        return [
            {
                "id": f"[{i + 1}]",
                "title": m.title or "Untitled",
                "source": m.source_key,
                "uri": m.uri,
                "chunk_index": m.chunk_index,
                "score": round(m.score, 4),
                "excerpt": m.content[:320],
            }
            for i, m in enumerate(self.matches)
        ]

    def as_prompt_block(self, max_chars: int = 12000) -> str:
        parts: list[str] = []
        used = 0
        for i, m in enumerate(self.matches):
            header = f"[{i + 1}] {m.title or 'Untitled'}"
            if m.heading:
                header += f" > {m.heading}"
            header += f" (source: {m.source_key})"
            body = m.content.strip()
            block = f"{header}\n{body}"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n---\n\n".join(parts)


def _keywords(text: str) -> list[str]:
    return [w for w in WORD_RE.findall(text.lower()) if w not in STOPWORDS and len(w) > 2]


def _bm25_like_score(query_terms: list[str], content: str, avg_len: float) -> float:
    """BM25 scoring with per-document term frequencies (k1=1.5, b=0.75)."""
    if not query_terms:
        return 0.0
    doc_terms = WORD_RE.findall(content.lower())
    if not doc_terms:
        return 0.0
    length = len(doc_terms)
    freqs: dict[str, int] = {}
    for t in doc_terms:
        freqs[t] = freqs.get(t, 0) + 1
    k1, b = 1.5, 0.75
    score = 0.0
    for term in query_terms:
        tf = freqs.get(term, 0)
        if not tf:
            continue
        denom = tf + k1 * (1 - b + b * length / max(avg_len, 1.0))
        score += (tf * (k1 + 1)) / denom
    return score / math.sqrt(len(query_terms))


class RagPipeline:
    """Ingestion and retrieval over the knowledge corpus."""

    async def ingest_document(
        self,
        session: AsyncSession,
        *,
        source: KnowledgeSource,
        title: str,
        content: str,
        uri: str | None = None,
        external_id: str | None = None,
        mime_type: str = "text/markdown",
        author: str | None = None,
        classification: str = "internal",
        metadata: dict[str, Any] | None = None,
        max_tokens: int = 512,
        overlap_tokens: int = 64,
    ) -> Document:
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        existing: Document | None = None
        if external_id:
            existing = (
                await session.execute(
                    select(Document).where(
                        Document.source_id == source.id, Document.external_id == external_id
                    )
                )
            ).scalar_one_or_none()

        if existing and existing.content_hash == content_hash:
            return existing

        if existing:
            await session.execute(delete(Chunk).where(Chunk.document_id == existing.id))
            await get_vector_store().delete_document(session, existing.id)
            document = existing
            document.version += 1
        else:
            document = Document(source_id=source.id, external_id=external_id)
            session.add(document)

        document.title = title[:500]
        document.uri = uri
        document.mime_type = mime_type
        document.content = content
        document.content_hash = content_hash
        document.author = author
        document.classification = classification
        document.doc_metadata = metadata or {}
        document.embedding_status = "embedding"
        document.token_count = max(1, len(content) // 4)
        await session.flush()

        pieces = chunk_text(content, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
        if not pieces:
            document.embedding_status = "embedded"
            document.chunk_count = 0
            return document

        batch = await embed_texts(
            [p.content for p in pieces], context={"source": source.key, "document_id": document.id}
        )
        chunks: list[Chunk] = []
        for piece, vector in zip(pieces, batch.vectors, strict=False):
            chunk = Chunk(
                document_id=document.id,
                source_key=source.key,
                chunk_index=piece.index,
                content=piece.content,
                token_count=piece.token_count,
                heading=piece.heading,
                embedding=pack_vector(vector),
                embedding_model=batch.model,
                dimensions=batch.dimensions,
                chunk_metadata={"title": title, "uri": uri, "method": batch.method},
            )
            chunks.append(chunk)
            session.add(chunk)
        await session.flush()
        await get_vector_store().upsert(session, chunks)

        document.chunk_count = len(chunks)
        document.embedding_status = "embedded"

        source.document_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Document).where(Document.source_id == source.id)
                )
            ).scalar_one()
        )
        source.chunk_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Chunk).where(Chunk.source_key == source.key)
                )
            ).scalar_one()
        )
        source.embedding_model = batch.model
        source.vector_dimensions = batch.dimensions
        source.last_sync_at = datetime.now(UTC)
        source.status = "connected"
        return document

    async def search(
        self,
        session: AsyncSession,
        query: str,
        *,
        top_k: int = 5,
        source_keys: list[str] | None = None,
        min_score: float = 0.05,
        hybrid: bool = True,
        agent_key: str | None = None,
        execution_id: str | None = None,
    ) -> RetrievedContext:
        started = time.perf_counter()
        batch = await embed_texts([query], context={"agent": agent_key, "execution_id": execution_id})
        store = get_vector_store()
        vector_matches = await store.search(
            session,
            batch.vectors[0] if batch.vectors else [],
            top_k=max(top_k * 4, 20),
            source_keys=source_keys,
            min_score=0.0,
        )

        matches = vector_matches
        if hybrid:
            terms = _keywords(query)
            if terms:
                lengths = [len(WORD_RE.findall(m.content.lower())) for m in vector_matches] or [1]
                avg_len = sum(lengths) / len(lengths)
                keyword_pool = await self._keyword_candidates(session, terms, source_keys, top_k * 4)
                seen = {m.chunk_id for m in vector_matches}
                for match in keyword_pool:
                    if match.chunk_id not in seen:
                        vector_matches.append(match)
                        seen.add(match.chunk_id)
                scored: list[tuple[float, VectorMatch]] = []
                max_bm25 = 0.0
                bm25_scores = {}
                for m in vector_matches:
                    bm = _bm25_like_score(terms, m.content, avg_len)
                    bm25_scores[m.chunk_id] = bm
                    max_bm25 = max(max_bm25, bm)
                for m in vector_matches:
                    normalised_bm = (bm25_scores[m.chunk_id] / max_bm25) if max_bm25 else 0.0
                    combined = 0.65 * max(m.score, 0.0) + 0.35 * normalised_bm
                    m.metadata = {
                        **(m.metadata or {}),
                        "vector_score": round(m.score, 4),
                        "keyword_score": round(normalised_bm, 4),
                    }
                    m.score = combined
                    scored.append((combined, m))
                scored.sort(key=lambda x: x[0], reverse=True)
                matches = [m for _, m in scored]

        matches = [m for m in matches if m.score >= min_score][:top_k]
        latency_ms = (time.perf_counter() - started) * 1000

        session.add(
            SearchQueryLog(
                timestamp=datetime.now(UTC),
                query=query[:2000],
                agent_key=agent_key,
                execution_id=execution_id,
                collection=store.backend,
                backend=store.backend,
                top_k=top_k,
                result_count=len(matches),
                latency_ms=latency_ms,
                top_score=matches[0].score if matches else None,
            )
        )
        return RetrievedContext(
            matches=matches,
            latency_ms=latency_ms,
            method=batch.method,
            embedding_model=batch.model,
            backend=store.backend,
            query=query,
        )

    async def _keyword_candidates(
        self, session: AsyncSession, terms: list[str], source_keys: list[str] | None, limit: int
    ) -> list[VectorMatch]:
        stmt = select(Chunk, Document).join(Document, Chunk.document_id == Document.id)
        if source_keys:
            stmt = stmt.where(Chunk.source_key.in_(source_keys))
        clause: ColumnElement[bool] | None = None
        for term in terms[:6]:
            cond = Chunk.content.ilike(f"%{term}%")
            clause = cond if clause is None else (clause | cond)
        if clause is not None:
            stmt = stmt.where(clause)
        rows = (await session.execute(stmt.limit(limit))).all()
        return [
            VectorMatch(
                chunk_id=c.id,
                document_id=c.document_id,
                source_key=c.source_key,
                content=c.content,
                score=0.0,
                heading=c.heading,
                title=d.title,
                uri=d.uri,
                chunk_index=c.chunk_index,
                metadata={**(c.chunk_metadata or {}), "classification": d.classification},
            )
            for c, d in rows
        ]

    async def reindex_source(self, session: AsyncSession, source: KnowledgeSource) -> dict[str, Any]:
        docs = (
            (await session.execute(select(Document).where(Document.source_id == source.id))).scalars().all()
        )
        reindexed = 0
        for doc in docs:
            await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
            await self.ingest_document(
                session,
                source=source,
                title=doc.title,
                content=doc.content,
                uri=doc.uri,
                external_id=doc.external_id,
                mime_type=doc.mime_type,
                author=doc.author,
                classification=doc.classification,
                metadata=doc.doc_metadata,
            )
            reindexed += 1
        return {"source": source.key, "documents_reindexed": reindexed}


pipeline = RagPipeline()
