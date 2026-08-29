"""Model catalogue: capabilities, context windows and list pricing (USD / 1M tokens).

Prices are published list prices and are configurable at runtime via the
``MODEL_PRICE_OVERRIDES`` environment variable (JSON: {"model": {"input": x, "output": y}}).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Literal

Provider = Literal[
    "openai",
    "azure_openai",
    "anthropic",
    "google",
    "bedrock",
    "mistral",
    "deepseek",
    "together",
    "groq",
    "openrouter",
    "ollama",
]


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: Provider
    display_name: str
    family: str
    context_window: int
    max_output_tokens: int
    input_price_per_mtok: float
    output_price_per_mtok: float
    cached_input_price_per_mtok: float = 0.0
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_vision: bool = False
    supports_json_mode: bool = True
    is_embedding: bool = False
    dimensions: int = 0
    tier: Literal["frontier", "balanced", "fast", "embedding"] = "balanced"
    tags: tuple[str, ...] = field(default_factory=tuple)


CATALOG: dict[str, ModelSpec] = {}


def _register(*specs: ModelSpec) -> None:
    for spec in specs:
        CATALOG[spec.id] = spec


# fmt: off
# The catalogue is a price list. Each model occupies two lines with its fields in a fixed
# order, so a reviewer can scan a column — context window, input price, output price —
# down the page and compare. One argument per line would run to a thousand lines and
# make that comparison impossible.
# --- OpenAI -----------------------------------------------------------------
_register(
    ModelSpec("gpt-5.5", "openai", "GPT-5.5", "gpt", 400_000, 128_000, 1.75, 14.0, 0.175,
              supports_vision=True, tier="frontier", tags=("reasoning", "agentic")),
    ModelSpec("gpt-5.5-mini", "openai", "GPT-5.5 Mini", "gpt", 400_000, 128_000, 0.35, 2.8, 0.035,
              supports_vision=True, tier="fast"),
    ModelSpec("gpt-4.1", "openai", "GPT-4.1", "gpt", 1_047_576, 32_768, 2.0, 8.0, 0.5,
              supports_vision=True),
    ModelSpec("gpt-4o", "openai", "GPT-4o", "gpt", 128_000, 16_384, 2.5, 10.0, 1.25,
              supports_vision=True),
    ModelSpec("gpt-4o-mini", "openai", "GPT-4o mini", "gpt", 128_000, 16_384, 0.15, 0.6, 0.075,
              supports_vision=True, tier="fast"),
    ModelSpec("text-embedding-3-small", "openai", "Embedding 3 Small", "embedding", 8191, 0,
              0.02, 0.0, is_embedding=True, dimensions=1536, tier="embedding",
              supports_tools=False, supports_streaming=False),
    ModelSpec("text-embedding-3-large", "openai", "Embedding 3 Large", "embedding", 8191, 0,
              0.13, 0.0, is_embedding=True, dimensions=3072, tier="embedding",
              supports_tools=False, supports_streaming=False),
)

# --- Anthropic ---------------------------------------------------------------
_register(
    ModelSpec("claude-opus-4-5", "anthropic", "Claude Opus 4.5", "claude", 200_000, 64_000,
              5.0, 25.0, 0.5, supports_vision=True, tier="frontier", tags=("reasoning",)),
    ModelSpec("claude-sonnet-4-5", "anthropic", "Claude Sonnet 4.5", "claude", 200_000, 64_000,
              3.0, 15.0, 0.3, supports_vision=True, tier="balanced", tags=("agentic", "coding")),
    ModelSpec("claude-haiku-4-5", "anthropic", "Claude Haiku 4.5", "claude", 200_000, 32_000,
              1.0, 5.0, 0.1, supports_vision=True, tier="fast"),
)

# --- Google ------------------------------------------------------------------
_register(
    # Google retired the 2.5 pair for new accounts and names these as the replacements in
    # its own 404 body. Keeping the retired ids here only offers an operator a model their
    # key cannot call, which is what the "Available models" tab exists to correct.
    ModelSpec("gemini-3.1-pro-preview", "google", "Gemini 3.1 Pro", "gemini", 1_048_576, 65_536,
              1.25, 10.0, 0.31, supports_vision=True, tier="frontier"),
    ModelSpec("gemini-3.6-flash", "google", "Gemini 3.6 Flash", "gemini", 1_048_576, 65_536,
              0.30, 2.5, 0.075, supports_vision=True, tier="fast"),
    ModelSpec("text-embedding-004", "google", "Gemini Embedding 004", "embedding", 2048, 0,
              0.0, 0.0, is_embedding=True, dimensions=768, tier="embedding",
              supports_tools=False, supports_streaming=False),
)

# --- Amazon Bedrock -----------------------------------------------------------
_register(
    ModelSpec("amazon.nova-pro-v1:0", "bedrock", "Amazon Nova Pro", "nova", 300_000, 5_000,
              0.8, 3.2, 0.2, supports_vision=True, tier="balanced"),
    ModelSpec("amazon.nova-lite-v1:0", "bedrock", "Amazon Nova Lite", "nova", 300_000, 5_000,
              0.06, 0.24, 0.015, supports_vision=True, tier="fast"),
    ModelSpec("anthropic.claude-sonnet-4-5-v1:0", "bedrock", "Claude Sonnet 4.5 (Bedrock)",
              "claude", 200_000, 64_000, 3.0, 15.0, 0.3, supports_vision=True),
    ModelSpec("meta.llama3-3-70b-instruct-v1:0", "bedrock", "Llama 3.3 70B (Bedrock)", "llama",
              128_000, 8_192, 0.72, 0.72),
    ModelSpec("mistral.mistral-large-2407-v1:0", "bedrock", "Mistral Large (Bedrock)", "mistral",
              128_000, 8_192, 2.0, 6.0),
    ModelSpec("amazon.titan-embed-text-v2:0", "bedrock", "Titan Embed v2", "embedding", 8192, 0,
              0.02, 0.0, is_embedding=True, dimensions=1024, tier="embedding",
              supports_tools=False, supports_streaming=False),
)

# --- Mistral / DeepSeek / Llama -----------------------------------------------
_register(
    ModelSpec("mistral-large-latest", "mistral", "Mistral Large", "mistral", 131_072, 32_768,
              2.0, 6.0, tier="balanced"),
    ModelSpec("mistral-small-latest", "mistral", "Mistral Small", "mistral", 131_072, 32_768,
              0.2, 0.6, tier="fast"),
    ModelSpec("deepseek-chat", "deepseek", "DeepSeek V3", "deepseek", 128_000, 8_192,
              0.27, 1.1, 0.07, tier="balanced"),
    ModelSpec("deepseek-reasoner", "deepseek", "DeepSeek R1", "deepseek", 128_000, 32_768,
              0.55, 2.19, 0.14, tier="frontier", tags=("reasoning",)),
    ModelSpec("meta-llama/Llama-3.3-70B-Instruct-Turbo", "together", "Llama 3.3 70B", "llama",
              131_072, 8_192, 0.88, 0.88, tier="balanced"),
    ModelSpec("meta-llama/Llama-4-Scout-17B-16E-Instruct", "together", "Llama 4 Scout", "llama",
              327_680, 8_192, 0.18, 0.59, supports_vision=True, tier="fast"),
    ModelSpec("llama3.1", "ollama", "Llama 3.1 (local)", "llama", 128_000, 8_192, 0.0, 0.0,
              tier="fast", tags=("self-hosted",)),
)

# --- Groq --------------------------------------------------------------------
# OpenAI-compatible wire format at a different base URL, so the same client serves it.
_register(
    ModelSpec("openai/gpt-oss-120b", "groq", "GPT-OSS 120B (Groq)", "gpt-oss", 128_000, 32_768,
              0.15, 0.75, tier="balanced", tags=("open-weight",)),
    ModelSpec("openai/gpt-oss-20b", "groq", "GPT-OSS 20B (Groq)", "gpt-oss", 128_000, 32_768,
              0.10, 0.50, tier="fast", tags=("open-weight",)),
)

# --- OpenRouter --------------------------------------------------------------
# One credential in front of many providers; ids carry the upstream vendor as a prefix.
_register(
    ModelSpec("meta-llama/llama-3.3-70b-instruct", "openrouter", "Llama 3.3 70B (OpenRouter)",
              "llama", 131_072, 8_192, 0.10, 0.32, tier="balanced"),
    ModelSpec("mistralai/mistral-small-3.2-24b-instruct", "openrouter",
              "Mistral Small 3.2 24B (OpenRouter)", "mistral", 131_072, 8_192, 0.09, 0.25,
              tier="fast"),
    ModelSpec("deepseek/deepseek-chat", "openrouter", "DeepSeek Chat (OpenRouter)", "deepseek",
              128_000, 8_192, 0.26, 1.03, tier="balanced"),
    ModelSpec("nomic-embed-text", "ollama", "Nomic Embed (local)", "embedding", 8192, 0, 0.0, 0.0,
              is_embedding=True, dimensions=768, tier="embedding",
              supports_tools=False, supports_streaming=False),
)

# fmt: on


def _apply_overrides() -> None:
    raw = os.getenv("MODEL_PRICE_OVERRIDES")
    if not raw:
        return
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError:
        return
    for model_id, prices in overrides.items():
        spec = CATALOG.get(model_id)
        if not spec:
            continue
        CATALOG[model_id] = ModelSpec(
            **{
                **spec.__dict__,
                "input_price_per_mtok": float(prices.get("input", spec.input_price_per_mtok)),
                "output_price_per_mtok": float(prices.get("output", spec.output_price_per_mtok)),
            }
        )


_apply_overrides()

# Aliases so callers can request a family without pinning a snapshot.
ALIASES: dict[str, str] = {
    "gpt-5": "gpt-5.5",
    "claude": "claude-sonnet-4-5",
    "claude-opus": "claude-opus-4-5",
    "gemini": "gemini-3.1-pro-preview",
    "llama": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "mistral": "mistral-large-latest",
    "deepseek": "deepseek-chat",
    "nova": "amazon.nova-pro-v1:0",
}


def resolve_model(model_id: str) -> ModelSpec | None:
    return CATALOG.get(ALIASES.get(model_id, model_id))


def compute_cost(spec: ModelSpec, input_tokens: int, output_tokens: int, cached: int = 0) -> float:
    billable_input = max(input_tokens - cached, 0)
    cost = billable_input / 1_000_000 * spec.input_price_per_mtok
    cost += cached / 1_000_000 * (spec.cached_input_price_per_mtok or spec.input_price_per_mtok)
    cost += output_tokens / 1_000_000 * spec.output_price_per_mtok
    return round(cost, 8)


def list_models(*, embeddings: bool | None = None) -> list[ModelSpec]:
    values = list(CATALOG.values())
    if embeddings is True:
        return [m for m in values if m.is_embedding]
    if embeddings is False:
        return [m for m in values if not m.is_embedding]
    return values
