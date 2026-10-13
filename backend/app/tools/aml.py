"""AML / financial crime investigation tools.

Detection implements published typologies (structuring, rapid movement of funds,
high-risk corridors, round-amount layering, dormant reactivation, velocity spikes)
over the real transaction ledger.
"""

from __future__ import annotations

import json
import statistics
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.core.errors import NotFoundError, ValidationError
from app.core.storage import store
from app.db.models.banking import (
    Account,
    AmlAlert,
    AmlCase,
    Customer,
    SarReport,
    Transaction,
)
from app.tools.base import ToolContext, tool

HIGH_RISK_COUNTRIES = {"IRN", "PRK", "SYR", "AFG", "MMR", "YEM", "SSD", "CUB", "VEN", "RUS"}
STRUCTURING_THRESHOLD = 1_000_000.0  # INR reporting threshold
STRUCTURING_BAND = 0.9


async def _resolve_customer_id(ctx: ToolContext, identifier: str) -> str:
    """Accept whatever identifier the caller actually holds and return the internal id.

    Investigators and agents work from customer *numbers* (CUS-100004) -- that is what every
    other tool prints. Matching only on the internal id meant a customer number scanned an
    empty ledger and reported "no suspicious activity", which is the most dangerous possible
    way for this tool to be wrong.
    """
    ident = identifier.strip()
    customer = (
        await ctx.session.execute(
            select(Customer).where(
                or_(
                    Customer.id == ident,
                    Customer.customer_number == ident,
                    func.lower(Customer.email) == ident.lower(),
                )
            )
        )
    ).scalar_one_or_none()
    if customer is None:
        raise NotFoundError(f"Customer '{identifier}' not found")
    return customer.id


class MonitorArgs(BaseModel):
    customer_id: str | None = Field(
        default=None, description="Restrict to one customer: customer number, email or id"
    )
    days: int = Field(default=90, ge=1, le=730)
    min_amount: float = Field(default=0.0)
    persist_alerts: bool = Field(default=True)


@tool(
    "monitor_transactions",
    "Run the transaction monitoring rule set over the ledger and return typology hits.",
    MonitorArgs,
    category="aml",
    writes_data=True,
    timeout_seconds=120,
)
async def monitor_transactions(args: MonitorArgs, ctx: ToolContext) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=args.days)
    stmt = select(Transaction).where(Transaction.booked_at >= since)
    if args.customer_id:
        stmt = stmt.where(Transaction.customer_id == await _resolve_customer_id(ctx, args.customer_id))
    if args.min_amount:
        stmt = stmt.where(func.abs(Transaction.amount) >= args.min_amount)
    txns = (await ctx.session.execute(stmt.order_by(Transaction.booked_at))).scalars().all()
    if not txns:
        return {"transactions_scanned": 0, "alerts": [], "typologies": {}}

    by_customer: dict[str, list[Transaction]] = defaultdict(list)
    for t in txns:
        by_customer[t.customer_id].append(t)

    alerts: list[dict[str, Any]] = []

    for customer_id, items in by_customer.items():
        amounts = [abs(t.amount) for t in items]
        mean = statistics.fmean(amounts)
        stdev = statistics.pstdev(amounts) if len(amounts) > 1 else 0.0

        # R001 structuring: repeated amounts just below the reporting threshold
        near = [
            t
            for t in items
            if STRUCTURING_THRESHOLD * STRUCTURING_BAND <= abs(t.amount) < STRUCTURING_THRESHOLD
        ]
        by_day: dict[str, list[Transaction]] = defaultdict(list)
        for t in near:
            by_day[t.booked_at.date().isoformat()].append(t)
        for day, group in by_day.items():
            if len(group) >= 2:
                alerts.append(
                    _alert(
                        customer_id,
                        "R001",
                        "Potential structuring below reporting threshold",
                        "high",
                        min(95.0, 55.0 + 10 * len(group)),
                        group,
                        {
                            "day": day,
                            "count": len(group),
                            "total": round(sum(abs(t.amount) for t in group), 2),
                            "threshold": STRUCTURING_THRESHOLD,
                        },
                    )
                )

        # R002 rapid movement of funds: large credit followed by debits within 48h
        credits = [t for t in items if t.direction == "credit" and abs(t.amount) >= mean + 2 * stdev]
        for credit in credits:
            window = [
                t
                for t in items
                if t.direction == "debit" and 0 <= (t.booked_at - credit.booked_at).total_seconds() <= 172800
            ]
            outflow = sum(abs(t.amount) for t in window)
            if window and outflow >= abs(credit.amount) * 0.8:
                alerts.append(
                    _alert(
                        customer_id,
                        "R002",
                        "Rapid movement of funds (pass-through)",
                        "high",
                        78.0,
                        [credit, *window],
                        {
                            "credit": round(abs(credit.amount), 2),
                            "outflow_48h": round(outflow, 2),
                            "outflow_ratio": round(outflow / abs(credit.amount), 3),
                        },
                    )
                )

        # R003 high-risk jurisdiction exposure
        cross_border = [t for t in items if t.country in HIGH_RISK_COUNTRIES]
        if cross_border:
            alerts.append(
                _alert(
                    customer_id,
                    "R003",
                    "Transactions with high-risk jurisdictions",
                    "high",
                    70.0,
                    cross_border,
                    {
                        "countries": sorted({t.country for t in cross_border}),
                        "total": round(sum(abs(t.amount) for t in cross_border), 2),
                    },
                )
            )

        # R004 round-amount layering
        round_amounts = [t for t in items if abs(t.amount) >= 100000 and abs(t.amount) % 100000 == 0]
        if len(round_amounts) >= 3:
            alerts.append(
                _alert(
                    customer_id,
                    "R004",
                    "Repeated round-value transfers (layering)",
                    "medium",
                    55.0,
                    round_amounts,
                    {"count": len(round_amounts)},
                )
            )

        # R005 velocity spike vs trailing baseline
        if len(items) >= 10:
            midpoint = len(items) // 2
            first_half = sum(abs(t.amount) for t in items[:midpoint])
            second_half = sum(abs(t.amount) for t in items[midpoint:])
            if first_half > 0 and second_half / first_half >= 4:
                alerts.append(
                    _alert(
                        customer_id,
                        "R005",
                        "Sudden volume spike versus baseline",
                        "medium",
                        50.0,
                        items[midpoint:],
                        {
                            "baseline": round(first_half, 2),
                            "recent": round(second_half, 2),
                            "multiple": round(second_half / first_half, 2),
                        },
                    )
                )

        # R006 dormant account reactivation
        gaps = [(items[i].booked_at - items[i - 1].booked_at).days for i in range(1, len(items))]
        if gaps and max(gaps) >= 120:
            idx = gaps.index(max(gaps)) + 1
            reactivation = items[idx : idx + 5]
            if reactivation and sum(abs(t.amount) for t in reactivation) >= 500000:
                alerts.append(
                    _alert(
                        customer_id,
                        "R006",
                        "Dormant account reactivated with high-value activity",
                        "medium",
                        48.0,
                        reactivation,
                        {"dormant_days": max(gaps)},
                    )
                )

        # R007 counterparty concentration
        counterparties: dict[str, float] = defaultdict(float)
        for t in items:
            if t.counterparty_name:
                counterparties[t.counterparty_name] += abs(t.amount)
        total_volume = sum(counterparties.values())
        for name, value in counterparties.items():
            if total_volume and value / total_volume > 0.7 and value >= 1_000_000:
                alerts.append(
                    _alert(
                        customer_id,
                        "R007",
                        "Single counterparty concentration",
                        "low",
                        35.0,
                        [t for t in items if t.counterparty_name == name],
                        {
                            "counterparty": name,
                            "share": round(value / total_volume, 3),
                            "volume": round(value, 2),
                        },
                    )
                )

    persisted: list[str] = []
    if args.persist_alerts:
        for alert in alerts:
            existing = (
                (
                    await ctx.session.execute(
                        select(AmlAlert).where(
                            AmlAlert.customer_id == alert["customer_id"],
                            AmlAlert.rule_code == alert["rule_code"],
                            AmlAlert.status == "open",
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing:
                existing.evidence = alert["evidence"]
                existing.transaction_ids = alert["transaction_ids"]
                existing.score = alert["score"]
                persisted.append(existing.alert_number)
                continue
            row = AmlAlert(
                alert_number=f"ALT-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
                customer_id=alert["customer_id"],
                rule_code=alert["rule_code"],
                rule_name=alert["rule_name"],
                severity=alert["severity"],
                score=alert["score"],
                transaction_ids=alert["transaction_ids"],
                evidence=alert["evidence"],
                detected_at=datetime.now(UTC),
            )
            ctx.session.add(row)
            persisted.append(row.alert_number)
        await ctx.session.flush()

    typologies: dict[str, int] = defaultdict(int)
    for alert in alerts:
        typologies[alert["rule_name"]] += 1
    return {
        "transactions_scanned": len(txns),
        "window_days": args.days,
        "customers_scanned": len(by_customer),
        "alert_count": len(alerts),
        "alerts": alerts[:40],
        "typologies": dict(typologies),
        "persisted_alert_numbers": persisted[:40],
    }


def _alert(
    customer_id: str,
    rule_code: str,
    rule_name: str,
    severity: str,
    score: float,
    txns: list[Transaction],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "customer_id": customer_id,
        "rule_code": rule_code,
        "rule_name": rule_name,
        "severity": severity,
        "score": score,
        "transaction_ids": [t.id for t in txns][:50],
        "transaction_references": [t.reference for t in txns][:20],
        "evidence": evidence,
    }


class ProfileArgs(BaseModel):
    customer_id: str = Field(description="Customer number, email or id")
    days: int = Field(default=365, ge=30, le=1095)


@tool(
    "profile_customer",
    "Build a behavioural and demographic profile of a customer for investigation.",
    ProfileArgs,
    category="aml",
    timeout_seconds=60,
)
async def profile_customer(args: ProfileArgs, ctx: ToolContext) -> dict[str, Any]:
    customer = (
        await ctx.session.execute(
            select(Customer).where(Customer.id == await _resolve_customer_id(ctx, args.customer_id))
        )
    ).scalar_one()
    since = datetime.now(UTC) - timedelta(days=args.days)
    txns = (
        (
            await ctx.session.execute(
                select(Transaction)
                .where(Transaction.customer_id == customer.id, Transaction.booked_at >= since)
                .order_by(Transaction.booked_at)
            )
        )
        .scalars()
        .all()
    )
    accounts = (
        (await ctx.session.execute(select(Account).where(Account.customer_id == customer.id))).scalars().all()
    )

    credits = [t for t in txns if t.direction == "credit"]
    debits = [t for t in txns if t.direction == "debit"]
    channels: dict[str, int] = defaultdict(int)
    countries: dict[str, float] = defaultdict(float)
    counterparties: dict[str, float] = defaultdict(float)
    monthly: dict[str, float] = defaultdict(float)
    for t in txns:
        channels[t.channel] += 1
        countries[t.country] += abs(t.amount)
        if t.counterparty_name:
            counterparties[t.counterparty_name] += abs(t.amount)
        monthly[t.booked_at.strftime("%Y-%m")] += abs(t.amount)

    amounts = [abs(t.amount) for t in txns] or [0.0]
    return {
        "customer": {
            "customer_id": customer.id,
            "customer_number": customer.customer_number,
            "name": customer.full_name,
            "segment": customer.segment,
            "kyc_status": customer.kyc_status,
            "risk_rating": customer.risk_rating,
            "risk_score": customer.risk_score,
            "pep": customer.pep_flag,
            "sanctions": customer.sanctions_flag,
            "onboarded_at": customer.onboarded_at.isoformat() if customer.onboarded_at else None,
            "country": customer.address_country,
        },
        "accounts": [
            {
                "account_number_masked": f"****{a.account_number[-4:]}",
                "type": a.account_type,
                "balance": round(a.balance, 2),
                "status": a.status,
            }
            for a in accounts
        ],
        "activity": {
            "window_days": args.days,
            "transaction_count": len(txns),
            "total_credits": round(sum(abs(t.amount) for t in credits), 2),
            "total_debits": round(sum(abs(t.amount) for t in debits), 2),
            "net_flow": round(sum(abs(t.amount) for t in credits) - sum(abs(t.amount) for t in debits), 2),
            "average_amount": round(statistics.fmean(amounts), 2),
            "median_amount": round(statistics.median(amounts), 2),
            "max_amount": round(max(amounts), 2),
            "stdev_amount": round(statistics.pstdev(amounts) if len(amounts) > 1 else 0.0, 2),
            "flagged_count": sum(1 for t in txns if t.is_flagged),
        },
        "channels": dict(sorted(channels.items(), key=lambda kv: kv[1], reverse=True)),
        "geography": {
            k: round(v, 2) for k, v in sorted(countries.items(), key=lambda kv: kv[1], reverse=True)[:10]
        },
        "top_counterparties": [
            {"name": k, "volume": round(v, 2)}
            for k, v in sorted(counterparties.items(), key=lambda kv: kv[1], reverse=True)[:10]
        ],
        "monthly_volume": dict(sorted(monthly.items())),
    }


class TimelineArgs(BaseModel):
    customer_id: str = Field(description="Customer number, email or id")
    days: int = Field(default=180, ge=7, le=730)
    include_alerts: bool = True


@tool(
    "build_case_timeline",
    "Assemble a chronological timeline of transactions, alerts and KYC events for a customer.",
    TimelineArgs,
    category="aml",
    timeout_seconds=60,
)
async def build_case_timeline(args: TimelineArgs, ctx: ToolContext) -> dict[str, Any]:
    customer_id = await _resolve_customer_id(ctx, args.customer_id)
    since = datetime.now(UTC) - timedelta(days=args.days)
    txns = (
        (
            await ctx.session.execute(
                select(Transaction)
                .where(Transaction.customer_id == customer_id, Transaction.booked_at >= since)
                .order_by(Transaction.booked_at)
            )
        )
        .scalars()
        .all()
    )
    events: list[dict[str, Any]] = [
        {
            "at": t.booked_at.isoformat(),
            "type": "transaction",
            "summary": f"{t.direction.upper()} {t.currency} {abs(t.amount):,.2f} via {t.channel}"
            f"{f' to {t.counterparty_name}' if t.counterparty_name else ''}",
            "reference": t.reference,
            "amount": round(t.amount, 2),
            "country": t.country,
            "flagged": t.is_flagged,
            "risk_score": t.risk_score,
        }
        for t in txns
    ]
    if args.include_alerts:
        alerts = (
            (
                await ctx.session.execute(
                    select(AmlAlert)
                    .where(AmlAlert.customer_id == customer_id, AmlAlert.detected_at >= since)
                    .order_by(AmlAlert.detected_at)
                )
            )
            .scalars()
            .all()
        )
        events.extend(
            {
                "at": a.detected_at.isoformat(),
                "type": "alert",
                "summary": f"[{a.rule_code}] {a.rule_name} ({a.severity})",
                "reference": a.alert_number,
                "score": a.score,
                "evidence": a.evidence,
            }
            for a in alerts
        )
    events.sort(key=lambda e: e["at"])
    return {
        "customer_id": customer_id,
        "window_days": args.days,
        "event_count": len(events),
        "timeline": events[:300],
    }


class CaseArgs(BaseModel):
    customer_id: str = Field(description="Customer number, email or id")
    title: str
    alert_numbers: list[str] = Field(default_factory=list)
    priority: str = Field(default="medium")
    typologies: list[str] = Field(default_factory=list)


@tool(
    "create_investigation_case",
    "Open an AML investigation case linking the supplied alerts.",
    CaseArgs,
    category="aml",
    writes_data=True,
    idempotent=False,
)
async def create_investigation_case(args: CaseArgs, ctx: ToolContext) -> dict[str, Any]:
    alerts = []
    if args.alert_numbers:
        alerts = (
            (await ctx.session.execute(select(AmlAlert).where(AmlAlert.alert_number.in_(args.alert_numbers))))
            .scalars()
            .all()
        )
    risk_score = max((a.score for a in alerts), default=0.0)
    case = AmlCase(
        case_number=f"AML-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        customer_id=await _resolve_customer_id(ctx, args.customer_id),
        title=args.title[:300],
        priority=args.priority,
        risk_score=risk_score,
        typologies=args.typologies or sorted({a.rule_name for a in alerts}),
        alert_ids=[a.id for a in alerts],
        investigator=ctx.user_email or "ai_agent",
        execution_id=ctx.execution_id,
        timeline=[
            {"at": datetime.now(UTC).isoformat(), "event": "case_opened", "by": ctx.user_email or "ai_agent"}
        ],
    )
    ctx.session.add(case)
    for alert in alerts:
        alert.status = "under_investigation"
        alert.case_id = case.id
    await ctx.session.flush()
    return {
        "case_number": case.case_number,
        "case_id": case.id,
        "linked_alerts": len(alerts),
        "risk_score": risk_score,
        "typologies": case.typologies,
    }


class EvidenceArgs(BaseModel):
    case_number: str
    description: str
    evidence_type: str = Field(default="analysis")
    payload: dict[str, Any] = Field(default_factory=dict)


@tool(
    "attach_case_evidence",
    "Attach an evidence item to an AML case and append it to the case timeline.",
    EvidenceArgs,
    category="aml",
    writes_data=True,
    idempotent=False,
)
async def attach_case_evidence(args: EvidenceArgs, ctx: ToolContext) -> dict[str, Any]:
    case = (
        await ctx.session.execute(select(AmlCase).where(AmlCase.case_number == args.case_number))
    ).scalar_one_or_none()
    if case is None:
        raise NotFoundError(f"AML case '{args.case_number}' not found")
    item = {
        "id": uuid.uuid4().hex[:12],
        "at": datetime.now(UTC).isoformat(),
        "type": args.evidence_type,
        "description": args.description,
        "payload": args.payload,
        "collected_by": ctx.user_email or "ai_agent",
    }
    case.evidence = [*(case.evidence or []), item]
    case.timeline = [
        *(case.timeline or []),
        {"at": item["at"], "event": "evidence_added", "detail": args.description[:200]},
    ]
    await ctx.session.flush()
    return {"case_number": case.case_number, "evidence_id": item["id"], "evidence_count": len(case.evidence)}


class SarArgs(BaseModel):
    case_number: str
    narrative: str = Field(description="Regulator-facing narrative covering who/what/when/why")
    suspicious_amount: float
    typologies: list[str] = Field(default_factory=list)
    activity_start: str | None = None
    activity_end: str | None = None
    regulator: str = Field(default="FIU-IND")


@tool(
    "generate_sar",
    "Draft and store a Suspicious Activity Report for the case. Requires human approval.",
    SarArgs,
    category="aml",
    writes_data=True,
    idempotent=False,
    requires_approval=True,
    approval_risk="critical",
    timeout_seconds=90,
)
async def generate_sar(args: SarArgs, ctx: ToolContext) -> dict[str, Any]:
    case = (
        await ctx.session.execute(select(AmlCase).where(AmlCase.case_number == args.case_number))
    ).scalar_one_or_none()
    if case is None:
        raise NotFoundError(f"AML case '{args.case_number}' not found")
    if len(args.narrative) < 200:
        raise ValidationError(
            "SAR narrative must be at least 200 characters and cover the "
            "five Ws (who, what, when, where, why)"
        )

    customer = (
        await ctx.session.execute(select(Customer).where(Customer.id == case.customer_id))
    ).scalar_one_or_none()
    alerts = (await ctx.session.execute(select(AmlAlert).where(AmlAlert.case_id == case.id))).scalars().all()
    txn_ids = [tid for a in alerts for tid in (a.transaction_ids or [])]
    txns = []
    if txn_ids:
        txns = (
            (await ctx.session.execute(select(Transaction).where(Transaction.id.in_(txn_ids))))
            .scalars()
            .all()
        )

    sar = SarReport(
        sar_number=f"SAR-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        case_id=case.id,
        customer_id=case.customer_id,
        regulator=args.regulator,
        suspicious_amount=args.suspicious_amount,
        typologies=args.typologies or case.typologies,
        narrative=args.narrative,
        activity_start=datetime.fromisoformat(args.activity_start).date() if args.activity_start else None,
        activity_end=datetime.fromisoformat(args.activity_end).date() if args.activity_end else None,
        supporting_transactions=[
            {
                "reference": t.reference,
                "booked_at": t.booked_at.isoformat(),
                "amount": round(t.amount, 2),
                "currency": t.currency,
                "direction": t.direction,
                "counterparty": t.counterparty_name,
                "country": t.country,
                "channel": t.channel,
            }
            for t in txns[:200]
        ],
        status="pending_approval",
        execution_id=ctx.execution_id,
    )
    ctx.session.add(sar)
    await ctx.session.flush()

    document = {
        "sar_number": sar.sar_number,
        "filing_institution": sar.filing_institution,
        "regulator": sar.regulator,
        "generated_at": datetime.now(UTC).isoformat(),
        "subject": {
            "name": customer.full_name if customer else None,
            "customer_number": customer.customer_number if customer else None,
            "date_of_birth": customer.date_of_birth.isoformat()
            if customer and customer.date_of_birth
            else None,
            "address": {
                "line1": customer.address_line1,
                "city": customer.address_city,
                "state": customer.address_state,
                "postcode": customer.address_postcode,
                "country": customer.address_country,
            }
            if customer
            else {},
            "risk_rating": customer.risk_rating if customer else None,
        },
        "case": {
            "case_number": case.case_number,
            "typologies": sar.typologies,
            "risk_score": case.risk_score,
        },
        "suspicious_amount": sar.suspicious_amount,
        "currency": sar.currency,
        "activity_period": {"start": args.activity_start, "end": args.activity_end},
        "narrative": args.narrative,
        "supporting_transactions": sar.supporting_transactions,
        "alerts": [
            {
                "alert_number": a.alert_number,
                "rule": a.rule_name,
                "severity": a.severity,
                "evidence": a.evidence,
            }
            for a in alerts
        ],
        "prepared_by": ctx.user_email or "ai_agent",
        "execution_id": ctx.execution_id,
    }
    stored = await store.put(
        f"sar/{case.case_number}/{sar.sar_number}.json",
        json.dumps(document, indent=2, default=str).encode(),
        "application/json",
    )
    sar.artifact_uri = stored["uri"]
    case.sar_filed = True
    case.narrative = args.narrative
    case.timeline = [
        *(case.timeline or []),
        {"at": datetime.now(UTC).isoformat(), "event": "sar_drafted", "detail": sar.sar_number},
    ]
    await ctx.session.flush()
    return {
        "sar_number": sar.sar_number,
        "status": sar.status,
        "artifact": stored,
        "supporting_transaction_count": len(sar.supporting_transactions),
        "regulator": sar.regulator,
    }


class CloseCaseArgs(BaseModel):
    case_number: str
    disposition: str = Field(description="sar_filed|no_further_action|escalated|false_positive")
    rationale: str


@tool(
    "close_investigation_case",
    "Close an AML case with a disposition and rationale. Requires human approval.",
    CloseCaseArgs,
    category="aml",
    writes_data=True,
    idempotent=False,
    requires_approval=True,
    approval_risk="high",
)
async def close_investigation_case(args: CloseCaseArgs, ctx: ToolContext) -> dict[str, Any]:
    case = (
        await ctx.session.execute(select(AmlCase).where(AmlCase.case_number == args.case_number))
    ).scalar_one_or_none()
    if case is None:
        raise NotFoundError(f"AML case '{args.case_number}' not found")
    case.status = "closed"
    case.disposition = args.disposition
    case.closed_at = datetime.now(UTC)
    case.timeline = [
        *(case.timeline or []),
        {
            "at": case.closed_at.isoformat(),
            "event": "case_closed",
            "detail": f"{args.disposition}: {args.rationale[:300]}",
        },
    ]
    alerts = (await ctx.session.execute(select(AmlAlert).where(AmlAlert.case_id == case.id))).scalars().all()
    for alert in alerts:
        alert.status = "closed"
    await ctx.session.flush()
    return {
        "case_number": case.case_number,
        "status": case.status,
        "disposition": args.disposition,
        "alerts_closed": len(alerts),
    }
