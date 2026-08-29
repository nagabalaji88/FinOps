"""Embedding computation.

Primary path: the configured embedding model through the router (OpenAI, Azure,
Gemini, Bedrock Titan, Ollama/Nomic).

Fallback path: a local hashing embedder (feature hashing over word unigrams and
bigrams with sublinear TF and L2 normalisation). This is a real, deterministic
vectoriser -- not a stub -- so retrieval keeps working with zero external providers,
at lower semantic quality. The active method is always reported to the caller.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

import numpy as np

from app.core.config import settings
from app.core.logging import get_logger
from app.llm.router import router

log = get_logger("embeddings")

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'&/.-]*")
LOCAL_DIMENSIONS = 768


@dataclass(slots=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    model: str
    provider: str
    dimensions: int
    tokens: int
    cost_usd: float
    latency_ms: float
    method: str  # provider | local_hashing


def _tokenise(text: str) -> list[str]:
    words = TOKEN_RE.findall(text.lower())
    bigrams = [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
    return words + bigrams


def local_embed(texts: list[str], dimensions: int = LOCAL_DIMENSIONS) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        vec = np.zeros(dimensions, dtype=np.float32)
        counts: dict[str, int] = {}
        for token in _tokenise(text):
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign * (1.0 + math.log(count))
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        vectors.append(vec.tolist())
    return vectors


async def embed_texts(
    texts: list[str], *, model: str | None = None, context: dict | None = None
) -> EmbeddingBatch:
    if not texts:
        return EmbeddingBatch([], "none", "none", 0, 0, 0.0, 0.0, "local_hashing")

    model = model or settings.default_embedding_model
    if router.available_models(embeddings=True):
        try:
            result = await router.embed(texts=texts, model=model, context=context)
            return EmbeddingBatch(
                vectors=result.vectors,
                model=result.model,
                provider=result.provider,
                dimensions=result.dimensions,
                tokens=result.usage.input_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                method="provider",
            )
        except Exception as exc:
            log.warning("embedding_provider_failed_using_local", error=str(exc), model=model)

    import time

    started = time.perf_counter()
    vectors = local_embed(texts)
    return EmbeddingBatch(
        vectors=vectors,
        model="local-hashing-768",
        provider="internal",
        dimensions=LOCAL_DIMENSIONS,
        tokens=sum(max(1, len(t) // 4) for t in texts),
        cost_usd=0.0,
        latency_ms=(time.perf_counter() - started) * 1000,
        method="local_hashing",
    )


def active_dimensions() -> int:
    if router.available_models(embeddings=True):
        from app.llm.catalog import resolve_model

        spec = resolve_model(settings.default_embedding_model)
        if spec and spec.dimensions:
            return spec.dimensions
    return LOCAL_DIMENSIONS
