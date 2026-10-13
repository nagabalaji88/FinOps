"""Google Gemini (generativelanguage) provider with function calling and embeddings."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import ProviderError, ProviderNotConfiguredError
from app.core.redaction import redact_provider_body
from app.core.runtime_config import runtime_config
from app.llm.base import LLMProvider, estimate_tokens, http_client
from app.llm.types import EmbeddingResult, LLMResponse, Message, ToolCall, ToolSchema, Usage


def _to_contents(messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
    system: list[str] = []
    contents: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            system.append(m.content)
        elif m.role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": m.name or "tool",
                                "response": {"result": m.content},
                            }
                        }
                    ],
                }
            )
        elif m.role == "assistant":
            parts: list[dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            parts.extend({"functionCall": {"name": tc.name, "args": tc.arguments}} for tc in m.tool_calls)
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        else:
            contents.append({"role": "user", "parts": [{"text": m.content}]})
    return ("\n\n".join(system) or None), contents


class GoogleProvider(LLMProvider):
    name = "google"

    @property
    def api_key(self) -> str | None:
        """Resolved per call, so a key set in the admin view applies without a restart."""
        return runtime_config.api_key("google")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _require(self) -> None:
        if not self.configured:
            raise ProviderNotConfiguredError(
                "Google Gemini provider is not configured", details={"required": "GOOGLE_API_KEY"}
            )

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
    ) -> LLMResponse:
        self._require()
        system, contents = _to_contents(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if top_p is not None:
            payload["generationConfig"]["topP"] = top_p
        if stop:
            payload["generationConfig"]["stopSequences"] = stop
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {"name": t.name, "description": t.description, "parameters": t.parameters}
                        for t in tools
                    ]
                }
            ]
        if extra:
            payload.update(extra)

        url = f"{settings.google_base_url}/models/{model}:generateContent"
        started = time.perf_counter()
        try:
            resp = await http_client().post(
                url,
                params={"key": self.api_key},
                headers={"Content-Type": "application/json"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini transport error: {exc}") from exc
        latency = (time.perf_counter() - started) * 1000
        if resp.status_code >= 400:
            raise ProviderError(
                f"Gemini returned {resp.status_code}: {redact_provider_body(resp.text)}",
                details={"model": model},
                provider_status=resp.status_code,
            )
        data = resp.json()
        candidate = (data.get("candidates") or [{}])[0]
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for part in (candidate.get("content") or {}).get("parts", []):
            if "text" in part:
                text_parts.append(part["text"])
            if "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append(
                    ToolCall(id=str(uuid.uuid4()), name=fc.get("name", ""), arguments=fc.get("args") or {})
                )
        usage_raw = data.get("usageMetadata") or {}
        content = "".join(text_parts)
        usage = Usage(
            input_tokens=int(usage_raw.get("promptTokenCount", 0))
            or sum(estimate_tokens(m.content or "") for m in messages),
            output_tokens=int(usage_raw.get("candidatesTokenCount", 0)) or estimate_tokens(content),
            cached_input_tokens=int(usage_raw.get("cachedContentTokenCount", 0)),
        )
        return LLMResponse(
            content=content,
            model=model,
            provider=self.name,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=(candidate.get("finishReason") or "STOP").lower(),
            latency_ms=latency,
        )

    async def embed(self, *, model: str, texts: list[str]) -> EmbeddingResult:
        self._require()
        started = time.perf_counter()
        url = f"{settings.google_base_url}/models/{model}:batchEmbedContents"
        payload = {
            "requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]}} for t in texts]
        }
        resp = await http_client().post(url, params={"key": self.api_key}, json=payload)
        if resp.status_code >= 400:
            raise ProviderError(
                f"Gemini embedding failed {resp.status_code}: {redact_provider_body(resp.text)}",
                provider_status=resp.status_code,
            )
        data = resp.json()
        vectors = [e["values"] for e in data.get("embeddings", [])]
        return EmbeddingResult(
            vectors=vectors,
            model=model,
            provider=self.name,
            dimensions=len(vectors[0]) if vectors else 0,
            usage=Usage(input_tokens=sum(estimate_tokens(t) for t in texts)),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def list_models(self) -> list[str]:
        if not self.configured:
            return []
        resp = await http_client().get(
            f"{settings.google_base_url}/models", params={"key": self.api_key}, timeout=12.0
        )
        if resp.status_code >= 400:
            raise ProviderError(
                f"Gemini model listing failed {resp.status_code}: {redact_provider_body(resp.text)}",
                provider_status=resp.status_code,
            )
        # Gemini returns "models/gemini-3.1-pro-preview"; the bare id is what the catalogue uses.
        names = [str(m.get("name", "")) for m in resp.json().get("models") or []]
        return sorted({n.removeprefix("models/") for n in names if n})

    async def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {"provider": self.name, "configured": self.configured}
        if not self.configured:
            info["status"] = "not_configured"
            return info
        try:
            started = time.perf_counter()
            resp = await http_client().get(
                f"{settings.google_base_url}/models", params={"key": self.api_key}, timeout=8.0
            )
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            info["status"] = "healthy" if resp.status_code < 400 else "degraded"
            info["http_status"] = resp.status_code
        except Exception as exc:
            info["status"] = "unhealthy"
            info["error"] = str(exc)
        return info
