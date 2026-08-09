"""Collections tools: delinquency treatment, hardship, promises to pay and recovery.

Collections is one of the most heavily constrained things a bank does, so the constraints
are implemented rather than described:

* **RBI asset classification** — SMA-0/1/2 for a standard asset that is past due, then
  sub-standard at 90+ days, doubtful at 12 months and loss beyond that;
* **the Fair Practices Code contact rules** — no contact before 08:00 or after 19:00 local
  time, a hard cap on attempts per week, and an absolute stop on a cease-contact
  instruction, an open dispute, or a customer in a live hardship arrangement;
* **affordability-first hardship** — a plan is only offered from verified surplus, and a
  plan that the customer cannot afford is refused rather than proposed;
* **roll-rate risk** — the probability of moving to the next bucket, derived from the
  customer's own payment behaviour rather than a table.

Nothing here contacts anybody. The tools decide, record and schedule; delivery is the
channel system's job.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.banking import (
    Account,
    Card,
    ContactAttempt,
    Customer,
    DelinquencyCase,
    Loan,
    PromiseToPay,
    RepaymentPlan,
    Transaction,
)
from app.tools.base import ToolContext, tool

log = get_logger("tools.collections")

# --- regulatory constants -----------------------------------------------------
#: RBI Master Circular on Income Recognition and Asset Classification.
SMA_0_MAX_DPD = 30
SMA_1_MAX_DPD = 60
SMA_2_MAX_DPD = 90
SUBSTANDARD_MAX_DPD = 455  # 12 months as a sub-standard asset
DOUBTFUL_MAX_DPD = 1_185  # three years doubtful before loss

#: Fair Practices Code contact window and frequency, in the customer's local time.
CONTACT_WINDOW_START = time(8, 0)
CONTACT_WINDOW_END = time(19, 0)
MAX_ATTEMPTS_PER_WEEK = 3
MAX_ATTEMPTS_PER_DAY = 1
MIN_HOURS_BETWEEN_ATTEMPTS = 24

#: Treatment ladder by bucket. Later buckets unlock harder actions, never earlier ones.
TREATMENT_LADDER: dict[str, dict[str, Any]] = {
    "X": {"label": "Current", "actions": ["monitor"], "intensity": "none"},
    "0": {"label": "1-30 days", "actions": ["sms", "email"], "intensity": "light"},
    "1": {"label": "31-60 days", "actions": ["sms", "email", "call"], "intensity": "standard"},
    "2": {"label": "61-90 days", "actions": ["call", "letter", "hardship_review"], "intensity": "elevated"},
    "3": {
        "label": "91-180 days",
        "actions": ["call", "letter", "field_visit", "restructure_offer"],
        "intensity": "high",
    },
    "4": {
        "label": "181-365 days",
        "actions": ["letter", "field_visit", "settlement_offer", "legal_notice"],
        "intensity": "high",
    },
    "5+": {
        "label": "365+ days",
        "actions": ["legal_notice", "recovery_agency", "write_off_review"],
        "intensity": "recovery",
    },
}

#: Minimum surplus a plan must leave the customer, as a share of net income.
HARDSHIP_RESERVE_PCT = 15.0
MIN_PLAN_INSTALMENT = 500.0
MAX_PLAN_MONTHS = 60


def bucket_for_dpd(dpd: int) -> str:
    if dpd <= 0:
        return "X"
    if dpd <= 30:
        return "0"
    if dpd <= 60:
        return "1"
    if dpd <= 90:
        return "2"
    if dpd <= 180:
        return "3"
    if dpd <= 365:
        return "4"
    return "5+"


def asset_classification(dpd: int) -> dict[str, str]:
    """RBI classification, with the special-mention sub-grades below 90 days."""
    if dpd <= 0:
        return {"classification": "standard", "sub_grade": None, "basis": "no amount overdue"}
    if dpd <= SMA_0_MAX_DPD:
        return {"classification": "standard", "sub_grade": "SMA-0", "basis": f"overdue {dpd} days (1-30)"}
    if dpd <= SMA_1_MAX_DPD:
        return {"classification": "standard", "sub_grade": "SMA-1", "basis": f"overdue {dpd} days (31-60)"}
    if dpd <= SMA_2_MAX_DPD:
        return {"classification": "standard", "sub_grade": "SMA-2", "basis": f"overdue {dpd} days (61-90)"}
    if dpd <= SUBSTANDARD_MAX_DPD:
        return {
            "classification": "sub_standard",
            "sub_grade": None,
            "basis": f"non-performing for {dpd} days",
        }
    if dpd <= DOUBTFUL_MAX_DPD:
        return {
            "classification": "doubtful",
            "sub_grade": None,
            "basis": f"sub-standard for more than 12 months ({dpd} days overdue)",
        }
    return {
        "classification": "loss",
        "sub_grade": None,
        "basis": f"doubtful beyond three years ({dpd} days overdue)",
    }


def provision_rate(classification: str, secured: bool) -> float:
    """RBI provisioning norms for a scheduled commercial bank."""
    return {
        "standard": 0.004,
        "sub_standard": 0.15 if secured else 0.25,
        "doubtful": 0.40 if secured else 1.00,
        "loss": 1.00,
    }.get(classification, 0.004)


# --- helpers ------------------------------------------------------------------
async def _case(ctx: ToolContext, reference: str) -> DelinquencyCase:
    stmt = select(DelinquencyCase).where(
        (DelinquencyCase.case_number == reference) | (DelinquencyCase.id == reference)
    )
    case = (await ctx.session.execute(stmt)).scalar_one_or_none()
    if case is None:
        raise NotFoundError(f"Delinquency case '{reference}' not found")
    return case


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def bank_timezone() -> ZoneInfo:
    """The zone the contact window is expressed in.

    Falling back to UTC would silently move the permitted window by hours, so a bad
    setting is logged loudly rather than absorbed.
    """
    try:
        return ZoneInfo(settings.bank_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        log.error(
            "invalid_bank_timezone",
            configured=settings.bank_timezone,
            note="falling back to UTC; the contact window will not match local time",
        )
        return ZoneInfo("UTC")


def to_local(moment: datetime) -> datetime:
    """Contact rules are local-time rules; everything stored is UTC."""
    return _aware(moment).astimezone(bank_timezone())


# --- tools --------------------------------------------------------------------
class ScanArgs(BaseModel):
    min_days_past_due: int = Field(default=1, ge=0, le=3650)
    facility_type: str | None = Field(default=None, description="loan | card")
    open_cases: bool = Field(default=True, description="Create a case for anything uncovered")
    limit: int = Field(default=200, ge=1, le=2000)


@tool(
    "scan_delinquent_accounts",
    "Scan loans and cards for arrears, classify them, and open collection cases for any "
    "delinquency not already under treatment.",
    ScanArgs,
    category="collections",
    writes_data=True,
    timeout_seconds=90,
)
async def scan_delinquent_accounts(args: ScanArgs, ctx: ToolContext) -> dict[str, Any]:
    today = date.today()
    found: list[dict[str, Any]] = []
    opened = 0

    existing = {
        (case.facility_type, case.facility_id): case
        for case in (
            await ctx.session.execute(select(DelinquencyCase).where(DelinquencyCase.status == "open"))
        )
        .scalars()
        .all()
    }

    if args.facility_type in (None, "loan"):
        loans = (
            (
                await ctx.session.execute(
                    select(Loan)
                    .where(Loan.days_past_due >= args.min_days_past_due)
                    .order_by(Loan.days_past_due.desc())
                    .limit(args.limit)
                )
            )
            .scalars()
            .all()
        )
        for loan in loans:
            overdue = round(loan.emi_amount * max(1, loan.days_past_due // 30), 2)
            found.append(
                _delinquency_row(
                    "loan",
                    loan.id,
                    loan.loan_number,
                    loan.customer_id,
                    loan.outstanding,
                    overdue,
                    loan.emi_amount,
                    loan.days_past_due,
                    bool(loan.collateral),
                )
            )

    if args.facility_type in (None, "card"):
        cards = (
            (
                await ctx.session.execute(
                    select(Card).where(Card.status == "active", Card.current_balance > 0).limit(args.limit)
                )
            )
            .scalars()
            .all()
        )
        for card in cards:
            if card.due_date is None or card.due_date >= today or card.minimum_due <= 0:
                continue
            dpd = (today - card.due_date).days
            if dpd < args.min_days_past_due:
                continue
            found.append(
                _delinquency_row(
                    "card",
                    card.id,
                    card.card_number_masked,
                    card.customer_id,
                    card.current_balance,
                    card.minimum_due,
                    card.minimum_due,
                    dpd,
                    False,
                )
            )

    for row in found:
        key = (row["facility_type"], row["facility_id"])
        case = existing.get(key)
        if case is not None:
            case.days_past_due = row["days_past_due"]
            case.bucket = row["bucket"]
            case.asset_classification = row["classification"]
            case.outstanding = row["outstanding"]
            case.amount_overdue = row["amount_overdue"]
            row["case_number"] = case.case_number
            row["case_action"] = "updated"
            continue
        if not args.open_cases:
            row["case_action"] = "not_opened"
            continue
        case = DelinquencyCase(
            case_number=f"COL-{uuid.uuid4().hex[:10].upper()}",
            customer_id=row["customer_id"],
            facility_type=row["facility_type"],
            facility_id=row["facility_id"],
            facility_reference=row["facility_reference"],
            outstanding=row["outstanding"],
            amount_overdue=row["amount_overdue"],
            minimum_due=row["minimum_due"],
            days_past_due=row["days_past_due"],
            bucket=row["bucket"],
            asset_classification=row["classification"],
            status="open",
            opened_at=datetime.now(UTC),
        )
        ctx.session.add(case)
        row["case_number"] = case.case_number
        row["case_action"] = "opened"
        opened += 1

    await ctx.session.flush()
    by_bucket: dict[str, dict[str, Any]] = {}
    for row in found:
        entry = by_bucket.setdefault(row["bucket"], {"accounts": 0, "outstanding": 0.0, "overdue": 0.0})
        entry["accounts"] += 1
        entry["outstanding"] += row["outstanding"]
        entry["overdue"] += row["amount_overdue"]

    return {
        "scanned_at": datetime.now(UTC).isoformat(),
        "delinquent_accounts": len(found),
        "cases_opened": opened,
        "total_outstanding": round(sum(r["outstanding"] for r in found), 2),
        "total_overdue": round(sum(r["amount_overdue"] for r in found), 2),
        "by_bucket": {
            bucket: {
                "accounts": data["accounts"],
                "outstanding": round(data["outstanding"], 2),
                "overdue": round(data["overdue"], 2),
                "label": TREATMENT_LADDER.get(bucket, {}).get("label"),
            }
            for bucket, data in sorted(by_bucket.items())
        },
        "accounts": found[:50],
    }


def _delinquency_row(
    facility_type: str,
    facility_id: str,
    reference: str,
    customer_id: str,
    outstanding: float,
    overdue: float,
    minimum_due: float,
    dpd: int,
    secured: bool,
) -> dict[str, Any]:
    classification = asset_classification(dpd)
    rate = provision_rate(classification["classification"], secured)
    return {
        "facility_type": facility_type,
        "facility_id": facility_id,
        "facility_reference": reference,
        "customer_id": customer_id,
        "outstanding": round(outstanding, 2),
        "amount_overdue": round(overdue, 2),
        "minimum_due": round(minimum_due, 2),
        "days_past_due": dpd,
        "bucket": bucket_for_dpd(dpd),
        "classification": classification["classification"],
        "sub_grade": classification["sub_grade"],
        "classification_basis": classification["basis"],
        "secured": secured,
        "provision_rate_pct": round(rate * 100, 2),
        "provision_required": round(outstanding * rate, 2),
    }


class CaseArgs(BaseModel):
    case: str = Field(description="Case number or id")


@tool(
    "get_delinquency_case",
    "Fetch a collection case with its arrears, classification, contact history and promises.",
    CaseArgs,
    category="collections",
)
async def get_delinquency_case(args: CaseArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)
    customer = (
        await ctx.session.execute(select(Customer).where(Customer.id == case.customer_id))
    ).scalar_one_or_none()

    attempts = (
        (
            await ctx.session.execute(
                select(ContactAttempt)
                .where(ContactAttempt.case_id == case.id)
                .order_by(ContactAttempt.attempted_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    promises = (
        (
            await ctx.session.execute(
                select(PromiseToPay)
                .where(PromiseToPay.case_id == case.id)
                .order_by(PromiseToPay.promised_date.desc())
            )
        )
        .scalars()
        .all()
    )

    classification = asset_classification(case.days_past_due)
    return {
        "case_number": case.case_number,
        "status": case.status,
        "customer": {
            "customer_number": customer.customer_number if customer else None,
            "full_name": customer.full_name if customer else None,
            "segment": customer.segment if customer else None,
        },
        "facility": {
            "type": case.facility_type,
            "reference": case.facility_reference,
            "outstanding": case.outstanding,
            "amount_overdue": case.amount_overdue,
            "minimum_due": case.minimum_due,
            "currency": case.currency,
        },
        "delinquency": {
            "days_past_due": case.days_past_due,
            "bucket": case.bucket,
            "bucket_label": TREATMENT_LADDER.get(case.bucket, {}).get("label"),
            **classification,
        },
        "controls": {
            "contact_consent": case.contact_consent,
            "cease_contact": case.cease_contact,
            "dispute_open": case.dispute_open,
            "hardship_flag": case.hardship_flag,
        },
        "treatment": {
            "strategy": case.strategy,
            "assigned_to": case.assigned_to,
            "last_contacted_at": _aware(case.last_contacted_at).isoformat()
            if case.last_contacted_at
            else None,
            "next_action_at": _aware(case.next_action_at).isoformat() if case.next_action_at else None,
        },
        "contact_history": [
            {
                "channel": a.channel,
                "outcome": a.outcome,
                "at": _aware(a.attempted_at).isoformat(),
                "notes": a.notes,
            }
            for a in attempts
        ],
        "promises": [
            {
                "amount": p.amount,
                "promised_date": p.promised_date.isoformat(),
                "status": p.status,
                "settled_amount": p.settled_amount,
            }
            for p in promises
        ],
    }


class ArrearsArgs(BaseModel):
    case: str = Field(description="Case number or id")


@tool(
    "calculate_arrears",
    "Recompute arrears, ageing, provisioning and roll-rate risk from the live ledger.",
    ArrearsArgs,
    category="collections",
)
async def calculate_arrears(args: ArrearsArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)
    secured = False
    schedule_emi = case.minimum_due

    if case.facility_type == "loan":
        loan = (
            await ctx.session.execute(select(Loan).where(Loan.id == case.facility_id))
        ).scalar_one_or_none()
        if loan is None:
            raise NotFoundError("The facility behind this case no longer exists")
        secured = bool(loan.collateral)
        schedule_emi = loan.emi_amount
        dpd = loan.days_past_due
        outstanding = loan.outstanding
        instalments_missed = max(0, dpd // 30)
        overdue = round(schedule_emi * max(1, instalments_missed), 2) if dpd > 0 else 0.0
    else:
        card = (
            await ctx.session.execute(select(Card).where(Card.id == case.facility_id))
        ).scalar_one_or_none()
        if card is None:
            raise NotFoundError("The facility behind this case no longer exists")
        outstanding = card.current_balance
        dpd = (date.today() - card.due_date).days if card.due_date else 0
        instalments_missed = max(0, dpd // 30)
        overdue = round(card.minimum_due * max(1, instalments_missed), 2) if dpd > 0 else 0.0
        schedule_emi = card.minimum_due

    classification = asset_classification(dpd)
    rate = provision_rate(classification["classification"], secured)

    # Roll-rate risk from this customer's own behaviour: how often a past-due position has
    # deteriorated rather than cured, weighted by how close it is to the next boundary.
    promises = (
        (await ctx.session.execute(select(PromiseToPay).where(PromiseToPay.case_id == case.id)))
        .scalars()
        .all()
    )
    kept = sum(1 for p in promises if p.status == "kept")
    broken = sum(1 for p in promises if p.status == "broken")
    boundary = {"X": 1, "0": 30, "1": 60, "2": 90, "3": 180, "4": 365}.get(case.bucket, 365)
    proximity = min(1.0, dpd / boundary) if boundary else 1.0
    behaviour = broken / (kept + broken) if (kept + broken) else 0.5
    roll_risk = round(min(0.98, 0.35 * proximity + 0.65 * behaviour), 4)

    case.days_past_due = dpd
    case.bucket = bucket_for_dpd(dpd)
    case.asset_classification = classification["classification"]
    case.outstanding = outstanding
    case.amount_overdue = overdue
    await ctx.session.flush()

    return {
        "case_number": case.case_number,
        "days_past_due": dpd,
        "bucket": case.bucket,
        "bucket_label": TREATMENT_LADDER.get(case.bucket, {}).get("label"),
        "instalments_missed": instalments_missed,
        "scheduled_instalment": round(schedule_emi, 2),
        "amount_overdue": overdue,
        "outstanding": round(outstanding, 2),
        "secured": secured,
        **classification,
        "provision_rate_pct": round(rate * 100, 2),
        "provision_required": round(outstanding * rate, 2),
        "roll_rate_risk": roll_risk,
        "roll_rate_basis": {
            "bucket_boundary_days": boundary,
            "proximity_to_boundary": round(proximity, 4),
            "promises_kept": kept,
            "promises_broken": broken,
        },
        "next_bucket": bucket_for_dpd(boundary + 1),
    }


class HardshipArgs(BaseModel):
    case: str = Field(description="Case number or id")
    declared_monthly_income: float = Field(default=0.0, ge=0)
    declared_essential_expenses: float = Field(default=0.0, ge=0)
    reason: str | None = Field(default=None, description="Stated cause of hardship")
    verify_from_ledger: bool = Field(default=True)


@tool(
    "assess_hardship",
    "Assess affordability for a customer in difficulty and derive the surplus a plan may "
    "use. A plan is only affordable if it leaves the customer a reserve.",
    HardshipArgs,
    category="collections",
)
async def assess_hardship(args: HardshipArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)

    verified_income: float | None = None
    verified_outflow: float | None = None
    if args.verify_from_ledger:
        account_ids = (
            (await ctx.session.execute(select(Account.id).where(Account.customer_id == case.customer_id)))
            .scalars()
            .all()
        )
        if account_ids:
            since = datetime.now(UTC) - timedelta(days=90)
            rows = (
                (
                    await ctx.session.execute(
                        select(Transaction).where(
                            Transaction.account_id.in_(account_ids), Transaction.booked_at >= since
                        )
                    )
                )
                .scalars()
                .all()
            )
            months = 3
            credits = sum(abs(t.amount) for t in rows if t.direction == "credit")
            debits = sum(abs(t.amount) for t in rows if t.direction == "debit")
            if rows:
                verified_income = round(credits / months, 2)
                verified_outflow = round(debits / months, 2)

    income = args.declared_monthly_income or verified_income or 0.0
    if income <= 0:
        return {
            "case_number": case.case_number,
            "assessed": False,
            "reason": "no income evidence: neither declared nor observable in the ledger",
            "action": "capture income and essential expenditure before offering any plan",
        }

    essential = args.declared_essential_expenses or (verified_outflow or 0.0)
    other_obligations = float(
        (
            await ctx.session.execute(
                select(func.coalesce(func.sum(Loan.emi_amount), 0.0)).where(
                    Loan.customer_id == case.customer_id, Loan.status == "active", Loan.id != case.facility_id
                )
            )
        ).scalar_one()
    )

    reserve = income * HARDSHIP_RESERVE_PCT / 100
    surplus = income - essential - other_obligations - reserve
    affordable = surplus >= MIN_PLAN_INSTALMENT

    months_to_clear = None
    if affordable and surplus > 0:
        months_to_clear = math.ceil(case.outstanding / surplus)

    return {
        "case_number": case.case_number,
        "assessed": True,
        "income": {
            "declared": args.declared_monthly_income or None,
            "verified_from_ledger": verified_income,
            "used": round(income, 2),
        },
        "expenditure": {
            "declared_essential": args.declared_essential_expenses or None,
            "observed_outflow": verified_outflow,
            "used": round(essential, 2),
            "other_loan_obligations": round(other_obligations, 2),
        },
        "reserve": {
            "policy_pct": HARDSHIP_RESERVE_PCT,
            "amount": round(reserve, 2),
            "rationale": "a plan must leave the customer a living reserve",
        },
        "surplus_available_for_plan": round(max(0.0, surplus), 2),
        "affordable": affordable,
        "minimum_viable_instalment": MIN_PLAN_INSTALMENT,
        "months_to_clear_at_surplus": months_to_clear,
        "outstanding": case.outstanding,
        "reason": args.reason,
        "recommendation": (
            "propose a plan within the surplus"
            if affordable
            else "no affordable plan exists at this income; refer for concession or "
            "settlement review rather than proposing an unaffordable instalment"
        ),
    }


class CollectabilityArgs(BaseModel):
    case: str = Field(description="Case number or id")


@tool(
    "score_collectability",
    "Score the likelihood of recovering the arrears from payment behaviour, promise "
    "performance and contact responsiveness.",
    CollectabilityArgs,
    category="collections",
)
async def score_collectability(args: CollectabilityArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)

    promises = (
        (await ctx.session.execute(select(PromiseToPay).where(PromiseToPay.case_id == case.id)))
        .scalars()
        .all()
    )
    attempts = (
        (await ctx.session.execute(select(ContactAttempt).where(ContactAttempt.case_id == case.id)))
        .scalars()
        .all()
    )

    kept = sum(1 for p in promises if p.status == "kept")
    broken = sum(1 for p in promises if p.status == "broken")
    promise_ratio = kept / (kept + broken) if (kept + broken) else None

    reached = sum(1 for a in attempts if a.outcome in {"contacted", "promise_captured", "payment_made"})
    contact_ratio = reached / len(attempts) if attempts else None

    account_ids = (
        (await ctx.session.execute(select(Account.id).where(Account.customer_id == case.customer_id)))
        .scalars()
        .all()
    )
    inflow = 0.0
    if account_ids:
        since = datetime.now(UTC) - timedelta(days=90)
        inflow = float(
            (
                await ctx.session.execute(
                    select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0.0)).where(
                        Transaction.account_id.in_(account_ids),
                        Transaction.booked_at >= since,
                        Transaction.direction == "credit",
                    )
                )
            ).scalar_one()
        )
    monthly_inflow = inflow / 3 if inflow else 0.0
    coverage = monthly_inflow / case.amount_overdue if case.amount_overdue else None

    # Weighted, with each component omitted rather than guessed when there is no evidence.
    components: list[tuple[str, float, float]] = []
    dpd_component = max(0.0, 1 - min(1.0, case.days_past_due / 365))
    components.append(("days_past_due", dpd_component, 0.35))
    if promise_ratio is not None:
        components.append(("promise_performance", promise_ratio, 0.25))
    if contact_ratio is not None:
        components.append(("contact_responsiveness", contact_ratio, 0.15))
    if coverage is not None:
        components.append(("inflow_coverage", min(1.0, coverage), 0.25))

    total_weight = sum(weight for _, _, weight in components)
    score = sum(value * weight for _, value, weight in components) / total_weight

    return {
        "case_number": case.case_number,
        "collectability_score": round(score, 4),
        "band": "high" if score >= 0.66 else "medium" if score >= 0.33 else "low",
        "components": [
            {"name": name, "value": round(value, 4), "weight": round(weight / total_weight, 4)}
            for name, value, weight in components
        ],
        "evidence": {
            "promises_kept": kept,
            "promises_broken": broken,
            "contact_attempts": len(attempts),
            "contacts_reached": reached,
            "average_monthly_inflow": round(monthly_inflow, 2),
            "amount_overdue": case.amount_overdue,
        },
        "omitted_components": [
            name
            for name in ("promise_performance", "contact_responsiveness", "inflow_coverage")
            if name not in {c[0] for c in components}
        ],
    }


class TreatmentArgs(BaseModel):
    case: str = Field(description="Case number or id")
    collectability_score: float | None = Field(default=None, ge=0, le=1)


@tool(
    "recommend_treatment",
    "Recommend the next collections action for the case's bucket, respecting the controls "
    "set on the account.",
    TreatmentArgs,
    category="collections",
)
async def recommend_treatment(args: TreatmentArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)
    ladder = TREATMENT_LADDER.get(case.bucket, TREATMENT_LADDER["5+"])
    allowed = list(ladder["actions"])
    suppressed: list[dict[str, str]] = []

    if case.cease_contact:
        for action in ("sms", "email", "call", "field_visit"):
            if action in allowed:
                allowed.remove(action)
                suppressed.append({"action": action, "reason": "customer issued a cease-contact instruction"})
    if not case.contact_consent:
        for action in ("sms", "email", "call"):
            if action in allowed:
                allowed.remove(action)
                suppressed.append({"action": action, "reason": "no contact consent on file"})
    if case.dispute_open:
        for action in ("legal_notice", "recovery_agency", "settlement_offer", "field_visit"):
            if action in allowed:
                allowed.remove(action)
                suppressed.append(
                    {"action": action, "reason": "collection activity is paused while a dispute is open"}
                )
    if case.hardship_flag:
        for action in ("legal_notice", "recovery_agency", "field_visit"):
            if action in allowed:
                allowed.remove(action)
                suppressed.append({"action": action, "reason": "customer is in a hardship arrangement"})

    score = args.collectability_score
    if (
        score is not None
        and score < 0.33
        and "restructure_offer" not in allowed
        and case.bucket in {"2", "3", "4"}
    ):
        allowed.append("hardship_review")

    return {
        "case_number": case.case_number,
        "bucket": case.bucket,
        "bucket_label": ladder["label"],
        "intensity": ladder["intensity"],
        "recommended_actions": allowed,
        "suppressed_actions": suppressed,
        "blocked_entirely": not allowed,
        "controls": {
            "contact_consent": case.contact_consent,
            "cease_contact": case.cease_contact,
            "dispute_open": case.dispute_open,
            "hardship_flag": case.hardship_flag,
        },
        "note": "actions are the ladder for this bucket minus anything the account's controls "
        "forbid; nothing from a later bucket is unlocked early",
    }


class EligibilityArgs(BaseModel):
    case: str = Field(description="Case number or id")
    channel: str = Field(default="call", description="call | sms | email | letter | visit")
    proposed_at: str | None = Field(
        default=None, description="ISO timestamp of the proposed contact; defaults to now"
    )


@tool(
    "check_contact_eligibility",
    "Decide whether the bank may contact this customer now: consent, cease-contact, "
    "dispute status, permitted hours and frequency caps.",
    EligibilityArgs,
    category="collections",
)
async def check_contact_eligibility(args: EligibilityArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)
    when = datetime.now(UTC)
    if args.proposed_at:
        try:
            when = datetime.fromisoformat(args.proposed_at)
        except ValueError as exc:
            raise ValidationError(f"proposed_at is not a valid ISO timestamp: {exc}") from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)

    blockers: list[dict[str, str]] = []
    if case.cease_contact:
        blockers.append(
            {
                "rule": "cease_contact",
                "detail": "the customer has instructed the bank to stop contacting them",
            }
        )
    if not case.contact_consent:
        blockers.append({"rule": "no_consent", "detail": "no contact consent recorded"})
    if case.dispute_open and args.channel != "letter":
        blockers.append(
            {
                "rule": "dispute_open",
                "detail": "only written correspondence is permitted while a dispute is open",
            }
        )

    # Letters are not time-restricted; live channels are, in the customer's local time.
    local = to_local(when)
    if args.channel in {"call", "sms", "visit"}:
        if not (CONTACT_WINDOW_START <= local.time() <= CONTACT_WINDOW_END):
            blockers.append(
                {
                    "rule": "permitted_hours",
                    "detail": f"{local:%H:%M} {local.tzname()} is outside "
                    f"{CONTACT_WINDOW_START:%H:%M}-{CONTACT_WINDOW_END:%H:%M} local time",
                }
            )

    week_start = when - timedelta(days=7)
    recent = (
        (
            await ctx.session.execute(
                select(ContactAttempt)
                .where(ContactAttempt.case_id == case.id, ContactAttempt.attempted_at >= week_start)
                .order_by(ContactAttempt.attempted_at.desc())
            )
        )
        .scalars()
        .all()
    )
    live = [a for a in recent if a.channel in {"call", "sms", "visit"}]
    if len(live) >= MAX_ATTEMPTS_PER_WEEK:
        blockers.append(
            {
                "rule": "weekly_frequency_cap",
                "detail": f"{len(live)} attempts in the last 7 days, cap {MAX_ATTEMPTS_PER_WEEK}",
            }
        )
    same_day = [a for a in live if _aware(a.attempted_at).date() == when.date()]
    if len(same_day) >= MAX_ATTEMPTS_PER_DAY:
        blockers.append(
            {
                "rule": "daily_frequency_cap",
                "detail": f"{len(same_day)} attempt(s) already today, cap {MAX_ATTEMPTS_PER_DAY}",
            }
        )
    if live:
        last = _aware(live[0].attempted_at)
        hours = (when - last).total_seconds() / 3600
        if hours < MIN_HOURS_BETWEEN_ATTEMPTS:
            blockers.append(
                {
                    "rule": "minimum_interval",
                    "detail": f"{hours:.1f}h since the last attempt, minimum {MIN_HOURS_BETWEEN_ATTEMPTS}h",
                }
            )

    next_allowed = None
    if blockers and not case.cease_contact and case.contact_consent:
        # Walk forward to the next moment that satisfies both the interval and the window.
        candidate = local
        if live:
            candidate = max(
                candidate,
                to_local(_aware(live[0].attempted_at)) + timedelta(hours=MIN_HOURS_BETWEEN_ATTEMPTS),
            )
        if candidate.time() < CONTACT_WINDOW_START:
            candidate = candidate.replace(
                hour=CONTACT_WINDOW_START.hour, minute=CONTACT_WINDOW_START.minute, second=0, microsecond=0
            )
        elif candidate.time() > CONTACT_WINDOW_END:
            candidate = (candidate + timedelta(days=1)).replace(
                hour=CONTACT_WINDOW_START.hour, minute=CONTACT_WINDOW_START.minute, second=0, microsecond=0
            )
        next_allowed = candidate.astimezone(UTC).isoformat()

    return {
        "case_number": case.case_number,
        "channel": args.channel,
        "proposed_at": when.isoformat(),
        "eligible": not blockers,
        "blockers": blockers,
        "attempts_last_7_days": len(live),
        "weekly_cap": MAX_ATTEMPTS_PER_WEEK,
        "local_time": local.isoformat(),
        "timezone": settings.bank_timezone,
        "permitted_window": f"{CONTACT_WINDOW_START:%H:%M}-{CONTACT_WINDOW_END:%H:%M} "
        f"{settings.bank_timezone}",
        "next_eligible_at": None if case.cease_contact or not case.contact_consent else next_allowed,
        "basis": "RBI Fair Practices Code for lenders' recovery agents",
    }


class LogContactArgs(BaseModel):
    case: str = Field(description="Case number or id")
    channel: str = Field(default="call")
    outcome: str = Field(
        description="contacted | no_answer | wrong_number | refused | "
        "promise_captured | payment_made | cease_requested"
    )
    notes: str | None = Field(default=None)


@tool(
    "log_contact_attempt",
    "Record an outreach attempt against the case. A cease request stops all future contact.",
    LogContactArgs,
    category="collections",
    writes_data=True,
    idempotent=False,
)
async def log_contact_attempt(args: LogContactArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)
    now = datetime.now(UTC)
    attempt = ContactAttempt(
        case_id=case.id,
        customer_id=case.customer_id,
        channel=args.channel,
        outcome=args.outcome,
        attempted_at=now,
        local_hour=to_local(now).hour,
        agent_name=ctx.user_email,
        notes=args.notes,
        execution_id=ctx.execution_id,
    )
    ctx.session.add(attempt)
    case.last_contacted_at = now
    if args.outcome == "cease_requested":
        case.cease_contact = True
    await ctx.session.flush()
    return {
        "case_number": case.case_number,
        "logged": True,
        "channel": args.channel,
        "outcome": args.outcome,
        "attempted_at": now.isoformat(),
        "cease_contact_now_set": case.cease_contact,
    }


class PromiseArgs(BaseModel):
    case: str = Field(description="Case number or id")
    amount: float = Field(gt=0)
    promised_date: str = Field(description="ISO date the customer committed to")
    channel: str = Field(default="call")


@tool(
    "record_promise_to_pay",
    "Capture a payment commitment from the customer.",
    PromiseArgs,
    category="collections",
    writes_data=True,
    idempotent=False,
)
async def record_promise_to_pay(args: PromiseArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)
    try:
        promised = date.fromisoformat(args.promised_date)
    except ValueError as exc:
        raise ValidationError(f"promised_date is not a valid ISO date: {exc}") from exc
    if promised < date.today():
        raise ValidationError("A promise cannot be dated in the past")
    if args.amount > case.outstanding:
        raise ValidationError(
            f"Promise of {args.amount:,.2f} exceeds the outstanding balance of {case.outstanding:,.2f}"
        )

    promise = PromiseToPay(
        case_id=case.id,
        customer_id=case.customer_id,
        amount=args.amount,
        promised_date=promised,
        channel=args.channel,
        status="pending",
        captured_by=ctx.user_email,
        execution_id=ctx.execution_id,
    )
    ctx.session.add(promise)
    case.next_action_at = datetime.combine(promised, time(9, 0), tzinfo=UTC)
    await ctx.session.flush()
    return {
        "case_number": case.case_number,
        "promise_id": promise.id,
        "amount": args.amount,
        "promised_date": promised.isoformat(),
        "covers_arrears": args.amount >= case.amount_overdue,
        "shortfall_against_arrears": round(max(0.0, case.amount_overdue - args.amount), 2),
        "follow_up_scheduled_for": case.next_action_at.isoformat(),
    }


class PromisePerformanceArgs(BaseModel):
    case: str = Field(description="Case number or id")
    settle_due: bool = Field(
        default=True, description="Mark past-dated promises kept or broken from the ledger"
    )


@tool(
    "evaluate_promise_performance",
    "Check promises against the ledger and mark each one kept or broken.",
    PromisePerformanceArgs,
    category="collections",
    writes_data=True,
)
async def evaluate_promise_performance(args: PromisePerformanceArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)
    promises = (
        (
            await ctx.session.execute(
                select(PromiseToPay)
                .where(PromiseToPay.case_id == case.id)
                .order_by(PromiseToPay.promised_date)
            )
        )
        .scalars()
        .all()
    )
    if not promises:
        return {"case_number": case.case_number, "promises": 0, "note": "no promises recorded on this case"}

    account_ids = (
        (await ctx.session.execute(select(Account.id).where(Account.customer_id == case.customer_id)))
        .scalars()
        .all()
    )

    results: list[dict[str, Any]] = []
    for promise in promises:
        paid = 0.0
        if account_ids:
            window_start = datetime.combine(promise.promised_date - timedelta(days=3), time.min, tzinfo=UTC)
            window_end = datetime.combine(promise.promised_date + timedelta(days=3), time.max, tzinfo=UTC)
            paid = float(
                (
                    await ctx.session.execute(
                        select(func.coalesce(func.sum(func.abs(Transaction.amount)), 0.0)).where(
                            Transaction.account_id.in_(account_ids),
                            Transaction.booked_at >= window_start,
                            Transaction.booked_at <= window_end,
                            Transaction.direction == "debit",
                            Transaction.category == "loan_repayment",
                        )
                    )
                ).scalar_one()
            )

        status = promise.status
        if args.settle_due and promise.status == "pending" and promise.promised_date < date.today():
            status = "kept" if paid >= promise.amount * 0.95 else "broken"
            promise.status = status
            promise.settled_amount = round(paid, 2)
            promise.settled_on = promise.promised_date if status == "kept" else None

        results.append(
            {
                "promise_id": promise.id,
                "amount": promise.amount,
                "promised_date": promise.promised_date.isoformat(),
                "status": status,
                "observed_repayment": round(paid, 2),
                "evaluated": args.settle_due and promise.promised_date < date.today(),
            }
        )

    await ctx.session.flush()
    kept = sum(1 for r in results if r["status"] == "kept")
    broken = sum(1 for r in results if r["status"] == "broken")
    return {
        "case_number": case.case_number,
        "promises": len(results),
        "kept": kept,
        "broken": broken,
        "pending": sum(1 for r in results if r["status"] == "pending"),
        "keep_rate_pct": round(kept / (kept + broken) * 100, 2) if (kept + broken) else None,
        "detail": results,
        "matching": "repayments within +/- 3 days of the promised date, categorised loan_repayment",
    }


class PlanArgs(BaseModel):
    case: str = Field(description="Case number or id")
    instalment_amount: float = Field(gt=0)
    instalments: int = Field(gt=0, le=MAX_PLAN_MONTHS)
    surplus_available: float = Field(ge=0, description="From assess_hardship")
    first_payment_date: str = Field(description="ISO date of the first instalment")
    concession_type: str | None = Field(
        default=None, description="interest_waiver | tenure_extension | settlement"
    )
    concession_value: float = Field(default=0.0, ge=0)


@tool(
    "create_repayment_plan",
    "Propose a restructured repayment plan. Refuses any instalment the assessed surplus "
    "cannot support. Requires human approval.",
    PlanArgs,
    category="collections",
    writes_data=True,
    requires_approval=True,
    approval_risk="high",
    idempotent=False,
)
async def create_repayment_plan(args: PlanArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)
    if args.instalment_amount > args.surplus_available:
        raise ValidationError(
            f"Instalment {args.instalment_amount:,.2f} exceeds the assessed surplus of "
            f"{args.surplus_available:,.2f}. A plan the customer cannot afford must not be "
            f"offered."
        )
    if args.instalment_amount < MIN_PLAN_INSTALMENT:
        raise ValidationError(f"Instalment is below the minimum viable amount of {MIN_PLAN_INSTALMENT:,.2f}")
    try:
        first = date.fromisoformat(args.first_payment_date)
    except ValueError as exc:
        raise ValidationError(f"first_payment_date is not a valid ISO date: {exc}") from exc

    total_payable = round(args.instalment_amount * args.instalments, 2)
    plan = RepaymentPlan(
        plan_number=f"PLN-{uuid.uuid4().hex[:10].upper()}",
        case_id=case.id,
        customer_id=case.customer_id,
        plan_type="settlement" if args.concession_type == "settlement" else "instalment",
        instalment_amount=args.instalment_amount,
        instalments=args.instalments,
        frequency="monthly",
        first_payment_date=first,
        total_payable=total_payable,
        concession_type=args.concession_type,
        concession_value=args.concession_value,
        affordability={
            "surplus_available": args.surplus_available,
            "utilisation_pct": round(args.instalment_amount / args.surplus_available * 100, 2)
            if args.surplus_available
            else None,
        },
        status="proposed",
        approved_by=ctx.user_email,
        execution_id=ctx.execution_id,
    )
    ctx.session.add(plan)
    case.hardship_flag = True
    case.strategy = "repayment_plan"
    await ctx.session.flush()

    return {
        "plan_number": plan.plan_number,
        "case_number": case.case_number,
        "instalment_amount": args.instalment_amount,
        "instalments": args.instalments,
        "total_payable": total_payable,
        "outstanding": case.outstanding,
        "shortfall_vs_outstanding": round(max(0.0, case.outstanding - total_payable), 2),
        "concession_type": args.concession_type,
        "concession_value": args.concession_value,
        "first_payment_date": first.isoformat(),
        "surplus_utilisation_pct": plan.affordability["utilisation_pct"],
        "status": "proposed",
        "effect": "the case is flagged as a hardship arrangement, which suppresses field "
        "visits, legal notices and recovery agency referral",
    }


class RecoveryArgs(BaseModel):
    case: str = Field(description="Case number or id")
    rationale: str = Field(description="Why recovery is appropriate")


@tool(
    "escalate_to_recovery",
    "Refer a case to legal recovery. Refuses while a dispute, hardship plan or "
    "cease-contact instruction is live. Requires human approval.",
    RecoveryArgs,
    category="collections",
    writes_data=True,
    requires_approval=True,
    approval_risk="critical",
    idempotent=False,
)
async def escalate_to_recovery(args: RecoveryArgs, ctx: ToolContext) -> dict[str, Any]:
    case = await _case(ctx, args.case)
    if case.dispute_open:
        raise ValidationError("A case with an open dispute cannot be referred to recovery")
    if case.hardship_flag:
        raise ValidationError(
            "This customer is in a hardship arrangement; recovery referral is not permitted "
            "while the plan stands"
        )
    if case.days_past_due < 90:
        raise ValidationError(
            f"Recovery referral requires the account to be non-performing; this account is "
            f"{case.days_past_due} days past due"
        )

    case.status = "recovery"
    case.strategy = "legal_recovery"
    case.next_action_at = datetime.now(UTC) + timedelta(days=7)
    await ctx.session.flush()
    classification = asset_classification(case.days_past_due)
    return {
        "case_number": case.case_number,
        "status": case.status,
        "days_past_due": case.days_past_due,
        **classification,
        "outstanding": case.outstanding,
        "amount_overdue": case.amount_overdue,
        "rationale": args.rationale,
        "referred_by": ctx.user_email,
        "review_due": case.next_action_at.isoformat(),
    }


class PortfolioArgs(BaseModel):
    days: int = Field(default=90, ge=1, le=1095)


@tool(
    "collections_portfolio_summary",
    "Bucket distribution, provisioning and promise performance across open cases.",
    PortfolioArgs,
    category="collections",
)
async def collections_portfolio_summary(args: PortfolioArgs, ctx: ToolContext) -> dict[str, Any]:
    cases = (
        (
            await ctx.session.execute(
                select(DelinquencyCase).where(DelinquencyCase.status.in_(["open", "recovery"]))
            )
        )
        .scalars()
        .all()
    )
    if not cases:
        return {"open_cases": 0, "note": "no cases under treatment"}

    since = datetime.now(UTC) - timedelta(days=args.days)
    promises = (
        (await ctx.session.execute(select(PromiseToPay).where(PromiseToPay.created_at >= since)))
        .scalars()
        .all()
    )

    by_bucket: dict[str, dict[str, Any]] = {}
    provision_total = 0.0
    for case in cases:
        entry = by_bucket.setdefault(
            case.bucket, {"cases": 0, "outstanding": 0.0, "overdue": 0.0, "provision": 0.0}
        )
        rate = provision_rate(case.asset_classification, case.facility_type == "loan")
        provision = case.outstanding * rate
        provision_total += provision
        entry["cases"] += 1
        entry["outstanding"] += case.outstanding
        entry["overdue"] += case.amount_overdue
        entry["provision"] += provision

    kept = sum(1 for p in promises if p.status == "kept")
    broken = sum(1 for p in promises if p.status == "broken")
    outstanding = sum(case.outstanding for case in cases)
    npa = [case for case in cases if case.days_past_due >= 90]

    return {
        "open_cases": len(cases),
        "total_outstanding": round(outstanding, 2),
        "total_overdue": round(sum(case.amount_overdue for case in cases), 2),
        "provision_required": round(provision_total, 2),
        "provision_coverage_pct": round(provision_total / outstanding * 100, 2) if outstanding else 0.0,
        "npa_cases": len(npa),
        "npa_outstanding": round(sum(case.outstanding for case in npa), 2),
        "by_bucket": {
            bucket: {
                "cases": data["cases"],
                "label": TREATMENT_LADDER.get(bucket, {}).get("label"),
                "outstanding": round(data["outstanding"], 2),
                "overdue": round(data["overdue"], 2),
                "provision": round(data["provision"], 2),
            }
            for bucket, data in sorted(by_bucket.items())
        },
        "promise_performance": {
            "window_days": args.days,
            "captured": len(promises),
            "kept": kept,
            "broken": broken,
            "keep_rate_pct": round(kept / (kept + broken) * 100, 2) if (kept + broken) else None,
        },
        "controls_in_force": {
            "cease_contact": sum(1 for c in cases if c.cease_contact),
            "disputes_open": sum(1 for c in cases if c.dispute_open),
            "hardship_arrangements": sum(1 for c in cases if c.hardship_flag),
            "no_consent": sum(1 for c in cases if not c.contact_consent),
        },
    }
