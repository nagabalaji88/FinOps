"""Anthropic Messages API provider with tool use and SSE streaming."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import ProviderError, ProviderNotConfiguredError
from app.llm.base import LLMProvider, estimate_tokens, http_client
from app.llm.types import LLMResponse, Message, StreamChunk, ToolCall, ToolSchema, Usage

API_VERSION = "2023-06-01"


def _split_messages(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        elif m.role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": m.content,
                        }
                    ],
                }
            )
        elif m.role == "assistant" and m.tool_calls:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks.extend(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                for tc in m.tool_calls
            )
            converted.append({"role": "assistant", "content": blocks})
        else:
            converted.append({"role": m.role, "content": m.content or ""})
    # Anthropic requires alternating roles starting with user.
    if converted and converted[0]["role"] == "assistant":
        converted.insert(0, {"role": "user", "content": "(continuing prior conversation)"})
    return "\n\n".join(system_parts), converted


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    @property
    def configured(self) -> bool:
        return bool(settings.anthropic_api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": settings.anthropic_api_key or "",
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def _require(self) -> None:
        if not self.configured:
            raise ProviderNotConfiguredError(
                "Anthropic provider is not configured", details={"required": "ANTHROPIC_API_KEY"}
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
    ) -> dict[str, Any]:
        system, converted = _split_messages(messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        if top_p is not None:
            payload["top_p"] = top_p
        if stop:
            payload["stop_sequences"] = stop
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        return payload

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
        payload = self._payload(model, messages, tools, temperature, max_tokens, top_p, stop)
        if json_mode and not tools:
            payload["system"] = (
                payload.get("system", "") + "\n\nRespond with a single valid JSON object and nothing else."
            ).strip()
        if extra:
            payload.update(extra)
        started = time.perf_counter()
        try:
            resp = await http_client().post(
                f"{settings.anthropic_base_url}/v1/messages", headers=self._headers(), json=payload
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic transport error: {exc}") from exc
        latency = (time.perf_counter() - started) * 1000
        if resp.status_code >= 400:
            raise ProviderError(
                f"Anthropic returned {resp.status_code}",
                details={"body": resp.text[:1200], "model": model},
            )
        data = resp.json()
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", str(uuid.uuid4())),
                        name=block.get("name", ""),
                        arguments=block.get("input") or {},
                    )
                )
        usage_raw = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("input_tokens", 0)),
            output_tokens=int(usage_raw.get("output_tokens", 0)),
            cached_input_tokens=int(usage_raw.get("cache_read_input_tokens", 0)),
        )
        return LLMResponse(
            content="".join(text_parts),
            model=data.get("model", model),
            provider=self.name,
            usage=usage,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason") or "stop",
            latency_ms=latency,
            request_id=resp.headers.get("request-id") or data.get("id"),
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
            model, messages, tools, temperature, max_tokens, kwargs.get("top_p"), kwargs.get("stop")
        )
        payload["stream"] = True
        started = time.perf_counter()
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        current_tool: dict[str, Any] | None = None
        usage = Usage()
        stop_reason = "stop"
        request_id: str | None = None

        async with http_client().stream(
            "POST",
            f"{settings.anthropic_base_url}/v1/messages",
            headers=self._headers(),
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode()[:1200]
                raise ProviderError(f"Anthropic stream failed {resp.status_code}", details={"body": body})
            request_id = resp.headers.get("request-id")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "message_start":
                    u = (event.get("message") or {}).get("usage") or {}
                    usage.input_tokens = int(u.get("input_tokens", 0))
                    usage.cached_input_tokens = int(u.get("cache_read_input_tokens", 0))
                elif etype == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        current_tool = {"id": block.get("id"), "name": block.get("name"), "json": ""}
                elif etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        text_parts.append(text)
                        yield StreamChunk(delta=text)
                    elif delta.get("type") == "input_json_delta" and current_tool is not None:
                        current_tool["json"] += delta.get("partial_json", "")
                elif etype == "content_block_stop" and current_tool is not None:
                    try:
                        args = json.loads(current_tool["json"] or "{}")
                    except json.JSONDecodeError:
                        args = {"_raw": current_tool["json"]}
                    tool_calls.append(
                        ToolCall(
                            id=current_tool["id"] or str(uuid.uuid4()),
                            name=current_tool["name"] or "",
                            arguments=args,
                        )
                    )
                    current_tool = None
                elif etype == "message_delta":
                    usage.output_tokens = int(
                        (event.get("usage") or {}).get("output_tokens", usage.output_tokens)
                    )
                    stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason

        content = "".join(text_parts)
        if not usage.output_tokens:
            usage.output_tokens = estimate_tokens(content)
        yield StreamChunk(
            done=True,
            response=LLMResponse(
                content=content,
                model=model,
                provider=self.name,
                usage=usage,
                tool_calls=tool_calls,
                finish_reason=stop_reason,
                latency_ms=(time.perf_counter() - started) * 1000,
                request_id=request_id,
            ),
        )

    async def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "provider": self.name,
            "configured": self.configured,
            "base_url": settings.anthropic_base_url,
        }
        if not self.configured:
            info["status"] = "not_configured"
            return info
        try:
            started = time.perf_counter()
            resp = await http_client().get(
                f"{settings.anthropic_base_url}/v1/models", headers=self._headers(), timeout=8.0
            )
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            info["status"] = "healthy" if resp.status_code < 400 else "degraded"
            info["http_status"] = resp.status_code
        except Exception as exc:
            info["status"] = "unhealthy"
            info["error"] = str(exc)
        return info
