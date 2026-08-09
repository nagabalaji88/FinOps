"""Enterprise knowledge tools: RAG search, source listing, summarisation and connectors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.errors import NotFoundError, ValidationError
from app.db.models.knowledge import Chunk, Document, KnowledgeSource
from app.rag.pipeline import pipeline
from app.tools.base import ToolContext, tool


class SearchArgs(BaseModel):
    query: str
    top_k: int = Field(default=6, ge=1, le=25)
    sources: list[str] = Field(default_factory=list, description="Restrict to knowledge source keys")
    min_score: float = Field(default=0.05, ge=0.0, le=1.0)


@tool(
    "search_knowledge_base",
    "Hybrid vector + keyword search across connected enterprise knowledge with citations.",
    SearchArgs,
    category="knowledge",
    timeout_seconds=60,
)
async def search_knowledge_base(args: SearchArgs, ctx: ToolContext) -> dict[str, Any]:
    result = await pipeline.search(
        ctx.session,
        args.query,
        top_k=args.top_k,
        source_keys=args.sources or None,
        min_score=args.min_score,
        agent_key=ctx.agent_key,
        execution_id=ctx.execution_id,
    )
    return {
        "query": args.query,
        "backend": result.backend,
        "embedding_model": result.embedding_model,
        "method": result.method,
        "latency_ms": round(result.latency_ms, 2),
        "result_count": len(result.matches),
        "citations": result.to_citations(),
        "results": [m.to_dict() for m in result.matches],
    }


class SourcesArgs(BaseModel):
    connector: str | None = None
    enabled_only: bool = True


@tool(
    "list_knowledge_sources",
    "List connected knowledge sources with sync status, document and chunk counts.",
    SourcesArgs,
    category="knowledge",
)
async def list_knowledge_sources(args: SourcesArgs, ctx: ToolContext) -> dict[str, Any]:
    stmt = select(KnowledgeSource)
    if args.connector:
        stmt = stmt.where(KnowledgeSource.connector == args.connector)
    if args.enabled_only:
        stmt = stmt.where(KnowledgeSource.enabled.is_(True))
    sources = (await ctx.session.execute(stmt)).scalars().all()
    return {
        "count": len(sources),
        "sources": [
            {
                "key": s.key,
                "name": s.name,
                "connector": s.connector,
                "status": s.status,
                "documents": s.document_count,
                "chunks": s.chunk_count,
                "embedding_model": s.embedding_model,
                "dimensions": s.vector_dimensions,
                "classification": s.classification,
                "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None,
            }
            for s in sources
        ],
    }


class DocumentArgs(BaseModel):
    document_id: str | None = None
    title_contains: str | None = None
    max_characters: int = Field(default=12000, ge=500, le=60000)


@tool(
    "fetch_document",
    "Fetch the full text of a knowledge document by id or title match.",
    DocumentArgs,
    category="knowledge",
)
async def fetch_document(args: DocumentArgs, ctx: ToolContext) -> dict[str, Any]:
    stmt = select(Document)
    if args.document_id:
        stmt = stmt.where(Document.id == args.document_id)
    elif args.title_contains:
        stmt = stmt.where(Document.title.ilike(f"%{args.title_contains}%"))
    else:
        raise ValidationError("Provide document_id or title_contains")
    document = (await ctx.session.execute(stmt.limit(1))).scalar_one_or_none()
    if document is None:
        raise NotFoundError("Document not found")
    return {
        "document_id": document.id,
        "title": document.title,
        "uri": document.uri,
        "author": document.author,
        "classification": document.classification,
        "mime_type": document.mime_type,
        "chunk_count": document.chunk_count,
        "token_count": document.token_count,
        "updated_at": document.updated_at.isoformat(),
        "content": document.content[: args.max_characters],
        "truncated": len(document.content) > args.max_characters,
    }


class StatsArgs(BaseModel):
    source_key: str | None = None


@tool(
    "knowledge_base_stats",
    "Return corpus statistics: documents, chunks, embedding coverage and vector dimensions.",
    StatsArgs,
    category="knowledge",
)
async def knowledge_base_stats(args: StatsArgs, ctx: ToolContext) -> dict[str, Any]:
    doc_stmt = select(func.count()).select_from(Document)
    chunk_stmt = select(func.count()).select_from(Chunk)
    if args.source_key:
        source = (
            await ctx.session.execute(select(KnowledgeSource).where(KnowledgeSource.key == args.source_key))
        ).scalar_one_or_none()
        if source is None:
            raise NotFoundError(f"Knowledge source '{args.source_key}' not found")
        doc_stmt = doc_stmt.where(Document.source_id == source.id)
        chunk_stmt = chunk_stmt.where(Chunk.source_key == source.key)

    documents = int((await ctx.session.execute(doc_stmt)).scalar_one())
    chunks = int((await ctx.session.execute(chunk_stmt)).scalar_one())
    embedded = int(
        (
            await ctx.session.execute(
                select(func.count()).select_from(Chunk).where(Chunk.embedding.is_not(None))
            )
        ).scalar_one()
    )
    dimensions = (
        await ctx.session.execute(select(Chunk.dimensions).where(Chunk.dimensions > 0).limit(1))
    ).scalar_one_or_none()
    return {
        "source_key": args.source_key,
        "documents": documents,
        "chunks": chunks,
        "embedded_chunks": embedded,
        "embedding_coverage_pct": round(embedded / chunks * 100, 2) if chunks else 0.0,
        "vector_dimensions": dimensions or 0,
        "as_of": datetime.now(UTC).isoformat(),
    }


class SummariseArgs(BaseModel):
    document_id: str | None = None
    text: str | None = None
    style: str = Field(default="executive", description="executive|technical|bullet")
    max_words: int = Field(default=250, ge=50, le=1500)


@tool(
    "summarise_document",
    "Summarise a stored document or supplied text with the configured LLM.",
    SummariseArgs,
    category="knowledge",
    timeout_seconds=120,
)
async def summarise_document(args: SummariseArgs, ctx: ToolContext) -> dict[str, Any]:
    from app.llm.router import router
    from app.llm.types import Message

    content = args.text
    title = None
    if args.document_id:
        document = (
            await ctx.session.execute(select(Document).where(Document.id == args.document_id))
        ).scalar_one_or_none()
        if document is None:
            raise NotFoundError("Document not found")
        content = document.content
        title = document.title
    if not content:
        raise ValidationError("Provide document_id or text")

    styles = {
        "executive": "Write for a senior executive: outcomes, decisions and risks first.",
        "technical": "Write for an engineer: architecture, interfaces, constraints and gotchas.",
        "bullet": "Return terse bullet points only.",
    }
    response = await router.chat(
        messages=[
            Message(
                role="system",
                content=f"You summarise enterprise documents accurately with no invented "
                f"facts. {styles.get(args.style, styles['executive'])} "
                f"Maximum {args.max_words} words.",
            ),
            Message(role="user", content=f"Title: {title or 'Untitled'}\n\n{content[:40000]}"),
        ],
        temperature=0.1,
        max_tokens=min(args.max_words * 3, 2000),
        context={"execution_id": ctx.execution_id, "agent_key": ctx.agent_key, "user_email": ctx.user_email},
    )
    return {
        "document_id": args.document_id,
        "title": title,
        "style": args.style,
        "summary": response.content,
        "model": response.model,
        "tokens": {"input": response.usage.input_tokens, "output": response.usage.output_tokens},
        "cost_usd": round(response.cost_usd, 6),
    }
