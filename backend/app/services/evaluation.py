"""Automatic evaluation of completed executions.

Deterministic metrics (citation coverage, tool success, groundedness overlap) are always
computed. When an LLM provider is configured, an additional LLM-as-judge pass scores
faithfulness and answer relevance; without one, those fields stay null rather than being
filled with a guess.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.agents import Evaluation, Execution
from app.llm.router import router
from app.llm.types import Message

log = get_logger("evaluation")

JUDGE_PROMPT = """You are an impartial evaluator of an AI agent response in a regulated bank.

Score strictly from 0.0 to 1.0 and return ONLY JSON:
{
  "faithfulness": 0-1,        // claims supported by the provided context and tool outputs
  "answer_relevance": 0-1,    // does it answer the question actually asked
  "hallucination_score": 0-1, // 0 = no unsupported claims, 1 = largely fabricated
  "reasoning": "two sentences maximum"
}"""


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{4,}", (text or "").lower()))


async def evaluate_execution(session: AsyncSession, execution_id: str) -> dict[str, Any]:
    execution = (
        await session.execute(select(Execution).where(Execution.id == execution_id))
    ).scalar_one_or_none()
    if execution is None:
        raise NotFoundError("Execution not found")
    if execution.status != "succeeded":
        raise ValidationError(f"Only succeeded executions can be evaluated (status: "
                              f"{execution.status})")

    output = execution.output or {}
    response = execution.final_response or ""
    citations = output.get("citations") or []
    tool_calls = output.get("tool_calls") or []

    successful_tools = [t for t in tool_calls if t.get("ok")]
    tool_success_rate = (len(successful_tools) / len(tool_calls)) if tool_calls else 1.0

    cited_markers = set(re.findall(r"\[(\d+)\]", response))
    citation_score = (
        len(cited_markers) / len(citations) if citations else (1.0 if not citations else 0.0)
    )
    citation_score = min(citation_score, 1.0)

    context_tokens: set[str] = set()
    for citation in citations:
        context_tokens |= _tokens(citation.get("excerpt", ""))
    for call in tool_calls:
        context_tokens |= _tokens(json.dumps(call, default=str))
    response_tokens = _tokens(response)
    groundedness = (
        len(response_tokens & context_tokens) / len(response_tokens)
        if response_tokens and context_tokens else (1.0 if not context_tokens else 0.0)
    )

    faithfulness: float | None = None
    answer_relevance: float | None = None
    hallucination: float | None = None
    judge_reasoning: str | None = None
    judge_model: str | None = None

    if router.available_models(embeddings=False):
        try:
            context_block = json.dumps({
                "question": execution.input,
                "citations": citations[:8],
                "tool_results": tool_calls[:12],
            }, default=str)[:12000]
            judgement = await router.chat(
                messages=[
                    Message(role="system", content=JUDGE_PROMPT),
                    Message(role="user",
                            content=f"CONTEXT:\n{context_block}\n\nRESPONSE:\n{response[:8000]}"),
                ],
                temperature=0.0, max_tokens=500, json_mode=True, tier="fast",
                context={"agent_key": "evaluator", "execution_id": execution_id},
            )
            judge_model = judgement.model
            parsed = json.loads(re.search(r"\{.*\}", judgement.content, re.S).group(0))
            faithfulness = float(parsed.get("faithfulness", 0))
            answer_relevance = float(parsed.get("answer_relevance", 0))
            hallucination = float(parsed.get("hallucination_score", 0))
            judge_reasoning = str(parsed.get("reasoning", ""))[:600]
        except Exception as exc:
            log.warning("llm_judge_failed", execution_id=execution_id, error=str(exc))

    evaluation = (
        await session.execute(
            select(Evaluation).where(Evaluation.execution_id == execution_id,
                                     Evaluation.evaluator == "automatic")
        )
    ).scalar_one_or_none()
    if evaluation is None:
        evaluation = Evaluation(execution_id=execution_id, agent_key=execution.agent_key,
                                evaluator="automatic")
        session.add(evaluation)
    evaluation.faithfulness = faithfulness
    evaluation.groundedness = round(groundedness, 4)
    evaluation.hallucination_score = hallucination
    evaluation.citation_score = round(citation_score, 4)
    evaluation.tool_success_rate = round(tool_success_rate, 4)
    evaluation.answer_relevance = answer_relevance
    evaluation.latency_ms = execution.latency_ms
    evaluation.cost_usd = execution.cost_usd
    evaluation.details = {
        "judge_model": judge_model,
        "judge_reasoning": judge_reasoning,
        "citations": len(citations),
        "cited_markers": sorted(cited_markers),
        "tool_calls": len(tool_calls),
        "validation": output.get("validation", []),
        "guardrails": output.get("guardrails", []),
        "llm_judge_available": judge_model is not None,
    }
    await session.flush()
    return {
        "execution_id": execution_id,
        "agent_key": execution.agent_key,
        "faithfulness": evaluation.faithfulness,
        "groundedness": evaluation.groundedness,
        "hallucination_score": evaluation.hallucination_score,
        "citation_score": evaluation.citation_score,
        "tool_success_rate": evaluation.tool_success_rate,
        "answer_relevance": evaluation.answer_relevance,
        "latency_ms": evaluation.latency_ms,
        "cost_usd": evaluation.cost_usd,
        "details": evaluation.details,
    }
