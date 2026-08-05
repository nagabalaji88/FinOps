"""Operational CLI: `python -m app.cli <command>`."""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import pathlib
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app.core.logging import configure_logging, get_logger
from app.db.session import session_scope
from app.services.bootstrap import bootstrap, ensure_schema, seed_sample_banking

log = get_logger("cli")

OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.csv"
OFAC_ALT_URL = "https://www.treasury.gov/ofac/downloads/alt.csv"


async def cmd_init_db() -> None:
    await ensure_schema()
    async with session_scope() as session:
        result = await bootstrap(session)
    print(json.dumps(result, indent=2))


async def cmd_seed_banking(customers: int) -> None:
    await ensure_schema()
    async with session_scope() as session:
        result = await seed_sample_banking(session, customers=customers)
    print(json.dumps(result, indent=2))


async def cmd_load_sanctions(limit: int) -> None:
    """Download and load the OFAC SDN list (public data, no credentials required)."""
    from app.db.models.banking import SanctionsEntry
    from app.llm.base import http_client
    from app.tools.kyc import normalise_name

    client = http_client()
    print(f"Downloading {OFAC_SDN_URL} ...")
    sdn = await client.get(OFAC_SDN_URL, timeout=120.0)
    sdn.raise_for_status()
    alt = await client.get(OFAC_ALT_URL, timeout=120.0)

    aliases: dict[str, list[str]] = {}
    if alt.status_code < 400:
        for row in csv.reader(io.StringIO(alt.text)):
            if len(row) >= 4:
                aliases.setdefault(row[0], []).append(row[3].strip())

    inserted = 0
    async with session_scope() as session:
        await session.execute(delete(SanctionsEntry).where(SanctionsEntry.list_name == "OFAC_SDN"))
        for row in csv.reader(io.StringIO(sdn.text)):
            if len(row) < 12 or row[0] == "-0- ":
                continue
            uid, name, sdn_type, program = row[0], row[1].strip(), row[2].strip(), row[3].strip()
            if not name or name == "-0-":
                continue
            remarks = row[11].strip() if len(row) > 11 else ""
            dob = None
            if "DOB " in remarks:
                dob = remarks.split("DOB ")[1].split(";")[0].strip()
            session.add(SanctionsEntry(
                list_name="OFAC_SDN",
                entry_type="entity" if sdn_type.lower() != "individual" else "individual",
                full_name=name, normalised_name=normalise_name(name),
                aliases=aliases.get(uid, [])[:20], date_of_birth=dob, program=program,
                source_url="https://sanctionslist.ofac.treas.gov/Home/SdnList",
                remarks=remarks[:2000] or None,
            ))
            inserted += 1
            if limit and inserted >= limit:
                break
        await session.commit()
    print(json.dumps({"list": "OFAC_SDN", "entries_loaded": inserted}, indent=2))


async def cmd_reindex_knowledge() -> None:
    from app.db.models.knowledge import KnowledgeSource
    from app.rag.embeddings import active_dimensions
    from app.rag.pipeline import pipeline
    from app.rag.vectorstore import init_vector_store

    await init_vector_store(active_dimensions())
    async with session_scope() as session:
        sources = (await session.execute(select(KnowledgeSource))).scalars().all()
        results = [await pipeline.reindex_source(session, s) for s in sources]
        await session.commit()
    print(json.dumps(results, indent=2))


async def cmd_sync_knowledge(source_key: str | None) -> None:
    from app.db.models.knowledge import KnowledgeSource
    from app.rag.connectors import CONNECTORS

    async with session_scope() as session:
        stmt = select(KnowledgeSource).where(KnowledgeSource.enabled.is_(True))
        if source_key:
            stmt = stmt.where(KnowledgeSource.key == source_key)
        sources = (await session.execute(stmt)).scalars().all()
        results = []
        for source in sources:
            connector = CONNECTORS.get(source.connector)
            if connector is None:
                continue
            results.append(await connector.sync(session, source))
        await session.commit()
    print(json.dumps(results, indent=2))


async def cmd_retention(dry_run: bool) -> None:
    """Apply the data retention policy to traces, events, logs and cost records."""
    from app.core.config import settings
    from app.db.models.agents import CostRecord, ExecutionEvent, LogRecord, Span

    cutoff = datetime.now(UTC) - timedelta(days=settings.data_retention_days)
    summary = {}
    async with session_scope() as session:
        for label, model, column in [
            ("log_records", LogRecord, LogRecord.timestamp),
            ("execution_events", ExecutionEvent, ExecutionEvent.timestamp),
            ("spans", Span, Span.start_time),
            ("cost_records", CostRecord, CostRecord.timestamp),
        ]:
            count = int(
                (await session.execute(
                    select(func.count()).select_from(model).where(column < cutoff)
                )).scalar_one()
            )
            summary[label] = count
            if not dry_run and count:
                await session.execute(delete(model).where(column < cutoff))
        if not dry_run:
            await session.commit()
    print(json.dumps({"cutoff": cutoff.isoformat(), "dry_run": dry_run,
                      "records": summary}, indent=2))


async def cmd_validate(
    agent: str | None,
    scenario_ids: list[str] | None,
    tag: str | None,
    output: str | None,
    fmt: str,
    concurrency: int,
    reviewer: str,
    fail_on_blocked: bool,
    setup: bool = True,
    show_output: bool = True,
) -> int:
    """Run every scenario input through its agent, in order, and report the results.

    Self-contained by default: it prepares the database, seeds the platform and loads the
    sample banking dataset if they are missing, then executes the inputs one at a time and
    prints each agent's result as it arrives.
    """
    from app.rag.embeddings import active_dimensions
    from app.rag.vectorstore import init_vector_store
    from app.services import telemetry
    from app.tools import registry  # noqa: F401 - registers tools
    from app.validation import SCENARIOS, ValidationRunner, to_console, to_json, to_markdown
    from app.validation.report import format_live
    from app.validation.runner import prepare_environment

    selected = list(SCENARIOS)
    if agent:
        selected = [s for s in selected if s.agent_key == agent]
    if scenario_ids:
        wanted = {s.strip().upper() for s in scenario_ids}
        selected = [s for s in selected if s.id.upper() in wanted]
    if tag:
        selected = [s for s in selected if tag in s.tags]
    if not selected:
        print("No scenarios matched the filters.")
        return 2

    if setup:
        print("Preparing the environment ...")
        prepared = await prepare_environment(selected)
        for step in prepared["steps"]:
            detail = {k: v for k, v in step.items() if k not in {"step", "action"}}
            print(f"  {step['step']:<16} {step['action']}"
                  + (f"  {json.dumps(detail)}" if detail else ""))
        print()

    await init_vector_store(active_dimensions())
    telemetry.install()

    validation_runner = ValidationRunner(reviewer_email=reviewer, concurrency=concurrency)
    preflight = await validation_runner.preflight(selected)
    for warning in preflight.get("warnings", []):
        print(f"  ! {warning}")
    if preflight.get("warnings"):
        print()

    providers = preflight.get("configured_providers") or ["none"]
    order = "sequentially" if concurrency == 1 else f"{concurrency} at a time"
    print(f"Running {len(selected)} input(s) {order} across "
          f"{len({s.agent_key for s in selected})} agent(s) · providers: {', '.join(providers)}")

    position = {"n": 0}

    def announce(result) -> None:
        position["n"] += 1
        print(format_live(position["n"], len(selected), result, show_output=show_output),
              flush=True)

    report = await validation_runner.run(selected, on_result=announce)
    print("\n" + "=" * 96)
    print(to_console(report))

    if output:
        rendered = to_json(report) if fmt == "json" else to_markdown(report)
        path = pathlib.Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        print(f"\n  Report written to {path}")

    if not report.ok:
        return 1
    if fail_on_blocked and report.blocked:
        print(f"\n  {report.blocked} scenario(s) could not be evaluated "
              f"and --fail-on-blocked is set.")
        return 3
    return 0


async def cmd_health() -> None:
    from app.core.cache import cache
    from app.db.session import ping_database
    from app.llm.router import router as model_router

    await cache.connect()
    health = {
        "database": await ping_database(),
        "cache_backend": cache.backend,
        "configured_llm_providers": model_router.configured_providers(),
        "provider_health": await model_router.health(),
    }
    print(json.dumps(health, indent=2, default=str))


async def cmd_run_agent(agent_key: str, payload: str, user_email: str) -> None:
    from app.db.models.identity import User
    from app.engine.executor import engine as execution_engine
    from app.rag.embeddings import active_dimensions
    from app.rag.vectorstore import init_vector_store
    from app.services import telemetry
    from app.tools import registry  # noqa: F401

    await init_vector_store(active_dimensions())
    telemetry.install()
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.email == user_email))
        ).scalar_one_or_none()
        execution = await execution_engine.submit(
            session, agent_key=agent_key, payload=json.loads(payload), user=user,
            trigger="cli", wait=True,
        )
        await session.refresh(execution)
        print(json.dumps({
            "execution_id": execution.id, "status": execution.status,
            "latency_ms": execution.latency_ms, "cost_usd": execution.cost_usd,
            "tokens": execution.tokens_input + execution.tokens_output,
            "error": execution.error, "response": execution.final_response,
        }, indent=2))


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="finops", description="FinOps AI Command Center CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create schema and seed platform data")
    seed = sub.add_parser("seed-banking", help="Load the sample banking environment")
    seed.add_argument("--customers", type=int, default=24)
    sanctions = sub.add_parser("load-sanctions", help="Download and load the OFAC SDN list")
    sanctions.add_argument("--limit", type=int, default=0, help="0 loads the whole list")
    sub.add_parser("reindex-knowledge", help="Rebuild embeddings for all knowledge sources")
    sync = sub.add_parser("sync-knowledge", help="Pull documents from configured connectors")
    sync.add_argument("--source", default=None)
    retention = sub.add_parser("retention", help="Apply the data retention policy")
    retention.add_argument("--apply", action="store_true", help="Delete instead of reporting")
    sub.add_parser("health", help="Report platform and provider health")
    validate = sub.add_parser("validate",
                              help="Run the conformance suite against the implemented agents")
    validate.add_argument("--agent", default=None, help="Restrict to one agent key")
    validate.add_argument("--scenario", action="append", default=None,
                          help="Run specific scenario ids (repeatable)")
    validate.add_argument("--tag", default=None, help="Restrict to scenarios carrying a tag")
    validate.add_argument("--output", default=None, help="Write the report to this path")
    validate.add_argument("--format", dest="fmt", default="markdown",
                          choices=["markdown", "json"])
    validate.add_argument("--concurrency", type=int, default=1)
    validate.add_argument("--reviewer", default="approver@finops.local",
                          help="Identity used to decide approval gates")
    validate.add_argument("--fail-on-blocked", action="store_true",
                          help="Exit non-zero when a scenario cannot be evaluated "
                               "(for example because no LLM provider is configured)")
    validate.add_argument("--no-setup", dest="setup", action="store_false",
                          help="Skip environment preparation and assume the database is "
                               "already migrated and seeded")
    validate.add_argument("--quiet", dest="show_output", action="store_false",
                          help="Print verdicts only, without each agent's response")
    run = sub.add_parser("run-agent", help="Execute an agent from the command line")
    run.add_argument("agent_key")
    run.add_argument("--input", default="{}")
    run.add_argument("--user", default="admin@finops.local")

    args = parser.parse_args()
    commands = {
        "init-db": lambda: cmd_init_db(),
        "seed-banking": lambda: cmd_seed_banking(args.customers),
        "load-sanctions": lambda: cmd_load_sanctions(args.limit),
        "reindex-knowledge": lambda: cmd_reindex_knowledge(),
        "sync-knowledge": lambda: cmd_sync_knowledge(args.source),
        "retention": lambda: cmd_retention(not args.apply),
        "health": lambda: cmd_health(),
        "run-agent": lambda: cmd_run_agent(args.agent_key, args.input, args.user),
        "validate": lambda: cmd_validate(args.agent, args.scenario, args.tag, args.output,
                                         args.fmt, args.concurrency, args.reviewer,
                                         args.fail_on_blocked, args.setup, args.show_output),
    }
    try:
        exit_code = asyncio.run(commands[args.command]())
    except KeyboardInterrupt:
        sys.exit(130)
    if isinstance(exit_code, int) and exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
