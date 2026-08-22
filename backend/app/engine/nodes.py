"""Graph nodes.

Every node is an independent unit that opens a span, emits streaming events, writes
its own inputs/outputs to the trace, and can be retried in isolation. The node set
matches the published execution DAG:

    planner -> retriever -> memory -> llm <-> tools -> validation -> guardrails
            -> human_approval -> response
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.bus import APPROVAL_CHANNEL, bus
from app.core.config import settings
from app.core.errors import BudgetExceededError, GuardrailViolation
from app.core.logging import get_logger
from app.core.metrics import agent_retries_total, approvals_total
from app.db.models.agents import Approval, Span
from app.db.models.knowledge import KnowledgeSource, MemoryMessage, MemoryThread
from app.engine.events import EventEmitter, EventType
from app.engine.state import ExecutionState, PendingApproval
from app.guardrails.nemo import nemo_guardrails
from app.guardrails.patterns import (
    PII_PATTERNS,
    PROMPT_INJECTION_PATTERNS,
)
from app.guardrails.patterns import (
    mask as _mask,
)
from app.llm.jsonio import extract_json_object
from app.llm.router import router
from app.llm.types import Message, ToolCall
from app.rag.pipeline import pipeline
from app.tools.base import ToolContext, registry

log = get_logger("engine.nodes")


class ApprovalPause(Exception):
    """Raised to suspend an execution until a human decides."""

    def __init__(self, approval: PendingApproval):
        super().__init__(approval.title)
        self.approval = approval


@dataclass
class RunContext:
    session: AsyncSession
    state: ExecutionState
    emitter: EventEmitter
    agent: Any  # AgentSpec
    spans: list[Span] = field(default_factory=list)

    async def open_span(self, name: str, kind: str, *, inputs: dict[str, Any] | None = None) -> Span:
        span = Span(
            execution_id=self.state.execution_id,
            trace_id=self.state.trace_id,
            span_id=self.state.new_span_id(),
            parent_span_id=self.state.parent_span(),
            name=name,
            kind=kind,
            status="running",
            start_time=datetime.now(UTC),
            input_payload=_truncate(inputs or {}),
            attributes={"agent": self.state.agent_key, "node": self.state.current_node},
        )
        self.session.add(span)
        self.state.span_stack.append(span.span_id)
        await self.session.flush()
        return span

    async def close_span(
        self,
        span: Span,
        *,
        status: str = "ok",
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
        attributes: dict[str, Any] | None = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
        cost_usd: float = 0.0,
        retry_count: int = 0,
    ) -> None:
        span.end_time = datetime.now(UTC)
        span.duration_ms = (span.end_time - span.start_time).total_seconds() * 1000
        span.status = status
        span.output_payload = _truncate(outputs or {})
        span.error = error
        span.attributes = {**(span.attributes or {}), **(attributes or {})}
        span.tokens_input = tokens_input
        span.tokens_output = tokens_output
        span.cost_usd = cost_usd
        span.retry_count = retry_count
        if self.state.span_stack and self.state.span_stack[-1] == span.span_id:
            self.state.span_stack.pop()
        await self.session.flush()


def _truncate(payload: Any, limit: int = 8000) -> Any:
    try:
        raw = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return {"_unserialisable": str(payload)[:limit]}
    if len(raw) <= limit:
        return json.loads(raw)
    return {"_truncated": True, "preview": raw[:limit]}


class Node:
    key: str = "node"
    label: str = "Node"
    kind: str = "generic"

    async def should_run(self, ctx: RunContext) -> bool:
        return True

    async def run(self, ctx: RunContext) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def __call__(self, ctx: RunContext) -> None:
        ctx.state.current_node = self.key
        if not await self.should_run(ctx):
            await ctx.emitter.emit(
                EventType.NODE_SKIPPED, {"node": self.key, "label": self.label}, node=self.key
            )
            return
        ctx.state.node_path.append(self.key)
        await ctx.emitter.emit(
            EventType.NODE_STARTED,
            {"node": self.key, "label": self.label, "kind": self.kind},
            node=self.key,
            log_message=f"Node '{self.label}' started",
        )
        await self.run(ctx)
        await ctx.emitter.emit(
            EventType.NODE_COMPLETED, {"node": self.key, "label": self.label}, node=self.key
        )


# --- 0. Input rails ----------------------------------------------------------
class InputRailsNode(Node):
    """NeMo Guardrails input rails, before anything reads a system of record.

    Skipped for agents with no rail configuration. A refusal ends the run here — the
    request never reaches the planner, the retriever or a tool.
    """

    key = "input_rails"
    label = "Input Rails"
    kind = "guardrail"

    async def should_run(self, ctx: RunContext) -> bool:
        return nemo_guardrails.covers(ctx.agent.key)

    async def run(self, ctx: RunContext) -> None:
        state = ctx.state
        text = _user_text(state.input)
        span = await ctx.open_span("guardrails.input_rails", "guardrail", inputs={"chars": len(text)})
        result = await nemo_guardrails.check_input(
            ctx.agent.key,
            text,
            context={"pii_allowlist": list(ctx.agent.pii_allowlist or [])},
        )
        findings = [finding.to_dict() for finding in result.findings]
        state.guardrail_findings.extend(findings)
        await ctx.close_span(
            span,
            status="error" if result.blocked else "ok",
            outputs={
                "evaluated": result.evaluated,
                "blocked": result.blocked,
                "findings": findings,
                "llm_rails": result.llm_rails,
            },
        )
        await ctx.emitter.emit(
            EventType.GUARDRAIL,
            {
                "rails": "input",
                "engine": "nemo",
                "findings": findings,
                "blocked": result.blocked,
                "evaluated": result.evaluated,
                "llm_rails": result.llm_rails,
                "reason": result.reason,
            },
            node=self.key,
            log_message=f"Input rails: {len(findings)} findings{', blocked' if result.blocked else ''}",
        )
        if result.blocked and settings.nemo_block_on_input_rail:
            raise GuardrailViolation(
                "Request blocked by input guardrails",
                details={"findings": findings, "reason": result.reason},
            )


def _user_text(payload: dict[str, Any]) -> str:
    """The natural-language part of an agent input, which is what a rail reads."""
    for key in ("query", "question", "message", "request", "text", "narrative"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return json.dumps(payload, default=str)


# --- 1. Planner --------------------------------------------------------------
PLANNER_INSTRUCTIONS = """You are the planning stage of a regulated financial-services AI agent.
Produce a concise execution plan for the user request.

Return ONLY a JSON object:
{
  "objective": "one sentence restating the goal",
  "steps": [{"step": 1, "action": "...", "tool": "tool_name or null", "why": "..."}],
  "required_tools": ["tool_name"],
  "needs_knowledge_search": true/false,
  "risk_level": "low|medium|high",
  "clarifications": ["..."]
}"""


class PlannerNode(Node):
    key = "planner"
    label = "Planner"
    kind = "planner"

    async def run(self, ctx: RunContext) -> None:
        state = ctx.state
        available = registry.by_names(ctx.agent.tools)
        tool_summary = "\n".join(f"- {t.name}: {t.description}" for t in available) or "- (none)"
        span = await ctx.open_span("planner.llm", "planner", inputs={"tools": len(available)})
        prompt = (
            f"{PLANNER_INSTRUCTIONS}\n\nAgent: {ctx.agent.name}\nAgent purpose: "
            f"{ctx.agent.description}\n\nAvailable tools:\n{tool_summary}\n\n"
            f"User request:\n{json.dumps(state.input, default=str)[:4000]}"
        )
        try:
            plan, response = await router.chat_json(
                messages=[Message(role="user", content=prompt)],
                model=ctx.agent.planner_model or state.model,
                temperature=0.0,
                max_tokens=900,
                context={
                    "execution_id": state.execution_id,
                    "agent_key": state.agent_key,
                    "span_id": span.span_id,
                    "user_id": state.user_id,
                    "user_email": state.user_email,
                    "department": state.department,
                },
            )
        except Exception as exc:
            await ctx.close_span(span, status="error", error=str(exc))
            raise
        state.llm_calls += 1
        state.record_usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached=response.usage.cached_input_tokens,
            cost=response.cost_usd,
        )
        plan = plan or {
            "objective": str(state.input.get("query") or state.input.get("objective") or "")[:400],
            "steps": [],
            "required_tools": [],
            "needs_knowledge_search": bool(ctx.agent.knowledge_sources),
            "risk_level": "medium",
            "raw": response.content[:2000],
        }
        state.plan = plan
        state.reasoning_log.append(f"Plan: {plan.get('objective', '')}")
        await ctx.close_span(
            span,
            outputs=plan,
            attributes={"model": response.model, "provider": response.provider},
            tokens_input=response.usage.input_tokens,
            tokens_output=response.usage.output_tokens,
            cost_usd=response.cost_usd,
        )
        await ctx.emitter.emit(
            EventType.PLANNING,
            {"plan": plan, "model": response.model, "latency_ms": round(response.latency_ms, 2)},
            node=self.key,
            log_message=f"Plan created with {len(plan.get('steps', []))} steps",
        )


def _parse_json(text: str) -> dict[str, Any] | None:
    """Kept as the module-local name; the scan itself is shared with the router."""
    return extract_json_object(text)


# --- 2. Retriever -------------------------------------------------------------
class RetrieverNode(Node):
    key = "retriever"
    label = "Knowledge Retriever"
    kind = "retriever"

    async def should_run(self, ctx: RunContext) -> bool:
        if not ctx.agent.knowledge_sources:
            return False
        if ctx.state.plan and ctx.state.plan.get("needs_knowledge_search") is False:
            return False
        return True

    async def run(self, ctx: RunContext) -> None:
        state = ctx.state
        query = str(
            state.input.get("query")
            or state.input.get("question")
            or (state.plan or {}).get("objective")
            or ""
        ).strip()
        if not query:
            return
        span = await ctx.open_span(
            "retriever.hybrid_search",
            "retriever",
            inputs={"query": query[:500], "sources": ctx.agent.knowledge_sources},
        )
        sources = (
            (
                await ctx.session.execute(
                    select(KnowledgeSource).where(KnowledgeSource.key.in_(ctx.agent.knowledge_sources))
                )
            )
            .scalars()
            .all()
        )
        source_keys = [s.key for s in sources if s.enabled]
        result = await pipeline.search(
            ctx.session,
            query,
            top_k=ctx.agent.retrieval_top_k,
            source_keys=source_keys or None,
            agent_key=state.agent_key,
            execution_id=state.execution_id,
        )
        state.retrieved = [m.to_dict() for m in result.matches]
        state.citations = result.to_citations()
        state.scratch["retrieval_context"] = result.as_prompt_block()
        await ctx.close_span(
            span,
            outputs={
                "matches": len(result.matches),
                "top_score": result.matches[0].score if result.matches else None,
            },
            attributes={
                "backend": result.backend,
                "embedding_model": result.embedding_model,
                "method": result.method,
                "latency_ms": round(result.latency_ms, 2),
            },
        )
        await ctx.emitter.emit(
            EventType.KNOWLEDGE_SEARCH,
            {
                "query": query[:500],
                "results": len(result.matches),
                "backend": result.backend,
                "embedding_model": result.embedding_model,
                "latency_ms": round(result.latency_ms, 2),
                "citations": state.citations,
            },
            node=self.key,
            log_message=f"Knowledge search returned {len(result.matches)} chunks in "
            f"{result.latency_ms:.0f}ms",
        )
        await ctx.emitter.emit(
            EventType.VECTOR_SEARCH,
            {
                "backend": result.backend,
                "top_k": ctx.agent.retrieval_top_k,
                "scores": [round(m.score, 4) for m in result.matches],
            },
            node=self.key,
        )


# --- 3. Memory ----------------------------------------------------------------
class MemoryNode(Node):
    key = "memory"
    label = "Conversation Memory"
    kind = "memory"

    async def should_run(self, ctx: RunContext) -> bool:
        return bool(ctx.agent.memory_enabled and ctx.state.thread_id)

    async def run(self, ctx: RunContext) -> None:
        state = ctx.state
        span = await ctx.open_span("memory.load", "memory", inputs={"thread": state.thread_id})
        thread = (
            await ctx.session.execute(select(MemoryThread).where(MemoryThread.thread_key == state.thread_id))
        ).scalar_one_or_none()
        if thread is None:
            thread = MemoryThread(
                thread_key=state.thread_id or "",
                agent_key=state.agent_key,
                subject_id=str(state.input.get("customer_id") or "") or None,
                title=str(state.input.get("query", ""))[:280],
                last_activity_at=datetime.now(UTC),
            )
            ctx.session.add(thread)
            await ctx.session.flush()
        messages = (
            (
                await ctx.session.execute(
                    select(MemoryMessage)
                    .where(MemoryMessage.thread_id == thread.id)
                    .order_by(MemoryMessage.created_at.desc())
                    .limit(ctx.agent.memory_window)
                )
            )
            .scalars()
            .all()
        )
        history = list(reversed(messages))
        state.scratch["memory_thread_id"] = thread.id
        state.memory_summary = thread.summary or None
        for m in history:
            state.messages.append(Message(role=m.role, content=m.content))  # type: ignore[arg-type]
        await ctx.close_span(
            span,
            outputs={"messages_loaded": len(history), "has_summary": bool(thread.summary)},
            attributes={"thread_id": thread.id, "facts": len(thread.facts or [])},
        )
        await ctx.emitter.emit(
            EventType.MEMORY_RETRIEVAL,
            {
                "thread": state.thread_id,
                "messages": len(history),
                "summary": (thread.summary or "")[:400],
                "facts": thread.facts or [],
            },
            node=self.key,
            log_message=f"Loaded {len(history)} memory messages",
        )


# --- 4/5. Reasoning loop (LLM + tools) ---------------------------------------
class ReasoningNode(Node):
    """The agentic loop: LLM decides, tools execute, results feed back."""

    key = "llm"
    label = "Reasoning & Tool Use"
    kind = "llm"

    async def run(self, ctx: RunContext) -> None:
        state = ctx.state
        agent = ctx.agent

        if state.pending_tool_calls:
            # Resuming after a human decision: replay the suspended tool calls first.
            pending = state.pending_tool_calls
            state.pending_tool_calls = []
            await self._execute_tools(ctx, pending)
        elif not state.messages or state.messages[-1].role != "user":
            state.messages.extend(agent.build_messages(state))

        tool_schemas = registry.schemas_for(agent.tools)
        max_iterations = agent.max_iterations

        while state.iteration < max_iterations:
            state.iteration += 1
            if state.budget.exhausted:
                raise BudgetExceededError(
                    "Execution budget exhausted",
                    details={
                        "spent_usd": round(state.budget.spent_usd, 4),
                        "cap_usd": state.budget.max_cost_usd,
                    },
                )

            state.current_node = self.key
            span = await ctx.open_span(
                f"llm.completion#{state.iteration}",
                "llm",
                inputs={
                    "messages": len(state.messages),
                    "tools": len(tool_schemas),
                    "iteration": state.iteration,
                },
            )
            await ctx.emitter.emit(
                EventType.LLM_STARTED,
                {
                    "iteration": state.iteration,
                    "messages": len(state.messages),
                    "tools_offered": [t.name for t in tool_schemas],
                },
                node=self.key,
                span_id=span.span_id,
            )
            try:
                response = await router.chat(
                    messages=state.messages,
                    model=state.model,
                    tools=tool_schemas or None,
                    temperature=agent.temperature,
                    max_tokens=agent.max_tokens,
                    context={
                        "execution_id": state.execution_id,
                        "agent_key": state.agent_key,
                        "span_id": span.span_id,
                        "user_id": state.user_id,
                        "user_email": state.user_email,
                        "department": state.department,
                    },
                )
            except Exception as exc:
                await ctx.close_span(span, status="error", error=str(exc))
                await ctx.emitter.emit(
                    EventType.NODE_FAILED,
                    {"node": self.key, "error": str(exc)},
                    node=self.key,
                    log_message=f"LLM call failed: {exc}",
                )
                raise

            state.llm_calls += 1
            state.model = state.model or response.model
            state.provider = response.provider
            state.record_usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cached=response.usage.cached_input_tokens,
                cost=response.cost_usd,
            )
            await ctx.close_span(
                span,
                outputs={
                    "content": response.content[:4000],
                    "tool_calls": [tc.name for tc in response.tool_calls],
                    "finish_reason": response.finish_reason,
                    "truncated": response.truncated,
                },
                attributes={
                    "model": response.model,
                    "provider": response.provider,
                    "request_id": response.request_id,
                    "latency_ms": round(response.latency_ms, 2),
                    "truncated": response.truncated,
                },
                tokens_input=response.usage.input_tokens,
                tokens_output=response.usage.output_tokens,
                cost_usd=response.cost_usd,
            )
            await ctx.emitter.emit(
                EventType.LLM_COMPLETED,
                {
                    "model": response.model,
                    "provider": response.provider,
                    "latency_ms": round(response.latency_ms, 2),
                    "tokens": {
                        "input": response.usage.input_tokens,
                        "output": response.usage.output_tokens,
                        "cached": response.usage.cached_input_tokens,
                    },
                    "cost_usd": round(response.cost_usd, 6),
                    "content_preview": response.content[:600],
                    "truncated": response.truncated,
                    "tool_calls": [
                        {"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls
                    ],
                },
                node=self.key,
                span_id=span.span_id,
                log_message=f"LLM {response.model} responded in {response.latency_ms:.0f}ms "
                f"({response.usage.total} tokens, ${response.cost_usd:.4f})"
                + (" - CUT OFF at the output ceiling" if response.truncated else ""),
            )
            if response.truncated:
                # Visible in the run's own reasoning log, not only in the server log: the
                # person reading the answer is the one who needs to know it is unfinished.
                state.reasoning_log.append(
                    f"Reply hit the {agent.max_tokens}-token ceiling and is incomplete."
                )
            await ctx.session.commit()
            if response.content:
                state.reasoning_log.append(response.content[:1500])
                await ctx.emitter.emit(EventType.REASONING, {"text": response.content[:2000]}, node=self.key)

            state.messages.append(
                Message(role="assistant", content=response.content, tool_calls=response.tool_calls)
            )

            if not response.tool_calls:
                state.final_response = response.content
                return

            await self._execute_tools(ctx, response.tool_calls)

        state.final_response = state.final_response or (state.messages[-1].content if state.messages else "")
        state.validation_findings.append(
            {
                "check": "iteration_limit",
                "status": "warning",
                "detail": f"Reached max iterations ({max_iterations})",
            }
        )

    async def _execute_tools(self, ctx: RunContext, tool_calls: list[ToolCall]) -> None:
        state = ctx.state
        state.current_node = "tools"
        completed = {m.tool_call_id for m in state.messages if m.role == "tool" and m.tool_call_id}
        for call in tool_calls:
            if call.id in completed:
                continue
            if not registry.has(call.name):
                state.messages.append(
                    Message(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=json.dumps({"ok": False, "error": f"Unknown tool '{call.name}'"}),
                    )
                )
                continue

            tool = registry.get(call.name)
            needs_approval = tool.requires_approval and not state.approved_tool_calls.get(call.id)
            if needs_approval:
                approval = await create_approval(
                    ctx,
                    node="tools",
                    title=f"Approve tool call: {tool.name}",
                    summary=f"{ctx.agent.name} requests to run '{tool.name}' ({tool.description[:200]})",
                    payload={
                        "tool": tool.name,
                        "arguments": call.arguments,
                        "tool_call_id": call.id,
                        "writes_data": tool.writes_data,
                    },
                    risk=tool.approval_risk,
                )
                state.pending_tool_calls = tool_calls
                raise ApprovalPause(approval)

            if state.approved_tool_calls.get(call.id) is False:
                state.messages.append(
                    Message(
                        role="tool",
                        tool_call_id=call.id,
                        name=call.name,
                        content=json.dumps({"ok": False, "error": "Rejected by human reviewer"}),
                    )
                )
                continue

            span = await ctx.open_span(f"tool.{call.name}", "tool", inputs={"arguments": call.arguments})
            await ctx.emitter.emit(
                EventType.TOOL_STARTED,
                {"tool": call.name, "arguments": call.arguments},
                node="tools",
                span_id=span.span_id,
                log_message=f"Invoking tool '{call.name}'",
            )
            result = await registry.invoke(
                call.name,
                call.arguments,
                ToolContext(
                    execution_id=state.execution_id,
                    agent_key=state.agent_key,
                    user_id=state.user_id,
                    user_email=state.user_email,
                    roles=state.roles,
                    correlation_id=state.correlation_id,
                    trace_id=state.trace_id,
                    session=ctx.session,
                    state=state.scratch,
                ),
            )
            state.tool_calls += 1
            state.retry_count += result.retries
            if result.retries:
                agent_retries_total.labels(state.agent_key, call.name).inc(result.retries)
                await ctx.emitter.emit(
                    EventType.RETRY,
                    {"tool": call.name, "retries": result.retries},
                    node="tools",
                    log_message=f"Tool '{call.name}' retried {result.retries}x",
                )
            state.tool_results.append(result.to_dict())
            await ctx.close_span(
                span,
                status="ok" if result.ok else "error",
                outputs={"data": result.data} if result.ok else {"error": result.error},
                error=result.error,
                retry_count=result.retries,
                attributes={"category": tool.category, "writes_data": tool.writes_data},
            )
            await ctx.emitter.emit(
                EventType.TOOL_COMPLETED if result.ok else EventType.TOOL_FAILED,
                {
                    "tool": call.name,
                    "ok": result.ok,
                    "latency_ms": round(result.latency_ms, 2),
                    "retries": result.retries,
                    "result_preview": _truncate(result.data, 2000) if result.ok else result.error,
                },
                node="tools",
                span_id=span.span_id,
                log_message=f"Tool '{call.name}' "
                f"{'succeeded' if result.ok else 'failed: ' + str(result.error)} "
                f"in {result.latency_ms:.0f}ms",
            )
            await ctx.session.commit()
            if tool.category in {"database", "banking"}:
                await ctx.emitter.emit(
                    EventType.DATABASE_QUERY, {"tool": call.name, "ok": result.ok}, node="tools"
                )
            elif tool.category in {"http", "external", "market"}:
                await ctx.emitter.emit(EventType.API_CALL, {"tool": call.name, "ok": result.ok}, node="tools")

            state.messages.append(
                Message(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=json.dumps(result.to_dict(), default=str)[:12000],
                )
            )
        state.current_node = self.key


async def create_approval(
    ctx: RunContext,
    *,
    node: str,
    title: str,
    summary: str,
    payload: dict[str, Any],
    risk: str = "medium",
) -> PendingApproval:
    state = ctx.state
    approval = Approval(
        execution_id=state.execution_id,
        agent_key=state.agent_key,
        node=node,
        title=title[:240],
        summary=summary,
        payload=payload,
        risk_level=risk,
        required_role=ctx.agent.approval_role,
        requested_by=state.user_email,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.approval_timeout_seconds),
        timeline=[
            {"at": datetime.now(UTC).isoformat(), "event": "requested", "by": state.user_email or "system"}
        ],
    )
    ctx.session.add(approval)
    await ctx.session.flush()
    state.approval_count += 1
    approvals_total.labels(state.agent_key, "requested").inc()

    span = await ctx.open_span("approval.request", "approval", inputs=payload)
    await ctx.close_span(span, status="ok", outputs={"approval_id": approval.id, "risk": risk})
    await ctx.emitter.emit(
        EventType.APPROVAL_REQUESTED,
        {
            "approval_id": approval.id,
            "title": approval.title,
            "summary": summary,
            "risk": risk,
            "payload": payload,
            "required_role": ctx.agent.approval_role,
        },
        node=node,
        log_message=f"Human approval requested: {title}",
    )
    await bus.publish(
        APPROVAL_CHANNEL,
        {
            "type": "approval.requested",
            "approval_id": approval.id,
            "execution_id": state.execution_id,
            "agent_key": state.agent_key,
            "title": approval.title,
            "risk": risk,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    return PendingApproval(
        approval_id=approval.id,
        tool_call=payload,
        node=node,
        title=approval.title,
        summary=summary,
        risk=risk,
    )


# --- 6. Validation -------------------------------------------------------------
class ValidationNode(Node):
    key = "validation"
    label = "Validation"
    kind = "validation"

    async def run(self, ctx: RunContext) -> None:
        state = ctx.state
        span = await ctx.open_span("validation.checks", "validation")
        findings: list[dict[str, Any]] = []
        response = state.final_response or ""

        findings.append(
            {
                "check": "non_empty_response",
                "status": "pass" if response.strip() else "fail",
                "detail": f"{len(response)} characters",
            }
        )
        failed_tools = [t for t in state.tool_results if not t["ok"]]
        findings.append(
            {
                "check": "tool_success",
                "status": "pass" if not failed_tools else "warning",
                "detail": f"{len(state.tool_results) - len(failed_tools)}/{len(state.tool_results)} "
                f"tool calls succeeded",
            }
        )
        if ctx.agent.require_citations:
            has_citation = bool(re.search(r"\[\d+\]", response)) and bool(state.citations)
            findings.append(
                {
                    "check": "citations_present",
                    "status": "pass" if has_citation else "fail",
                    "detail": f"{len(state.citations)} sources retrieved",
                }
            )
        if ctx.agent.output_schema:
            parsed = _parse_json(response)
            missing = [k for k in ctx.agent.output_schema if parsed is None or k not in parsed]
            findings.append(
                {
                    "check": "output_schema",
                    "status": "pass" if not missing else "warning",
                    "detail": f"missing keys: {missing}" if missing else "all required keys present",
                }
            )
            if parsed:
                state.output.update(parsed)
        grounded = _groundedness(response, state.retrieved)
        findings.append(
            {
                "check": "groundedness",
                "status": "pass" if grounded >= 0.35 or not state.retrieved else "warning",
                "detail": f"overlap score {grounded:.2f}",
                "score": round(grounded, 4),
            }
        )
        state.validation_findings.extend(findings)
        failed = [f for f in findings if f["status"] == "fail"]
        await ctx.close_span(span, status="ok" if not failed else "error", outputs={"findings": findings})
        await ctx.emitter.emit(
            EventType.VALIDATION,
            {"findings": findings, "failed": len(failed)},
            node=self.key,
            log_message=f"Validation: {len(findings) - len(failed)}/{len(findings)} checks passed",
        )
        if failed and ctx.agent.strict_validation:
            raise GuardrailViolation("Response failed validation", details={"findings": failed})


def _groundedness(response: str, retrieved: list[dict[str, Any]]) -> float:
    if not retrieved or not response:
        return 1.0
    source_terms: set[str] = set()
    for match in retrieved:
        source_terms.update(re.findall(r"[a-z0-9]{4,}", (match.get("content") or "").lower()))
    response_terms = set(re.findall(r"[a-z0-9]{4,}", response.lower()))
    if not response_terms:
        return 0.0
    return len(response_terms & source_terms) / len(response_terms)


# --- 7. Guardrails -------------------------------------------------------------
class GuardrailNode(Node):
    key = "guardrails"
    label = "Guardrails"
    kind = "guardrail"

    async def run(self, ctx: RunContext) -> None:
        state = ctx.state
        span = await ctx.open_span("guardrails.evaluate", "guardrail")
        findings: list[dict[str, Any]] = []
        response = state.final_response or ""
        original = response

        user_text = json.dumps(state.input, default=str)
        for pattern in PROMPT_INJECTION_PATTERNS:
            if pattern.search(user_text):
                findings.append(
                    {
                        "rule": "prompt_injection",
                        "severity": "high",
                        "action": "flagged",
                        "detail": pattern.pattern,
                    }
                )

        if ctx.agent.mask_pii:
            for label, pattern in PII_PATTERNS:
                if label in ctx.agent.pii_allowlist:
                    continue
                matches = pattern.findall(response)
                if matches:
                    response = pattern.sub(lambda m: _mask(m.group(0)), response)
                    findings.append(
                        {
                            "rule": f"pii_{label}",
                            "severity": "medium",
                            "action": "masked",
                            "count": len(matches),
                        }
                    )

        for term in ctx.agent.blocked_terms:
            if re.search(rf"\b{re.escape(term)}\b", response, re.I):
                findings.append(
                    {"rule": "blocked_term", "severity": "high", "action": "blocked", "term": term}
                )

        if ctx.agent.required_disclaimer and ctx.agent.required_disclaimer not in response:
            response = f"{response}\n\n_{ctx.agent.required_disclaimer}_"
            findings.append({"rule": "disclaimer", "severity": "low", "action": "appended"})

        # NeMo output rails run last, over the response the built-in rules already
        # cleaned, so a rail sees exactly what a caller would receive.
        rail_result = await nemo_guardrails.check_output(
            ctx.agent.key,
            response,
            user_text=_user_text(state.input),
            context={"pii_allowlist": list(ctx.agent.pii_allowlist or [])},
        )
        if rail_result.evaluated:
            findings.extend(finding.to_dict() for finding in rail_result.findings)
            if rail_result.text and not rail_result.blocked:
                response = rail_result.text

        blocked = [f for f in findings if f["action"] == "blocked"]
        state.guardrail_findings.extend(findings)
        state.final_response = response
        await ctx.close_span(
            span,
            status="error" if blocked else "ok",
            outputs={
                "findings": findings,
                "modified": response != original,
                "nemo_evaluated": rail_result.evaluated,
            },
        )
        await ctx.emitter.emit(
            EventType.GUARDRAIL,
            {
                "rails": "output",
                "findings": findings,
                "modified": response != original,
                "blocked": bool(blocked),
                "nemo_evaluated": rail_result.evaluated,
                "llm_rails": rail_result.llm_rails,
            },
            node=self.key,
            log_message=f"Guardrails applied: {len(findings)} findings",
        )
        if blocked:
            raise GuardrailViolation("Response blocked by guardrails", details={"findings": blocked})


# --- 8. Human approval ---------------------------------------------------------
class HumanApprovalNode(Node):
    key = "human_approval"
    label = "Human Approval"
    kind = "approval"

    async def should_run(self, ctx: RunContext) -> bool:
        if not ctx.agent.final_approval_required:
            return False
        if ctx.state.scratch.get("final_approval_granted"):
            return False
        risk = (ctx.state.plan or {}).get("risk_level", "medium")
        if ctx.agent.final_approval_risk_threshold == "always":
            return True
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        return order.get(risk, 1) >= order.get(ctx.agent.final_approval_risk_threshold, 1)

    async def run(self, ctx: RunContext) -> None:
        approval = await create_approval(
            ctx,
            node=self.key,
            title=f"Approve {ctx.agent.name} response",
            summary=(ctx.state.final_response or "")[:1500],
            payload={
                "response": ctx.state.final_response,
                "citations": ctx.state.citations,
                "tool_calls": [t["tool"] for t in ctx.state.tool_results],
                "final": True,
            },
            risk=(ctx.state.plan or {}).get("risk_level", "medium"),
        )
        raise ApprovalPause(approval)


# --- 9. Response ---------------------------------------------------------------
class ResponseNode(Node):
    key = "response"
    label = "Response"
    kind = "response"

    async def run(self, ctx: RunContext) -> None:
        state = ctx.state
        span = await ctx.open_span("response.finalise", "response")
        state.output = {
            **state.output,
            "response": state.final_response,
            "citations": state.citations,
            "plan": state.plan,
            "tool_calls": [
                {"tool": t["tool"], "ok": t["ok"], "latency_ms": t["latency_ms"]} for t in state.tool_results
            ],
            "validation": state.validation_findings,
            "guardrails": state.guardrail_findings,
            "artifacts": state.artifacts,
            "usage": {
                "tokens_input": state.tokens_input,
                "tokens_output": state.tokens_output,
                "tokens_cached": state.tokens_cached,
                "cost_usd": round(state.cost_usd, 6),
                "llm_calls": state.llm_calls,
                "tool_calls": state.tool_calls,
            },
        }
        if ctx.agent.memory_enabled and state.scratch.get("memory_thread_id"):
            await self._persist_memory(ctx)
        await ctx.close_span(span, outputs={"response_chars": len(state.final_response or "")})
        await ctx.emitter.emit(
            EventType.FINAL_RESPONSE,
            {"response": state.final_response, "citations": state.citations},
            node=self.key,
            log_message="Final response produced",
        )

    async def _persist_memory(self, ctx: RunContext) -> None:
        state = ctx.state
        thread_id = state.scratch["memory_thread_id"]
        thread = (
            await ctx.session.execute(select(MemoryThread).where(MemoryThread.id == thread_id))
        ).scalar_one_or_none()
        if thread is None:
            return
        user_text = str(state.input.get("query") or state.input.get("message") or "")
        if user_text:
            ctx.session.add(
                MemoryMessage(
                    thread_id=thread.id, role="user", content=user_text, execution_id=state.execution_id
                )
            )
        ctx.session.add(
            MemoryMessage(
                thread_id=thread.id,
                role="assistant",
                content=state.final_response or "",
                execution_id=state.execution_id,
            )
        )
        thread.message_count += 2
        thread.last_activity_at = datetime.now(UTC)
        if state.scratch.get("sentiment"):
            thread.sentiment = state.scratch["sentiment"].get("label")
            thread.sentiment_score = state.scratch["sentiment"].get("score")
        summary_parts = [thread.summary or ""]
        summary_parts.append(f"[{datetime.now(UTC).date().isoformat()}] {user_text[:160]}")
        thread.summary = "\n".join(p for p in summary_parts if p)[-4000:]


DEFAULT_NODES: list[Node] = [
    InputRailsNode(),
    PlannerNode(),
    RetrieverNode(),
    MemoryNode(),
    ReasoningNode(),
    ValidationNode(),
    GuardrailNode(),
    HumanApprovalNode(),
    ResponseNode(),
]

GRAPH_DEFINITION = [
    {
        "id": "input_rails",
        "label": "Input Rails",
        "kind": "guardrail",
        "description": "NeMo Guardrails input rails: injection, control bypass, out-of-mandate "
        "requests and financial-crime facilitation.",
    },
    {
        "id": "planner",
        "label": "Planner",
        "kind": "planner",
        "description": "Decomposes the request into an executable plan.",
    },
    {
        "id": "retriever",
        "label": "Retriever",
        "kind": "retriever",
        "description": "Hybrid vector + keyword retrieval over connected knowledge.",
    },
    {
        "id": "memory",
        "label": "Memory",
        "kind": "memory",
        "description": "Loads conversation history and durable facts.",
    },
    {
        "id": "llm",
        "label": "LLM",
        "kind": "llm",
        "description": "Reasoning loop that selects tools and drafts the answer.",
    },
    {
        "id": "tools",
        "label": "Tools",
        "kind": "tool",
        "description": "Executes tool calls against systems of record.",
    },
    {
        "id": "validation",
        "label": "Validation",
        "kind": "validation",
        "description": "Schema, citation, groundedness and tool-success checks.",
    },
    {
        "id": "guardrails",
        "label": "Guardrails",
        "kind": "guardrail",
        "description": "PII masking, blocked terms, disclaimers, and NeMo Guardrails "
        "output rails including tipping-off detection.",
    },
    {
        "id": "human_approval",
        "label": "Human Approval",
        "kind": "approval",
        "description": "Suspends execution for a reviewer decision when policy requires.",
    },
    {
        "id": "response",
        "label": "Response",
        "kind": "response",
        "description": "Finalises output, citations, artifacts and memory.",
    },
]

GRAPH_EDGES = [
    {"source": "input_rails", "target": "planner"},
    {"source": "planner", "target": "retriever"},
    {"source": "retriever", "target": "memory"},
    {"source": "memory", "target": "llm"},
    {"source": "llm", "target": "tools", "label": "tool call"},
    {"source": "tools", "target": "llm", "label": "observation"},
    {"source": "llm", "target": "validation", "label": "final answer"},
    {"source": "validation", "target": "guardrails"},
    {"source": "guardrails", "target": "human_approval"},
    {"source": "human_approval", "target": "response"},
]
