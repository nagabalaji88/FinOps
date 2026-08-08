"""Test fixtures: isolated database, seeded platform, authenticated client.

The scripted provider below is a *test double* used only inside the test suite to make
LLM behaviour deterministic. It is never registered by the application itself.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
_db_fd, _db_path = tempfile.mkstemp(suffix=".db", prefix="finops-test-")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
os.environ["JWT_SECRET"] = "test-secret-key-for-suite-only"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "TestPassword!2026"
# Ensure no real provider is reachable from the suite.
for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "AZURE_OPENAI_API_KEY",
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "MISTRAL_API_KEY", "DEEPSEEK_API_KEY",
            "TOGETHER_API_KEY", "OLLAMA_BASE_URL"):
    os.environ.pop(var, None)
# The scripted provider answers with canned agent responses, which is meaningless as a
# rail classifier. The suite therefore runs NeMo's deterministic rails only; the LLM-backed
# self-check rails are covered separately in test_guardrails.py against a stub classifier.
os.environ.setdefault("NEMO_LLM_RAILS_ENABLED", "false")

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db.session import SessionFactory, engine  # noqa: E402
from app.llm.base import LLMProvider  # noqa: E402
from app.llm.catalog import CATALOG, ModelSpec  # noqa: E402
from app.llm.router import router as model_router  # noqa: E402
from app.llm.types import LLMResponse, Message, ToolCall, ToolSchema, Usage  # noqa: E402
from app.main import app  # noqa: E402
from app.services import telemetry  # noqa: E402
from app.services.bootstrap import bootstrap, ensure_schema, seed_sample_banking  # noqa: E402

TEST_MODEL = "test-scripted-model"


class ScriptedProvider(LLMProvider):
    """Deterministic provider used by the suite to exercise the execution graph."""

    name = "scripted"

    def __init__(self) -> None:
        self.script: list[LLMResponse] = []
        self.calls: list[dict[str, Any]] = []

    @property
    def configured(self) -> bool:
        return True

    def queue_text(self, content: str) -> None:
        self.script.append(
            LLMResponse(content=content, model=TEST_MODEL, provider=self.name,
                        usage=Usage(input_tokens=120, output_tokens=40), latency_ms=5.0)
        )

    def queue_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self.script.append(
            LLMResponse(
                content="", model=TEST_MODEL, provider=self.name,
                usage=Usage(input_tokens=100, output_tokens=30),
                tool_calls=[ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name,
                                     arguments=arguments)],
                finish_reason="tool_use", latency_ms=5.0,
            )
        )

    def reset(self) -> None:
        self.script.clear()
        self.calls.clear()

    async def chat(self, *, model: str, messages: list[Message],
                   tools: list[ToolSchema] | None = None, **kwargs: Any) -> LLMResponse:
        self.calls.append({"model": model, "messages": len(messages),
                           "tools": [t.name for t in (tools or [])]})
        if self.script:
            return self.script.pop(0)
        return LLMResponse(content="Scripted default response.", model=TEST_MODEL,
                           provider=self.name, usage=Usage(input_tokens=50, output_tokens=10),
                           latency_ms=4.0)


scripted_provider = ScriptedProvider()


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _database() -> AsyncIterator[None]:
    from app.rag.vectorstore import init_vector_store

    await ensure_schema()
    await init_vector_store(768)
    telemetry.install()
    async with SessionFactory() as session:
        await bootstrap(session)
        # Enough customers for the credit-application profiles and the delinquency
        # band that follows them; see _APPLICATION_PROFILES in bootstrap.
        await seed_sample_banking(session, customers=12)
    yield
    await engine.dispose()
    os.close(_db_fd)
    os.unlink(_db_path)


@pytest.fixture(autouse=True)
def _register_scripted_model() -> Any:
    """Expose the scripted provider through the router for the duration of a test."""
    CATALOG[TEST_MODEL] = ModelSpec(
        id=TEST_MODEL, provider="scripted", display_name="Scripted Test Model",  # type: ignore[arg-type]
        family="test", context_window=100_000, max_output_tokens=8000,
        input_price_per_mtok=1.0, output_price_per_mtok=2.0, tier="balanced",
    )
    model_router._providers["scripted"] = scripted_provider  # noqa: SLF001
    scripted_provider.reset()
    yield scripted_provider
    scripted_provider.reset()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[Any]:
    async with SessionFactory() as db:
        yield db
        await db.rollback()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest_asyncio.fixture
async def admin_token(client: AsyncClient) -> str:
    response = await client.post("/api/v1/auth/login", json={
        "email": "admin@finops.local", "password": "TestPassword!2026"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest_asyncio.fixture
async def approver_token(client: AsyncClient) -> str:
    response = await client.post("/api/v1/auth/login", json={
        "email": "approver@finops.local", "password": "TestPassword!2026"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def auth(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}
