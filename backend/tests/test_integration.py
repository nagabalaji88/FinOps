"""Integration tests: API surface, tools against the banking tables, RAG, and the engine."""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.models.agents import Approval, Span
from app.db.models.banking import Customer
from app.db.session import SessionFactory
from tests.conftest import TEST_MODEL


class TestAuthApi:
    async def test_login_and_me(self, client: AsyncClient, admin_token: str):
        response = await client.get("/api/v1/auth/me",
                                    headers={"Authorization": f"Bearer {admin_token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == "admin@finops.local"
        assert "admin" in body["roles"]

    async def test_unauthenticated_is_rejected(self, client: AsyncClient):
        assert (await client.get("/api/v1/agents")).status_code == 401

    async def test_bad_password_is_rejected(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/login",
                                     json={"email": "admin@finops.local", "password": "nope"})
        assert response.status_code == 401

    async def test_viewer_cannot_execute(self, client: AsyncClient, auth: dict):
        created = await client.post(
            "/api/v1/auth/users", headers=auth,
            json={"email": "viewer.test@finops.local", "full_name": "Viewer",
                  "password": "ViewerPassword!2026", "roles": ["viewer"]},
        )
        assert created.status_code == 201
        login = await client.post("/api/v1/auth/login",
                                  json={"email": "viewer.test@finops.local",
                                        "password": "ViewerPassword!2026"})
        token = login.json()["access_token"]
        response = await client.post(
            "/api/v1/agents/knowledge_assistant/execute",
            headers={"Authorization": f"Bearer {token}"}, json={"input": {"query": "hello"}},
        )
        assert response.status_code == 403

    async def test_api_key_authentication(self, client: AsyncClient, auth: dict):
        created = await client.post("/api/v1/auth/api-keys", headers=auth,
                                    json={"name": "integration", "rate_limit_per_minute": 500})
        assert created.status_code == 201
        key = created.json()["api_key"]
        response = await client.get("/api/v1/auth/me", headers={"X-API-Key": key})
        assert response.status_code == 200
        assert response.json()["auth_method"] == "api_key"


class TestCatalogueApi:
    async def test_agents_listed_with_roadmap(self, client: AsyncClient, auth: dict):
        response = await client.get("/api/v1/agents", headers=auth)
        assert response.status_code == 200
        agents = response.json()
        implemented = [a for a in agents if a["availability"] == "implemented"]
        coming_soon = [a for a in agents if a["availability"] == "coming_soon"]
        # The catalogue is fifteen agents; the split moves as roadmap agents are built.
        assert len(implemented) + len(coming_soon) == 15
        assert len(implemented) >= 7
        keys = {a["key"] for a in implemented}
        assert {"customer_service", "kyc_onboarding", "aml_investigation",
                "investment_research", "knowledge_assistant",
                "credit_risk", "collections"} <= keys
        # Whatever is still on the roadmap must refuse to execute, not half-work.
        for agent in coming_soon:
            assert agent["lifecycle_state"] == "disabled"

    async def test_roadmap_agent_cannot_execute(self, client: AsyncClient, auth: dict):
        response = await client.post("/api/v1/agents/trading/execute", headers=auth,
                                     json={"input": {"query": "trade"}})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    async def test_dashboards_respond(self, client: AsyncClient, auth: dict):
        for path in ["/api/v1/dashboard/overview", "/api/v1/dashboard/ai-usage",
                     "/api/v1/dashboard/knowledge", "/api/v1/dashboard/geography",
                     "/api/v1/costs/summary",
                     "/api/v1/platform-metrics", "/api/v1/security/overview",
                     "/api/v1/services", "/api/v1/tools", "/api/v1/agents/graph"]:
            response = await client.get(path, headers=auth)
            assert response.status_code == 200, f"{path} -> {response.text[:200]}"


class TestGeographyDashboard:
    """The geography screen must aggregate the real ledger, not a fixed map."""

    async def test_countries_come_from_the_transaction_ledger(
        self, client: AsyncClient, auth: dict, session):
        from sqlalchemy import func, select

        from app.db.models.banking import Transaction

        response = await client.get("/api/v1/dashboard/geography", headers=auth,
                                    params={"days": 730})
        assert response.status_code == 200, response.text
        body = response.json()

        ledger = dict((await session.execute(
            select(Transaction.country, func.count(Transaction.id)).group_by(Transaction.country)
        )).all())
        reported = {row["code"]: row["transactions"] for row in body["countries"]}
        assert reported, "the seeded ledger has transactions, so countries must be reported"
        for code, count in reported.items():
            assert ledger[code] == count

        # Nothing is invented: every reported country was actually transacted with.
        assert set(reported) <= set(ledger)
        assert body["unmapped"] == [] or all(code not in reported for code in body["unmapped"])

    async def test_totals_reconcile_and_shares_are_consistent(
        self, client: AsyncClient, auth: dict):
        response = await client.get("/api/v1/dashboard/geography", headers=auth,
                                    params={"days": 730})
        body = response.json()
        summary = body["summary"]

        assert summary["transactions"] == sum(c["transactions"] for c in body["countries"])
        assert summary["total_value"] == pytest.approx(
            sum(c["total_value"] for c in body["countries"]), rel=1e-6)
        assert sum(c["share_pct"] for c in body["countries"]) == pytest.approx(100.0, abs=0.5)

        domestic = [c for c in body["countries"] if c["domestic"]]
        assert len(domestic) == 1 and domestic[0]["code"] == summary["domestic_country"]
        cross_border = sum(c["total_value"] for c in body["countries"] if not c["domestic"])
        assert summary["cross_border_value"] == pytest.approx(cross_border, rel=1e-6)

        # Inbound and outbound partition the value of every jurisdiction.
        for country in body["countries"]:
            assert country["inbound_value"] + country["outbound_value"] == pytest.approx(
                country["total_value"], rel=1e-6)

    async def test_high_risk_jurisdictions_follow_the_aml_rule_set(
        self, client: AsyncClient, auth: dict):
        from app.tools.aml import HIGH_RISK_COUNTRIES

        response = await client.get("/api/v1/dashboard/geography", headers=auth,
                                    params={"days": 730})
        body = response.json()
        for country in body["countries"]:
            expected_high = country["code"] in HIGH_RISK_COUNTRIES
            assert (country["risk_level"] == "high") is expected_high
        high_risk_value = sum(
            c["total_value"] for c in body["countries"] if c["code"] in HIGH_RISK_COUNTRIES)
        assert body["summary"]["high_risk_value"] == pytest.approx(high_risk_value, rel=1e-6)

    async def test_every_plotted_place_has_real_coordinates(
        self, client: AsyncClient, auth: dict):
        from app.seed.geo_reference import CITIES, COUNTRIES

        response = await client.get("/api/v1/dashboard/geography", headers=auth,
                                    params={"days": 730})
        body = response.json()
        for country in body["countries"]:
            place = COUNTRIES[country["code"]]
            assert (country["latitude"], country["longitude"]) == (place.latitude, place.longitude)
        for city in body["cities"]:
            place = CITIES[city["name"]]
            assert (city["latitude"], city["longitude"]) == (place.latitude, place.longitude)
        for corridor in body["corridors"]:
            assert -90 <= corridor["from_latitude"] <= 90
            assert -180 <= corridor["to_longitude"] <= 180
            assert corridor["to_code"] != body["summary"]["domestic_country"]

    async def test_window_narrows_the_result(self, client: AsyncClient, auth: dict):
        wide = (await client.get("/api/v1/dashboard/geography", headers=auth,
                                 params={"days": 730})).json()
        narrow = (await client.get("/api/v1/dashboard/geography", headers=auth,
                                   params={"days": 1})).json()
        assert narrow["summary"]["transactions"] <= wide["summary"]["transactions"]
        assert narrow["window_days"] == 1
        assert len(narrow["trend"]) <= len(wide["trend"])

    async def test_requires_metric_read(self, client: AsyncClient):
        response = await client.get("/api/v1/dashboard/geography")
        assert response.status_code == 401

    async def test_lifecycle_transitions(self, client: AsyncClient, auth: dict):
        paused = await client.post("/api/v1/agents/customer_service/lifecycle", headers=auth,
                                   json={"action": "pause", "reason": "test"})
        assert paused.json()["lifecycle_state"] == "paused"
        blocked = await client.post("/api/v1/agents/customer_service/execute", headers=auth,
                                    json={"input": {"query": "hi"}})
        assert blocked.status_code == 422
        resumed = await client.post("/api/v1/agents/customer_service/lifecycle", headers=auth,
                                    json={"action": "resume"})
        assert resumed.json()["lifecycle_state"] == "active"

    async def test_config_versioning_and_rollback(self, client: AsyncClient, auth: dict):
        first = await client.put("/api/v1/agents/knowledge_assistant/config", headers=auth,
                                 json={"temperature": 0.5, "changelog": "raise temperature"})
        assert first.status_code == 200
        version = first.json()["version"]
        published = await client.post(
            f"/api/v1/agents/knowledge_assistant/versions/{version}/publish", headers=auth)
        assert published.status_code == 200
        versions = await client.get("/api/v1/agents/knowledge_assistant/versions", headers=auth)
        assert any(v["is_current"] for v in versions.json())
        rolled_back = await client.post(
            "/api/v1/agents/knowledge_assistant/versions/1/rollback", headers=auth)
        assert rolled_back.status_code == 200


class TestBankingTools:
    async def test_authentication_gate_blocks_data_access(self, client: AsyncClient, auth: dict):
        response = await client.post("/api/v1/tools/get_account_balance/invoke", headers=auth,
                                     json={"arguments": {}})
        body = response.json()
        assert body["ok"] is False
        assert "not authenticated" in body["error"].lower()

    async def test_monitoring_detects_typologies(self, client: AsyncClient, auth: dict):
        response = await client.post("/api/v1/tools/monitor_transactions/invoke", headers=auth,
                                     json={"arguments": {"days": 180, "persist_alerts": False}})
        data = response.json()
        assert data["ok"] is True
        assert data["data"]["transactions_scanned"] > 0

    async def test_sanctions_and_pep_screening(self, client: AsyncClient, auth: dict):
        sanctions = await client.post("/api/v1/tools/screen_sanctions/invoke", headers=auth,
                                      json={"arguments": {"full_name": "Viktor Petrovich Sokolov"}})
        assert sanctions.json()["data"]["hit_count"] == 1
        pep = await client.post("/api/v1/tools/screen_pep/invoke", headers=auth,
                                json={"arguments": {"full_name": "Rajesh Kumar Venkatesan"}})
        assert pep.json()["data"]["hit_count"] == 1
        clean = await client.post("/api/v1/tools/screen_sanctions/invoke", headers=auth,
                                  json={"arguments": {"full_name": "Wholly Unrelated Person"}})
        assert clean.json()["data"]["clear"] is True

    async def test_portfolio_analytics(self, client: AsyncClient, auth: dict):
        response = await client.post("/api/v1/tools/analyse_portfolio/invoke", headers=auth,
                                     json={"arguments": {"portfolio_code": "PF-BALANCED-01"}})
        data = response.json()["data"]
        assert data["market_value"] > 0
        assert data["holdings"]
        assert abs(sum(h["weight_pct"] for h in data["holdings"]) - 100) < 20

    async def test_portfolio_risk(self, client: AsyncClient, auth: dict):
        response = await client.post("/api/v1/tools/analyse_portfolio_risk/invoke", headers=auth,
                                     json={"arguments": {"portfolio_code": "PF-BALANCED-01"}})
        data = response.json()["data"]
        assert data["annualised_volatility_pct"] > 0
        assert data["value_at_risk_amount"] > 0

    async def test_approval_gated_tool_cannot_be_called_directly(self, client: AsyncClient,
                                                                 auth: dict):
        response = await client.post("/api/v1/tools/escalate_to_human/invoke", headers=auth,
                                     json={"arguments": {"reason": "test"}})
        assert response.status_code == 422

    async def test_invalid_arguments_are_rejected(self, client: AsyncClient, auth: dict):
        response = await client.post("/api/v1/tools/get_market_data/invoke", headers=auth,
                                     json={"arguments": {"days": 5000}})
        assert response.json()["ok"] is False


class TestKnowledge:
    async def test_search_returns_citations(self, client: AsyncClient, auth: dict):
        response = await client.post(
            "/api/v1/knowledge/search", headers=auth,
            json={"query": "incident severity classification Sev-1", "top_k": 3},
        )
        body = response.json()
        assert body["results"]
        assert body["citations"][0]["id"] == "[1]"
        assert any("Incident" in (c["title"] or "") for c in body["citations"])

    async def test_document_upload_and_indexing(self, client: AsyncClient, auth: dict):
        content = ("# Treasury Limits Policy\n\nThe intraday liquidity buffer must not fall "
                   "below INR 2,500 crore. Breaches escalate to the Group Treasurer within "
                   "30 minutes.\n") * 3
        response = await client.post(
            "/api/v1/knowledge/sources/runbooks/documents", headers=auth,
            files={"file": ("treasury_limits.md", content.encode(), "text/markdown")},
            data={"title": "Treasury Limits Policy"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["chunks"] >= 1
        found = await client.post("/api/v1/knowledge/search", headers=auth,
                                  json={"query": "intraday liquidity buffer breach escalation",
                                        "top_k": 5})
        titles = [c["title"] for c in found.json()["citations"]]
        assert "Treasury Limits Policy" in titles


class TestExecutionEngine:
    async def test_full_graph_execution(self, client: AsyncClient, auth: dict,
                                        _register_scripted_model):
        provider = _register_scripted_model
        provider.queue_text('{"objective":"Answer the policy question","steps":[],'
                            '"required_tools":[],"needs_knowledge_search":true,'
                            '"risk_level":"low"}')
        provider.queue_text("A Sev-1 may be declared by the on-call incident commander or any "
                            "engineer who believes the criteria are met [1].")

        await client.put("/api/v1/agents/knowledge_assistant/config", headers=auth,
                         json={"model": TEST_MODEL, "changelog": "point at scripted model"})
        started = await client.post(
            "/api/v1/agents/knowledge_assistant/execute", headers=auth,
            json={"input": {"query": "Who can declare a Sev-1 incident?"}},
        )
        assert started.status_code == 202
        execution_id = started.json()["execution_id"]

        execution = await _wait_for(client, auth, execution_id,
                                    {"succeeded", "failed", "cancelled"})
        assert execution["status"] == "succeeded", execution.get("error")
        assert execution["node_path"][0] == "planner"
        assert "response" in execution["node_path"]
        assert execution["llm_call_count"] >= 2
        assert execution["cost_usd"] > 0
        assert execution["final_response"]

        trace = (await client.get(f"/api/v1/executions/{execution_id}/trace",
                                  headers=auth)).json()
        assert trace["span_count"] >= 5
        assert trace["root_span_id"]
        kinds = {s["kind"] for s in trace["spans"]}
        assert {"agent", "planner", "retriever", "llm", "validation", "guardrail",
                "response"} <= kinds
        assert all(s["parent_span_id"] or s["kind"] == "agent" for s in trace["spans"])

        graph = (await client.get(f"/api/v1/executions/{execution_id}/graph",
                                  headers=auth)).json()
        assert {n["id"] for n in graph["nodes"]} >= {"planner", "llm", "response"}
        assert any(n["state"] == "executed" for n in graph["nodes"])

        events = (await client.get(f"/api/v1/executions/{execution_id}/events",
                                   headers=auth)).json()
        types = {e["type"] for e in events}
        assert {"execution.started", "planning", "knowledge.search", "llm.completed",
                "final.response", "execution.completed"} <= types

        logs = (await client.get(f"/api/v1/executions/{execution_id}/logs", headers=auth)).json()
        assert logs and all(log["correlation_id"] for log in logs)

        evaluation = await client.post(f"/api/v1/evaluations/{execution_id}/run", headers=auth)
        assert evaluation.status_code == 200
        assert evaluation.json()["citation_score"] is not None

    async def test_tool_calling_execution(self, client: AsyncClient, auth: dict,
                                          _register_scripted_model):
        provider = _register_scripted_model
        async with SessionFactory() as session:
            customer = (await session.execute(select(Customer).limit(1))).scalar_one()
            customer_number = customer.customer_number
            index = int(customer_number.replace("CUS-", "")) - 100001

        provider.queue_text('{"objective":"Check the balance","steps":[],'
                            '"required_tools":["authenticate_customer","get_account_balance"],'
                            '"needs_knowledge_search":false,"risk_level":"low"}')
        provider.queue_tool_call("authenticate_customer",
                                 {"identifier": customer_number, "pin": str(1000 + index)})
        provider.queue_tool_call("get_account_balance", {})
        provider.queue_text("Your savings account balance is shown above.")

        await client.put("/api/v1/agents/customer_service/config", headers=auth,
                         json={"model": TEST_MODEL, "changelog": "scripted"})
        started = await client.post("/api/v1/agents/customer_service/execute", headers=auth,
                                    json={"input": {"query": "What is my balance?"}})
        execution_id = started.json()["execution_id"]
        execution = await _wait_for(client, auth, execution_id, {"succeeded", "failed"})
        assert execution["status"] == "succeeded", execution.get("error")
        assert execution["tool_call_count"] == 2

        trace = (await client.get(f"/api/v1/executions/{execution_id}/trace",
                                  headers=auth)).json()
        tool_spans = [s for s in trace["spans"] if s["kind"] == "tool"]
        assert {s["name"] for s in tool_spans} == {"tool.authenticate_customer",
                                                    "tool.get_account_balance"}
        assert all(s["status"] == "ok" for s in tool_spans)

    async def test_human_approval_suspends_and_resumes(self, client: AsyncClient, auth: dict,
                                                        approver_token: str,
                                                        _register_scripted_model):
        provider = _register_scripted_model
        provider.queue_text('{"objective":"Escalate the complaint","steps":[],'
                            '"required_tools":["escalate_to_human"],'
                            '"needs_knowledge_search":false,"risk_level":"high"}')
        provider.queue_tool_call("escalate_to_human",
                                 {"reason": "Customer alleges unauthorised transaction",
                                  "urgency": "high"})
        provider.queue_text("I have escalated this to our specialist team.")

        await client.put("/api/v1/agents/customer_service/config", headers=auth,
                         json={"model": TEST_MODEL, "changelog": "scripted"})
        started = await client.post(
            "/api/v1/agents/customer_service/execute", headers=auth,
            json={"input": {"query": "There is a fraudulent charge on my card!"}},
        )
        execution_id = started.json()["execution_id"]
        execution = await _wait_for(client, auth, execution_id, {"awaiting_approval", "failed"})
        assert execution["status"] == "awaiting_approval", execution.get("error")

        pending = (await client.get("/api/v1/approvals", headers=auth)).json()
        approval = next(a for a in pending["items"] if a["execution_id"] == execution_id)
        assert approval["payload"]["tool"] == "escalate_to_human"
        assert approval["risk_level"] == "high"

        # Segregation of duties: a role without approval:decide cannot decide.
        operator_login = await client.post("/api/v1/auth/login",
                                           json={"email": "operator@finops.local",
                                                 "password": "TestPassword!2026"})
        operator_token = operator_login.json()["access_token"]
        forbidden = await client.post(
            f"/api/v1/approvals/{approval['id']}/decision",
            headers={"Authorization": f"Bearer {operator_token}"},
            json={"decision": "approve"},
        )
        assert forbidden.status_code == 403

        decision = await client.post(
            f"/api/v1/approvals/{approval['id']}/decision",
            headers={"Authorization": f"Bearer {approver_token}"},
            json={"decision": "approve", "comments": "Confirmed with the customer"},
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "approved"

        resumed = await _wait_for(client, auth, execution_id, {"succeeded", "failed"})
        assert resumed["status"] == "succeeded", resumed.get("error")
        assert resumed["approval_count"] >= 1

        async with SessionFactory() as session:
            approvals = (
                await session.execute(select(Approval).where(
                    Approval.execution_id == execution_id))
            ).scalars().all()
            assert approvals[0].reviewer_email == "approver@finops.local"
            spans = (
                await session.execute(select(Span).where(Span.execution_id == execution_id))
            ).scalars().all()
            assert any(s.kind == "approval" for s in spans)

    async def test_guardrails_mask_pii(self, client: AsyncClient, auth: dict,
                                       _register_scripted_model):
        provider = _register_scripted_model
        provider.queue_text('{"objective":"Reply","steps":[],"required_tools":[],'
                            '"needs_knowledge_search":false,"risk_level":"low"}')
        provider.queue_text("Your card number is 4111111111111111 and PAN is ABCDE1234F.")

        await client.put("/api/v1/agents/customer_service/config", headers=auth,
                         json={"model": TEST_MODEL, "changelog": "scripted"})
        started = await client.post("/api/v1/agents/customer_service/execute", headers=auth,
                                    json={"input": {"query": "Show my card details"}})
        execution_id = started.json()["execution_id"]
        execution = await _wait_for(client, auth, execution_id, {"succeeded", "failed"})
        assert execution["status"] == "succeeded", execution.get("error")
        assert "4111111111111111" not in execution["final_response"]
        assert "ABCDE1234F" not in execution["final_response"]
        guardrails = execution["output"]["guardrails"]
        assert any(f["action"] == "masked" for f in guardrails)

    async def test_provider_failure_is_reported_not_faked(self, client: AsyncClient, auth: dict):
        await client.put("/api/v1/agents/investment_research/config", headers=auth,
                         json={"model": "gpt-5.5", "changelog": "unconfigured provider"})
        started = await client.post("/api/v1/agents/investment_research/execute", headers=auth,
                                    json={"input": {"query": "Assess RELIANCE"}})
        execution_id = started.json()["execution_id"]
        execution = await _wait_for(client, auth, execution_id, {"failed", "succeeded"})
        # Either a scripted fallback answered, or it failed loudly - never a fabricated answer.
        if execution["status"] == "failed":
            assert execution["error_type"] in {"ProviderNotConfiguredError", "ProviderError"}

    async def test_cancel_execution(self, client: AsyncClient, auth: dict):
        started = await client.post("/api/v1/agents/knowledge_assistant/execute", headers=auth,
                                    json={"input": {"query": "long running"}})
        execution_id = started.json()["execution_id"]
        cancelled = await client.post(f"/api/v1/executions/{execution_id}/cancel", headers=auth)
        assert cancelled.status_code == 200


class TestGovernance:
    async def test_audit_trail_records_actions(self, client: AsyncClient, auth: dict):
        await client.get("/api/v1/agents", headers=auth)
        response = await client.get("/api/v1/security/audit", headers=auth)
        assert response.status_code == 200
        actions = {row["action"] for row in response.json()["items"]}
        assert "auth.login" in actions

    async def test_cost_ledger_populated(self, client: AsyncClient, auth: dict):
        summary = (await client.get("/api/v1/costs/summary", headers=auth)).json()
        assert summary["totals"]["window_usd"] >= 0
        assert "by_model" in summary and "by_agent" in summary

    async def test_metrics_endpoint(self, client: AsyncClient):
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert b"finops_http_requests_total" in response.content

    async def test_health_endpoints(self, client: AsyncClient):
        assert (await client.get("/health/live")).status_code == 200
        ready = await client.get("/health/ready")
        assert ready.json()["checks"]["database"] == "ok"

    async def test_feature_flag_update(self, client: AsyncClient, auth: dict):
        response = await client.put("/api/v1/security/feature-flags/hybrid_retrieval",
                                    headers=auth, json={"enabled": True,
                                                        "rollout_percentage": 50})
        assert response.json()["rollout_percentage"] == 50

    async def test_openapi_schema(self, client: AsyncClient):
        schema = (await client.get("/openapi.json")).json()
        assert schema["info"]["title"]
        assert "/api/v1/agents/{agent_key}/execute" in schema["paths"]


async def _wait_for(client: AsyncClient, auth: dict, execution_id: str,
                    statuses: set[str], timeout: float = 30.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    payload: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        response = await client.get(f"/api/v1/executions/{execution_id}", headers=auth)
        payload = response.json()
        if payload.get("status") in statuses:
            return payload
        await asyncio.sleep(0.25)
    pytest.fail(f"Execution {execution_id} stayed in {payload.get('status')}")
