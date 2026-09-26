"""The model administration surface.

An operator has to be able to answer three questions without a deploy: what can this
deployment call, which model do the agents use, and are the credentials good. Each of those
is only useful if the answer is the truth about the running process rather than about the
environment it started with.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.runtime_config import runtime_config
from app.llm.catalog import CATALOG

pytestmark = pytest.mark.asyncio


class TestCatalogue:
    async def test_it_reports_the_selection_and_the_catalogue(self, client: AsyncClient, auth: dict):
        response = await client.get("/api/v1/models", headers=auth)
        assert response.status_code == 200
        body = response.json()
        assert len(body["models"]) == len(CATALOG)
        assert set(body["selection"]) == {
            "default_model",
            "guardrails_model",
            "effective_guardrails_model",
            "aligned",
        }
        assert "ready" in body["readiness"]

    async def test_each_model_says_what_it_would_need(self, client: AsyncClient, auth: dict):
        """'Unavailable' alone leaves an operator guessing which of nine keys is missing."""
        body = (await client.get("/api/v1/models", headers=auth)).json()
        for model in body["models"]:
            assert "credential_available" in model
            assert "is_local_runtime" in model

    @pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
    async def test_every_shipped_provider_names_its_variable(self):
        """The mapping is hand-maintained, so adding a provider and forgetting it is the
        likely mistake -- and its symptom is a model the UI cannot tell you how to enable."""
        from app.llm.router import PROVIDER_KEY_ENV_VARS

        shipped = {spec.provider for spec in CATALOG.values()} - {"scripted"}
        missing = shipped - set(PROVIDER_KEY_ENV_VARS)
        assert not missing, f"providers with no credential variable named: {sorted(missing)}"

    async def test_local_runtimes_are_not_reported_as_missing_a_key(self, client: AsyncClient, auth: dict):
        """A local model is not a model with a missing key; showing it as one reads as a fault."""
        body = (await client.get("/api/v1/models", headers=auth)).json()
        local = [m for m in body["models"] if m["is_local_runtime"]]
        assert local, "the catalogue should carry at least one local model"
        assert all(m["provider"] == "ollama" for m in local)


class TestSelection:
    async def test_choosing_a_model_persists_and_takes_effect(self, client: AsyncClient, auth: dict):
        response = await client.put(
            "/api/v1/models/selection", headers=auth, json={"default_model": "gpt-4o-mini"}
        )
        assert response.status_code == 200
        assert response.json()["default_model"] == "gpt-4o-mini"
        # The point of the whole feature: the running process, not just the row.
        assert runtime_config.default_model == "gpt-4o-mini"

    async def test_an_unknown_model_is_refused_with_the_known_ones(self, client: AsyncClient, auth: dict):
        response = await client.put(
            "/api/v1/models/selection", headers=auth, json={"default_model": "gpt-9-imaginary"}
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"]["known_models"]

    async def test_clearing_the_guardrails_model_realigns_it_with_the_agents(
        self, client: AsyncClient, auth: dict
    ):
        await client.put(
            "/api/v1/models/selection",
            headers=auth,
            json={"default_model": "gpt-4o-mini", "guardrails_model": "gpt-4o"},
        )
        assert runtime_config.guardrails_model == "gpt-4o"

        await client.put("/api/v1/models/selection", headers=auth, json={"guardrails_model": ""})
        body = (await client.get("/api/v1/models", headers=auth)).json()
        assert body["selection"]["guardrails_model"] is None
        assert body["selection"]["effective_guardrails_model"] == "gpt-4o-mini"
        assert body["selection"]["aligned"] is True

    async def test_the_rails_follow_the_selection(self, client: AsyncClient, auth: dict):
        from app.guardrails.llm_adapter import RouterLLM

        await client.put(
            "/api/v1/models/selection",
            headers=auth,
            json={"default_model": "gpt-4o-mini", "guardrails_model": ""},
        )
        assert RouterLLM().model_name == "gpt-4o-mini"


class TestKeys:
    async def test_a_stored_key_applies_without_a_restart(self, client: AsyncClient, auth: dict):
        """The whole reason this is not just an .env edit."""
        before = (await client.get("/api/v1/models/keys", headers=auth)).json()
        assert {k["provider"] for k in before} >= {"openai", "anthropic", "google"}

        response = await client.put(
            "/api/v1/models/keys/openai", headers=auth, json={"value": "sk-test-applied-now"}
        )
        assert response.status_code == 200
        assert runtime_config.api_key("openai") == "sk-test-applied-now"

        after = (await client.get("/api/v1/models/keys", headers=auth)).json()
        openai = next(k for k in after if k["provider"] == "openai")
        assert openai["set"] is True
        assert openai["source"] == "stored"

        await client.delete("/api/v1/models/keys/openai", headers=auth)
        assert runtime_config.api_key("openai") is None

    async def test_the_value_is_never_returned(self, client: AsyncClient, auth: dict):
        await client.put("/api/v1/models/keys/mistral", headers=auth, json={"value": "sk-secret-do-not-echo"})
        try:
            for path in ("/api/v1/models/keys", "/api/v1/models"):
                assert "sk-secret-do-not-echo" not in (await client.get(path, headers=auth)).text
        finally:
            await client.delete("/api/v1/models/keys/mistral", headers=auth)

    async def test_an_unknown_provider_is_refused(self, client: AsyncClient, auth: dict):
        response = await client.put("/api/v1/models/keys/notaprovider", headers=auth, json={"value": "x"})
        assert response.status_code == 404


class TestModelTest:
    async def test_it_refuses_rather_than_testing_a_different_model(self, client: AsyncClient, auth: dict):
        """select() substitutes when a model has no credential, which is right for traffic and
        wrong for a test: reporting success for a model that never ran is worse than no test."""
        response = await client.post("/api/v1/models/test", headers=auth, json={"model_id": "gpt-4o"})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "OPENAI_API_KEY" in body["error"]

    async def test_an_uncatalogued_model_is_not_found(self, client: AsyncClient, auth: dict):
        response = await client.post("/api/v1/models/test", headers=auth, json={"model_id": "nope-not-real"})
        assert response.status_code == 404

    async def test_the_key_test_route_is_not_swallowed_by_the_model_route(
        self, client: AsyncClient, auth: dict
    ):
        """Model ids contain slashes, so a :path parameter here would capture keys/<p>/test."""
        response = await client.post("/api/v1/models/keys/openai/test", headers=auth)
        assert response.status_code == 200
        assert response.json()["provider"] == "openai"


class TestAvailable:
    async def test_it_reports_per_provider_rather_than_one_flat_list(self, client: AsyncClient, auth: dict):
        response = await client.get("/api/v1/models/available", headers=auth)
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body["providers"], list)
        assert body["total"] == sum(len(p["models"]) for p in body["providers"])
        for provider in body["providers"]:
            assert provider["status"] in {"ok", "error", "no_listing_endpoint", "unknown"}
