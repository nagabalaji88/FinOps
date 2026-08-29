"""OpenAI-compatible Chat Completions provider.

Serves OpenAI, Azure OpenAI, Mistral, DeepSeek, Together (Llama) and Ollama, which
all expose the same wire format with different auth and base URLs.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import ProviderError, ProviderNotConfiguredError
from app.core.redaction import redact_provider_body
from app.core.runtime_config import runtime_config
from app.llm.base import LLMProvider, estimate_tokens, http_client, messages_to_openai, tools_to_openai
from app.llm.types import EmbeddingResult, LLMResponse, Message, StreamChunk, ToolCall, ToolSchema, Usage


class OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        name: str,
        *,
        base_url: str | None,
        api_key: str | None = None,
        auth_style: str = "bearer",
        azure_deployment_mode: bool = False,
        api_version: str | None = None,
        requires_key: bool = True,
    ):
        self.name = name
        self.base_url = (base_url or "").rstrip("/")
        self._static_key = api_key
        self.auth_style = auth_style
        self.azure_deployment_mode = azure_deployment_mode
        self.api_version = api_version
        self.requires_key = requires_key

    @property
    def api_key(self) -> str | None:
        """Resolved per call, so a key rotated in the admin view applies without a restart."""
        return runtime_config.api_key(self.name) or self._static_key

    @property
    def configured(self) -> bool:
        if not self.base_url:
            return False
        return bool(self.api_key) if self.requires_key else True

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            if self.auth_style == "api-key":
                headers["api-key"] = self.api_key
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _chat_url(self, model: str) -> str:
        if self.azure_deployment_mode:
            return (
                f"{self.base_url}/openai/deployments/{model}/chat/completions?api-version={self.api_version}"
            )
        return f"{self.base_url}/chat/completions"

    def _embed_url(self, model: str) -> str:
        if self.azure_deployment_mode:
            return f"{self.base_url}/openai/deployments/{model}/embeddings?api-version={self.api_version}"
        return f"{self.base_url}/embeddings"

    def _require(self) -> None:
        if not self.configured:
            raise ProviderNotConfiguredError(
                f"Provider '{self.name}' is not configured",
                details={"required": f"{self.name.upper()}_API_KEY and base URL"},
            )

    def _payload(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        temperature: float,
        max_tokens: int,
        top_p: float | None,
        stop: list[str] | None,
        json_mode: bool,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages_to_openai(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.azure_deployment_mode:
            payload.pop("model")
        if top_p is not None:
            payload["top_p"] = top_p
        if stop:
            payload["stop"] = stop
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        openai_tools = tools_to_openai(tools)
        if openai_tools:
            payload["tools"] = openai_tools
            payload["tool_choice"] = "auto"
        if extra:
            payload.update(extra)
        return payload

    @staticmethod
    def _parse_tool_calls(raw_calls: list[dict[str, Any]] | None) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for call in raw_calls or []:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(
                ToolCall(id=call.get("id") or str(uuid.uuid4()), name=fn.get("name", ""), arguments=args)
            )
        return calls

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
        payload = self._payload(
            model, messages, tools, temperature, max_tokens, top_p, stop, json_mode, extra
        )
        started = time.perf_counter()
        try:
            resp = await http_client().post(self._chat_url(model), headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name} transport error: {exc}") from exc
        latency = (time.perf_counter() - started) * 1000
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.name} returned {resp.status_code}: {redact_provider_body(resp.text)}",
                details={"model": model},
                provider_status=resp.status_code,
            )
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        usage_raw = data.get("usage") or {}
        details = usage_raw.get("prompt_tokens_details") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("prompt_tokens", 0)),
            output_tokens=int(usage_raw.get("completion_tokens", 0)),
            cached_input_tokens=int(details.get("cached_tokens", 0)),
        )
        content = message.get("content") or ""
        if not usage.input_tokens:
            usage.input_tokens = sum(estimate_tokens(m.content or "") for m in messages)
        if not usage.output_tokens:
            usage.output_tokens = estimate_tokens(content)
        return LLMResponse(
            content=content,
            model=data.get("model", model),
            provider=self.name,
            usage=usage,
            tool_calls=self._parse_tool_calls(message.get("tool_calls")),
            finish_reason=choice.get("finish_reason") or "stop",
            latency_ms=latency,
            request_id=resp.headers.get("x-request-id") or data.get("id"),
            raw={"system_fingerprint": data.get("system_fingerprint")},
        )

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
        self._require()
        payload = self._payload(
            model,
            messages,
            tools,
            temperature,
            max_tokens,
            kwargs.get("top_p"),
            kwargs.get("stop"),
            kwargs.get("json_mode", False),
            kwargs.get("extra"),
        )
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        started = time.perf_counter()
        content_parts: list[str] = []
        tool_buffers: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish_reason = "stop"
        request_id: str | None = None

        async with http_client().stream(
            "POST", self._chat_url(model), headers=self._headers(), json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = redact_provider_body(await resp.aread())
                raise ProviderError(
                    f"{self.name} stream failed {resp.status_code}: {body}", provider_status=resp.status_code
                )
            request_id = resp.headers.get("x-request-id")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if event.get("usage"):
                    u = event["usage"]
                    usage.input_tokens = int(u.get("prompt_tokens", usage.input_tokens))
                    usage.output_tokens = int(u.get("completion_tokens", usage.output_tokens))
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    text = delta.get("content")
                    if text:
                        content_parts.append(text)
                        yield StreamChunk(delta=text)
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        buf = tool_buffers.setdefault(idx, {"id": tc.get("id"), "name": "", "args": ""})
                        if tc.get("id"):
                            buf["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            buf["name"] = fn["name"]
                        if fn.get("arguments"):
                            buf["args"] += fn["arguments"]

        content = "".join(content_parts)
        tool_calls: list[ToolCall] = []
        for buf in tool_buffers.values():
            try:
                args = json.loads(buf["args"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": buf["args"]}
            tool_calls.append(ToolCall(id=buf["id"] or str(uuid.uuid4()), name=buf["name"], arguments=args))
        if not usage.input_tokens:
            usage.input_tokens = sum(estimate_tokens(m.content or "") for m in messages)
        if not usage.output_tokens:
            usage.output_tokens = estimate_tokens(content)
        response = LLMResponse(
            content=content,
            model=model,
            provider=self.name,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            latency_ms=(time.perf_counter() - started) * 1000,
            request_id=request_id,
        )
        yield StreamChunk(done=True, response=response)

    async def embed(self, *, model: str, texts: list[str]) -> EmbeddingResult:
        self._require()
        started = time.perf_counter()
        payload: dict[str, Any] = {"input": texts}
        if not self.azure_deployment_mode:
            payload["model"] = model
        resp = await http_client().post(self._embed_url(model), headers=self._headers(), json=payload)
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.name} embedding failed {resp.status_code}: {redact_provider_body(resp.text)}",
                provider_status=resp.status_code,
            )
        data = resp.json()
        vectors = [item["embedding"] for item in sorted(data["data"], key=lambda d: d["index"])]
        usage_raw = data.get("usage") or {}
        return EmbeddingResult(
            vectors=vectors,
            model=data.get("model", model),
            provider=self.name,
            dimensions=len(vectors[0]) if vectors else 0,
            usage=Usage(
                input_tokens=int(usage_raw.get("prompt_tokens", 0) or sum(estimate_tokens(t) for t in texts))
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def list_models(self) -> list[str]:
        if not self.configured:
            return []
        resp = await http_client().get(f"{self.base_url}/models", headers=self._headers(), timeout=12.0)
        if resp.status_code >= 400:
            raise ProviderError(
                f"{self.name} model listing failed {resp.status_code}: {redact_provider_body(resp.text)}",
                provider_status=resp.status_code,
            )
        data = resp.json().get("data") or []
        return sorted({str(item.get("id")) for item in data if item.get("id")})

    async def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "provider": self.name,
            "configured": self.configured,
            "base_url": self.base_url,
        }
        if not self.configured:
            info["status"] = "not_configured"
            return info
        try:
            started = time.perf_counter()
            resp = await http_client().get(f"{self.base_url}/models", headers=self._headers(), timeout=8.0)
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            info["status"] = "healthy" if resp.status_code < 400 else "degraded"
            info["http_status"] = resp.status_code
        except Exception as exc:
            info["status"] = "unhealthy"
            info["error"] = str(exc)
        return info


def build_openai() -> OpenAICompatProvider:
    return OpenAICompatProvider("openai", base_url=settings.openai_base_url)


def build_azure_openai() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        "azure_openai",
        base_url=settings.azure_openai_endpoint,
        auth_style="api-key",
        azure_deployment_mode=True,
        api_version=settings.azure_openai_api_version,
    )


def build_mistral() -> OpenAICompatProvider:
    return OpenAICompatProvider("mistral", base_url=settings.mistral_base_url)


def build_deepseek() -> OpenAICompatProvider:
    return OpenAICompatProvider("deepseek", base_url=settings.deepseek_base_url)


def build_together() -> OpenAICompatProvider:
    return OpenAICompatProvider("together", base_url=settings.together_base_url)


def build_groq() -> OpenAICompatProvider:
    return OpenAICompatProvider("groq", base_url=settings.groq_base_url)


def build_openrouter() -> OpenAICompatProvider:
    return OpenAICompatProvider("openrouter", base_url=settings.openrouter_base_url)


def build_ollama() -> OpenAICompatProvider:
    base = settings.ollama_base_url
    return OpenAICompatProvider(
        "ollama", base_url=f"{base.rstrip('/')}/v1" if base else None, api_key=None, requires_key=False
    )
