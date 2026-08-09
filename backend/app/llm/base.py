"""Base provider contract and shared HTTP client."""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import settings
from app.llm.types import EmbeddingResult, LLMResponse, Message, StreamChunk, ToolSchema

_client: httpx.AsyncClient | None = None


def http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
            follow_redirects=True,
        )
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


class LLMProvider(abc.ABC):
    name: str

    @property
    @abc.abstractmethod
    def configured(self) -> bool:
        """True when credentials/endpoints required by this provider are present."""

    @abc.abstractmethod
    async def chat(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        top_p: float | None = None,
        stop: list[str] | None = None,
        json_mode: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> LLMResponse: ...

    async def stream(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        """Default: providers that do not implement streaming yield one final chunk."""
        response = await self.chat(
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        yield StreamChunk(delta=response.content)
        yield StreamChunk(done=True, response=response)

    async def embed(self, *, model: str, texts: list[str]) -> EmbeddingResult:
        raise NotImplementedError(f"{self.name} does not provide embeddings")

    async def health(self) -> dict[str, Any]:
        return {"provider": self.name, "configured": self.configured}


def messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
            continue
        entry: dict[str, Any] = {"role": m.role, "content": m.content or ""}
        if m.tool_calls:
            import json as _json

            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": _json.dumps(tc.arguments)},
                }
                for tc in m.tool_calls
            ]
            entry["content"] = m.content or None
        out.append(entry)
    return out


def tools_to_openai(tools: list[ToolSchema] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in tools
    ]


def estimate_tokens(text: str) -> int:
    """Character-based estimate used only when a provider omits usage accounting."""
    return max(1, len(text) // 4)
