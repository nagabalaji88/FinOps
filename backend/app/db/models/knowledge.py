"""Knowledge sources, documents, chunks with embeddings, and conversation memory."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDMixin


class KnowledgeSource(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "knowledge_sources"

    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(180))
    # sharepoint|jira|confluence|slack|teams|github|sql|s3|upload|web
    connector: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # connected|not_configured|error|syncing
    status: Mapped[str] = mapped_column(String(24), default="not_configured")
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(120), default=None)
    vector_dimensions: Mapped[int] = mapped_column(Integer, default=0)
    classification: Mapped[str] = mapped_column(String(40), default="internal")
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)

    documents: Mapped[list[Document]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class Document(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_doc_source_ext", "source_id", "external_id"),)

    source_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(255), default=None)
    title: Mapped[str] = mapped_column(String(500))
    uri: Mapped[str | None] = mapped_column(String(1000), default=None)
    mime_type: Mapped[str] = mapped_column(String(120), default="text/plain")
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    author: Mapped[str | None] = mapped_column(String(255), default=None)
    classification: Mapped[str] = mapped_column(String(40), default="internal")
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # pending|embedding|embedded|failed
    embedding_status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    artifact_uri: Mapped[str | None] = mapped_column(String(1000), default=None)

    source: Mapped[KnowledgeSource] = relationship(back_populates="documents")
    chunks: Mapped[list[Chunk]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunk_doc_index", "document_id", "chunk_index"),)

    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    source_key: Mapped[str] = mapped_column(String(80), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    heading: Mapped[str | None] = mapped_column(String(500), default=None)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    embedding_model: Mapped[str | None] = mapped_column(String(120), default=None)
    dimensions: Mapped[int] = mapped_column(Integer, default=0)
    vector_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    document: Mapped[Document] = relationship(back_populates="chunks")


class SearchQueryLog(Base, UUIDMixin):
    __tablename__ = "search_query_logs"

    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    query: Mapped[str] = mapped_column(Text)
    agent_key: Mapped[str | None] = mapped_column(String(80), default=None)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    collection: Mapped[str] = mapped_column(String(120), default="finops_knowledge")
    backend: Mapped[str] = mapped_column(String(32), default="internal")
    top_k: Mapped[int] = mapped_column(Integer, default=5)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    top_score: Mapped[float | None] = mapped_column(Float, default=None)


class MemoryThread(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "memory_threads"

    thread_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    agent_key: Mapped[str] = mapped_column(String(80), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(120), index=True, default=None)
    title: Mapped[str] = mapped_column(String(300), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    sentiment: Mapped[str | None] = mapped_column(String(24), default=None)
    sentiment_score: Mapped[float | None] = mapped_column(Float, default=None)
    facts: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_activity_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)

    messages: Mapped[list[MemoryMessage]] = relationship(
        back_populates="thread", cascade="all, delete-orphan", order_by="MemoryMessage.created_at"
    )


class MemoryMessage(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "memory_messages"

    thread_id: Mapped[str] = mapped_column(
        ForeignKey("memory_threads.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(24))  # user|assistant|tool|system
    content: Mapped[str] = mapped_column(Text)
    execution_id: Mapped[str | None] = mapped_column(String(36), default=None)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    msg_metadata: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    thread: Mapped[MemoryThread] = relationship(back_populates="messages")
