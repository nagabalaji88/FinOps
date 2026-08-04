"""Amazon Bedrock provider using the Converse API with native SigV4 signing.

Signing is implemented directly against the AWS Signature Version 4 spec so the
service has no hard dependency on boto3; when boto3 credentials are present in the
environment they are picked up through the standard variables.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.core.errors import ProviderError, ProviderNotConfiguredError
from app.llm.base import LLMProvider, estimate_tokens, http_client
from app.llm.types import EmbeddingResult, LLMResponse, Message, ToolCall, ToolSchema, Usage

SERVICE = "bedrock"
ALGORITHM = "AWS4-HMAC-SHA256"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(f"AWS4{secret}".encode(), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def sigv4_headers(
    *,
    method: str,
    host: str,
    path: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str | None,
    payload: bytes,
    service: str = SERVICE,
) -> dict[str, str]:
    now = dt.datetime.now(dt.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    canonical_uri = quote(path, safe="/-_.~:")
    payload_hash = hashlib.sha256(payload).hexdigest()

    headers = {
        "content-type": "application/json",
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token

    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join(
        [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [ALGORITHM, amz_date, credential_scope,
         hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, service), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    headers["authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def _to_converse(messages: list[Message]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system: list[dict[str, Any]] = []
    converted: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            system.append({"text": m.content})
        elif m.role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": m.tool_call_id,
                                "content": [{"text": m.content}],
                            }
                        }
                    ],
                }
            )
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"text": m.content})
            blocks.extend(
                {"toolUse": {"toolUseId": tc.id, "name": tc.name, "input": tc.arguments}}
                for tc in m.tool_calls
            )
            converted.append({"role": "assistant", "content": blocks or [{"text": ""}]})
        else:
            converted.append({"role": "user", "content": [{"text": m.content}]})
    return system, converted


class BedrockProvider(LLMProvider):
    name = "bedrock"

    @property
    def configured(self) -> bool:
        return bool(settings.aws_access_key_id and settings.aws_secret_access_key)

    @property
    def host(self) -> str:
        if settings.bedrock_endpoint:
            return settings.bedrock_endpoint.replace("https://", "").rstrip("/")
        return f"bedrock-runtime.{settings.aws_region}.amazonaws.com"

    def _require(self) -> None:
        if not self.configured:
            raise ProviderNotConfiguredError(
                "Amazon Bedrock is not configured",
                details={"required": "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"},
            )

    async def _invoke(self, path: str, body: dict[str, Any]) -> tuple[dict[str, Any], float, str | None]:
        import os

        payload = json.dumps(body).encode()
        headers = sigv4_headers(
            method="POST",
            host=self.host,
            path=path,
            region=settings.aws_region,
            access_key=settings.aws_access_key_id or "",
            secret_key=settings.aws_secret_access_key or "",
            session_token=os.getenv("AWS_SESSION_TOKEN"),
            payload=payload,
        )
        started = time.perf_counter()
        try:
            resp = await http_client().post(f"https://{self.host}{path}", headers=headers,
                                            content=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Bedrock transport error: {exc}") from exc
        latency = (time.perf_counter() - started) * 1000
        if resp.status_code >= 400:
            raise ProviderError(f"Bedrock returned {resp.status_code}",
                                details={"body": resp.text[:1200], "path": path})
        return resp.json(), latency, resp.headers.get("x-amzn-requestid")

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
        system, converted = _to_converse(messages)
        body: dict[str, Any] = {
            "messages": converted,
            "inferenceConfig": {"temperature": temperature, "maxTokens": max_tokens},
        }
        if system:
            body["system"] = system
        if top_p is not None:
            body["inferenceConfig"]["topP"] = top_p
        if stop:
            body["inferenceConfig"]["stopSequences"] = stop
        if tools:
            body["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": {"json": t.parameters},
                        }
                    }
                    for t in tools
                ]
            }
        if extra:
            body.update(extra)

        data, latency, request_id = await self._invoke(f"/model/{quote(model, safe='')}/converse", body)
        blocks = ((data.get("output") or {}).get("message") or {}).get("content", [])
        text_parts = [b["text"] for b in blocks if "text" in b]
        tool_calls = [
            ToolCall(id=b["toolUse"].get("toolUseId", str(uuid.uuid4())),
                     name=b["toolUse"].get("name", ""), arguments=b["toolUse"].get("input") or {})
            for b in blocks
            if "toolUse" in b
        ]
        usage_raw = data.get("usage") or {}
        content = "".join(text_parts)
        usage = Usage(
            input_tokens=int(usage_raw.get("inputTokens", 0)) or
            sum(estimate_tokens(m.content or "") for m in messages),
            output_tokens=int(usage_raw.get("outputTokens", 0)) or estimate_tokens(content),
            cached_input_tokens=int(usage_raw.get("cacheReadInputTokens", 0) or 0),
        )
        return LLMResponse(
            content=content, model=model, provider=self.name, usage=usage, tool_calls=tool_calls,
            finish_reason=data.get("stopReason", "end_turn"), latency_ms=latency,
            request_id=request_id,
        )

    async def embed(self, *, model: str, texts: list[str]) -> EmbeddingResult:
        self._require()
        vectors: list[list[float]] = []
        total_latency = 0.0
        tokens = 0
        for text in texts:
            data, latency, _ = await self._invoke(
                f"/model/{quote(model, safe='')}/invoke", {"inputText": text}
            )
            vectors.append(data.get("embedding", []))
            tokens += int(data.get("inputTextTokenCount", estimate_tokens(text)))
            total_latency += latency
        return EmbeddingResult(
            vectors=vectors, model=model, provider=self.name,
            dimensions=len(vectors[0]) if vectors else 0,
            usage=Usage(input_tokens=tokens), latency_ms=total_latency,
        )

    async def health(self) -> dict[str, Any]:
        info: dict[str, Any] = {"provider": self.name, "configured": self.configured,
                                "region": settings.aws_region, "endpoint": self.host}
        if not self.configured:
            info["status"] = "not_configured"
            return info
        try:
            import os

            host = f"bedrock.{settings.aws_region}.amazonaws.com"
            path = "/foundation-models"
            headers = sigv4_headers(
                method="GET", host=host, path=path, region=settings.aws_region,
                access_key=settings.aws_access_key_id or "",
                secret_key=settings.aws_secret_access_key or "",
                session_token=os.getenv("AWS_SESSION_TOKEN"), payload=b"",
            )
            started = time.perf_counter()
            resp = await http_client().get(f"https://{host}{path}", headers=headers, timeout=8.0)
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
            info["status"] = "healthy" if resp.status_code < 400 else "degraded"
            info["http_status"] = resp.status_code
        except Exception as exc:
            info["status"] = "unhealthy"
            info["error"] = str(exc)
        return info
