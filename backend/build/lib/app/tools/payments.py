"""Payment operations tools: investigation, repair, screening resolution and returns.

Payment operations is a control function wearing a service function's clothes. The same
keystroke that fixes a mistyped beneficiary name is the one that commits wire stripping, so
the constraints are implemented rather than described:

* **ISO 20022 semantics** — a payment carries a UETR that survives every hop, a charge
  bearer that decides whether a short credit is a defect or the agreed outcome, and returns
  quote the ISO external reason code a correspondent would actually receive;
* **published validation** — IBAN check digits are ISO 13616 mod-97-10, BICs are ISO 9362,
  IFSC codes follow the RBI format. A payment is not "probably fine";
* **screening is terminal** — a true sanctions hit blocks the payment permanently. No tool
  here can release a payment whose screening has not been resolved, and none can resolve a
  hit as a false positive without a human;
* **the regulator's clock** — RBI's Harmonised Turn Around Time gives the customer a fixed
  per-day compensation once the deadline passes, whether or not anybody asks for it.

Nothing here moves money. The tools decide, record and instruct; settlement is the payment
system's job.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models.banking import (
    Customer,
    PaymentInstruction,
    PaymentInvestigation,
    PaymentRepair,
    PaymentReturn,
    PaymentScreeningHit,
    SanctionsEntry,
)
from app.tools.base import ToolContext, tool
from app.tools.kyc import name_similarity, normalise_name

log = get_logger("tools.payments")


# --- reference data -----------------------------------------------------------
#: ISO 20022 external return reason codes, restricted to the ones a retail and
#: correspondent operation actually issues. The description is the ISO wording.
RETURN_REASONS: dict[str, str] = {
    "AC01": "IncorrectAccountNumber - account number is invalid or missing",
    "AC04": "ClosedAccountNumber - account is closed",
    "AC06": "BlockedAccount - account is blocked",
    "AG01": "TransactionForbidden - transaction type not permitted on this account",
    "AM05": "Duplication - the payment duplicates one already processed",
    "BE01": "InconsistentWithEndCustomer - creditor name does not match the account",
    "BE04": "MissingCreditorAddress - creditor address is required and absent",
    "CURR": "IncorrectCurrency - currency not supported for this account",
    "MS03": "NotSpecifiedReasonAgentGenerated - no reason given by the agent",
    "RC01": "BankIdentifierIncorrect - the agent identifier is invalid",
    "RR01": "MissingDebtorAccountOrIdentification - regulatory data missing",
    "RR04": "RegulatoryReason - returned for a regulatory reason",
}

#: Charge bearers, ISO 20022 `ChrgBr`. OUR means the debtor pays every charge, so the
#: beneficiary must receive the full amount; under SHA and BEN a deduction is expected.
CHARGE_BEARERS = {"OUR", "SHA", "BEN", "DEBT", "CRED"}

#: Rail cut-offs in bank-local time, and how the value date is assigned after them. These
#: are the operating windows, not a service level: a payment submitted after cut-off simply
#: takes the next business day's value.
RAIL_CUTOFFS: dict[str, dict[str, Any]] = {
    "neft": {"cutoff": time(19, 0), "settles": "same_day", "business_days_only": True},
    "rtgs": {"cutoff": time(16, 30), "settles": "same_day", "business_days_only": True},
    "imps": {"cutoff": time(23, 59), "settles": "immediate", "business_days_only": False},
    "upi": {"cutoff": time(23, 59), "settles": "immediate", "business_days_only": False},
    "swift": {"cutoff": time(17, 0), "settles": "t+1", "business_days_only": True},
    "sepa": {"cutoff": time(15, 0), "settles": "t+1", "business_days_only": True},
    "ach": {"cutoff": time(14, 0), "settles": "t+1", "business_days_only": True},
}

#: RBI Harmonised Turn Around Time: the deadline by which a failed transaction must be
#: reversed, and the compensation per day of delay beyond it. Circular
#: DPSS.CO.PD No.629/02.01.014/2019-20.
HARMONISED_TAT: dict[str, dict[str, Any]] = {
    "upi": {"deadline_days": 1, "per_day_inr": 100.0, "description": "T+1 day"},
    "imps": {"deadline_days": 1, "per_day_inr": 100.0, "description": "T+1 day"},
    "neft": {"deadline_days": 1, "per_day_inr": 100.0, "description": "T+1 day"},
    "rtgs": {"deadline_days": 1, "per_day_inr": 100.0, "description": "T+1 day"},
    # Cross-border investigations are governed by the correspondent agreement rather than
    # the RBI circular, so no statutory per-day compensation attaches.
    "swift": {"deadline_days": 5, "per_day_inr": 0.0, "description": "T+5 (correspondent SLA)"},
    "sepa": {"deadline_days": 3, "per_day_inr": 0.0, "description": "T+3 (scheme rulebook)"},
    "ach": {"deadline_days": 3, "per_day_inr": 0.0, "description": "T+3 (scheme rulebook)"},
}

#: A screening hit at or above this similarity is not dismissible without a human. Below it
#: the name is different enough that an operator may clear it with a recorded rationale.
STRONG_MATCH_THRESHOLD = 0.85

BIC_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")


# --- published algorithms -----------------------------------------------------
def iban_valid(value: str) -> bool:
    """ISO 13616 mod-97-10. The check digits are the whole point of an IBAN."""
    cleaned = re.sub(r"\s+", "", (value or "").upper())
    if not IBAN_RE.match(cleaned):
        return False
    rotated = cleaned[4:] + cleaned[:4]
    numeric = "".join(str(int(c, 36)) for c in rotated)
    return int(numeric) % 97 == 1


def bic_valid(value: str) -> bool:
    """ISO 9362: institution (4 alpha), country (2 alpha), location (2), optional branch."""
    return bool(BIC_RE.match((value or "").upper()))


def ifsc_valid(value: str) -> bool:
    """RBI format: four-letter bank code, a reserved zero, then a six-character branch."""
    return bool(IFSC_RE.match((value or "").upper()))


def _next_business_day(day: date, *, skip_weekends: bool = True) -> date:
    nxt = day + timedelta(days=1)
    while skip_weekends and nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


async def _load_payment(ctx: ToolContext, reference: str) -> PaymentInstruction:
    stmt = select(PaymentInstruction).where(
        (PaymentInstruction.payment_reference == reference) | (PaymentInstruction.uetr == reference)
    )
    payment = (await ctx.session.execute(stmt)).scalars().first()
    if payment is None:
        raise NotFoundError(
            f"Payment '{reference}' not found",
            details={"searched": ["payment_reference", "uetr"]},
        )
    return payment


# --- read ---------------------------------------------------------------------
class PaymentRefArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR, e.g. PAY-100001")


@tool(
    name="get_payment",
    description=(
        "Read one payment instruction with its screening hits, open investigations, "
        "repairs and returns. Accepts either the payment reference or the UETR."
    ),
    args_model=PaymentRefArgs,
    category="payments",
)
async def get_payment(args: PaymentRefArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    hits = (
        (
            await ctx.session.execute(
                select(PaymentScreeningHit).where(PaymentScreeningHit.payment_id == payment.id)
            )
        )
        .scalars()
        .all()
    )
    cases = (
        (
            await ctx.session.execute(
                select(PaymentInvestigation).where(PaymentInvestigation.payment_id == payment.id)
            )
        )
        .scalars()
        .all()
    )
    repairs = (
        (await ctx.session.execute(select(PaymentRepair).where(PaymentRepair.payment_id == payment.id)))
        .scalars()
        .all()
    )
    returns = (
        (await ctx.session.execute(select(PaymentReturn).where(PaymentReturn.payment_id == payment.id)))
        .scalars()
        .all()
    )
    return {
        "payment_reference": payment.payment_reference,
        "uetr": payment.uetr,
        "message_type": payment.message_type,
        "direction": payment.direction,
        "rail": payment.rail,
        "status": payment.status,
        "status_reason": payment.status_reason,
        "screening_status": payment.screening_status,
        "currency": payment.currency,
        "amount": round(payment.amount, 2),
        "charge_bearer": payment.charge_bearer,
        "charges_deducted": round(payment.charges_deducted, 2),
        "debtor": {
            "name": payment.debtor_name,
            "account": payment.debtor_account,
            "agent_bic": payment.debtor_agent_bic,
        },
        "creditor": {
            "name": payment.creditor_name,
            "account": payment.creditor_account,
            "agent_bic": payment.creditor_agent_bic,
        },
        "intermediary_bic": payment.intermediary_bic,
        "remittance_info": payment.remittance_info,
        "purpose_code": payment.purpose_code,
        "submitted_at": payment.submitted_at.isoformat() if payment.submitted_at else None,
        "value_date": payment.value_date.isoformat() if payment.value_date else None,
        "settled_at": payment.settled_at.isoformat() if payment.settled_at else None,
        "cutoff_missed": payment.cutoff_missed,
        "duplicate_of": payment.duplicate_of,
        "screening_hits": [
            {
                "id": h.id,
                "matched_field": h.matched_field,
                "matched_value": h.matched_value,
                "list_name": h.list_name,
                "match_score": round(h.match_score, 4),
                "disposition": h.disposition,
                "decided_by": h.decided_by,
            }
            for h in hits
        ],
        "investigations": [
            {
                "case_number": c.case_number,
                "category": c.category,
                "status": c.status,
                "tat_deadline": c.tat_deadline.isoformat() if c.tat_deadline else None,
                "compensation_due": round(c.compensation_due, 2),
            }
            for c in cases
        ],
        "repairs": [
            {
                "field": r.field_name,
                "previous_value": r.previous_value,
                "repaired_value": r.repaired_value,
                "reason": r.reason,
                "approved_by": r.approved_by,
            }
            for r in repairs
        ],
        "returns": [
            {
                "return_reference": r.return_reference,
                "reason_code": r.reason_code,
                "returned_amount": round(r.returned_amount, 2),
                "status": r.status,
            }
            for r in returns
        ],
    }


@tool(
    name="trace_payment",
    description=(
        "Reconstruct a payment's journey from its UETR: submission, screening, cut-off and "
        "value dating, settlement or return, and where it is currently stalled. Use this "
        "before opening an investigation — most 'missing' payments are visible here."
    ),
    args_model=PaymentRefArgs,
    category="payments",
)
async def trace_payment(args: PaymentRefArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    now = datetime.now(UTC)

    legs: list[dict[str, Any]] = []
    if payment.submitted_at:
        legs.append(
            {
                "stage": "submitted",
                "at": payment.submitted_at.isoformat(),
                "detail": f"{payment.message_type} accepted on {payment.rail}",
            }
        )
    hits = (
        (
            await ctx.session.execute(
                select(PaymentScreeningHit).where(PaymentScreeningHit.payment_id == payment.id)
            )
        )
        .scalars()
        .all()
    )
    if hits:
        pending = [h for h in hits if h.disposition == "pending"]
        true_hits = [h for h in hits if h.disposition == "true_hit"]
        legs.append(
            {
                "stage": "screening",
                "at": hits[0].created_at.isoformat() if hits[0].created_at else None,
                "detail": (f"{len(hits)} hit(s); {len(pending)} pending, {len(true_hits)} confirmed"),
            }
        )
    elif payment.screening_status == "clear":
        legs.append({"stage": "screening", "at": None, "detail": "cleared with no hits"})

    if payment.cutoff_missed:
        legs.append(
            {
                "stage": "cutoff",
                "at": None,
                "detail": f"submitted after the {payment.rail.upper()} cut-off; value dated forward",
            }
        )
    if payment.settled_at:
        legs.append(
            {
                "stage": "settled",
                "at": payment.settled_at.isoformat(),
                "detail": f"credited with {payment.charge_bearer} charges",
            }
        )
    returns = (
        (await ctx.session.execute(select(PaymentReturn).where(PaymentReturn.payment_id == payment.id)))
        .scalars()
        .all()
    )
    for ret in returns:
        legs.append(
            {
                "stage": "returned",
                "at": ret.issued_at.isoformat() if ret.issued_at else None,
                "detail": f"{ret.reason_code} - {ret.reason_description}",
            }
        )

    # Where it is stuck, said plainly, because that is the question being asked.
    if payment.status == "settled":
        stalled_at = None
        summary = "Completed. The beneficiary was credited."
    elif payment.screening_status == "blocked":
        stalled_at = "screening"
        summary = "Blocked by sanctions screening. It cannot be released."
    elif payment.screening_status == "hold":
        stalled_at = "screening"
        summary = "On hold pending resolution of a screening hit."
    elif payment.status == "returned":
        stalled_at = None
        summary = "Returned to the originator."
    elif payment.value_date and payment.value_date > now.date():
        stalled_at = "value_date"
        summary = f"Accepted and value dated {payment.value_date.isoformat()}; not yet due."
    else:
        stalled_at = payment.status
        summary = f"In '{payment.status}' with no settlement recorded."

    age_hours = (
        round((now - payment.submitted_at).total_seconds() / 3600, 1) if payment.submitted_at else None
    )
    return {
        "payment_reference": payment.payment_reference,
        "uetr": payment.uetr,
        "rail": payment.rail,
        "amount": round(payment.amount, 2),
        "currency": payment.currency,
        "legs": legs,
        "stalled_at": stalled_at,
        "summary": summary,
        "age_hours": age_hours,
        "settled": payment.status == "settled",
    }


# --- validation ---------------------------------------------------------------
class ValidateArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR")


@tool(
    name="validate_payment_details",
    description=(
        "Check a payment's identifiers against their published formats: IBAN check digits "
        "(ISO 13616 mod-97), BIC structure (ISO 9362), IFSC format, currency and charge "
        "bearer. Returns the specific field at fault, which is what a return reason code "
        "has to be derived from."
    ),
    args_model=ValidateArgs,
    category="payments",
)
async def validate_payment_details(args: ValidateArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    problems: list[dict[str, str]] = []

    creditor_account = (payment.creditor_account or "").strip()
    # An account is an IBAN in the SEPA and correspondent world and a domestic number
    # elsewhere; only the IBAN form carries check digits that can be verified.
    if payment.rail in {"sepa", "swift"} and creditor_account[:2].isalpha():
        if not iban_valid(creditor_account):
            problems.append(
                {
                    "field": "creditor_account",
                    "problem": "IBAN check digits do not validate (ISO 13616 mod-97-10)",
                    "suggested_reason_code": "AC01",
                }
            )
    elif payment.rail in {"neft", "rtgs", "imps"} and not creditor_account:
        problems.append(
            {
                "field": "creditor_account",
                "problem": "account number is missing",
                "suggested_reason_code": "AC01",
            }
        )

    for field_name, value in (
        ("creditor_agent_bic", payment.creditor_agent_bic),
        ("debtor_agent_bic", payment.debtor_agent_bic),
        ("intermediary_bic", payment.intermediary_bic),
    ):
        if not value:
            continue
        # Domestic rails identify the branch by IFSC; SWIFT and SEPA use a BIC.
        looks_domestic = payment.rail in {"neft", "rtgs", "imps"}
        ok = ifsc_valid(value) if looks_domestic else bic_valid(value)
        if not ok:
            problems.append(
                {
                    "field": field_name,
                    "problem": (
                        "not a valid IFSC (RBI format)" if looks_domestic else "not a valid BIC (ISO 9362)"
                    ),
                    "suggested_reason_code": "RC01",
                }
            )

    if not payment.creditor_name.strip():
        problems.append(
            {
                "field": "creditor_name",
                "problem": "beneficiary name is missing",
                "suggested_reason_code": "BE01",
            }
        )
    if payment.charge_bearer not in CHARGE_BEARERS:
        problems.append(
            {
                "field": "charge_bearer",
                "problem": f"'{payment.charge_bearer}' is not an ISO 20022 charge bearer",
                "suggested_reason_code": "MS03",
            }
        )
    if len(payment.currency or "") != 3:
        problems.append(
            {
                "field": "currency",
                "problem": "currency is not a three-letter ISO 4217 code",
                "suggested_reason_code": "CURR",
            }
        )

    return {
        "payment_reference": payment.payment_reference,
        "valid": not problems,
        "problem_count": len(problems),
        "problems": problems,
        "checks_run": [
            "iban_mod97",
            "bic_iso9362",
            "ifsc_format",
            "beneficiary_name_present",
            "charge_bearer_iso20022",
            "currency_iso4217",
        ],
    }


# --- screening ----------------------------------------------------------------
class ScreenArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR")
    threshold: float = Field(default=0.80, ge=0.5, le=1.0, description="Match score floor")


@tool(
    name="screen_payment_parties",
    description=(
        "Screen every named party on the payment — debtor, creditor, agents and the "
        "remittance narrative — against the sanctions and watchlist tables. Records any "
        "hit and puts the payment on hold. Screening is never optional and never skipped."
    ),
    args_model=ScreenArgs,
    category="payments",
    writes_data=True,
)
async def screen_payment_parties(args: ScreenArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    entries = (await ctx.session.execute(select(SanctionsEntry))).scalars().all()

    parties = [
        ("debtor_name", payment.debtor_name),
        ("creditor_name", payment.creditor_name),
        ("remittance_info", payment.remittance_info),
    ]
    existing = {
        (h.matched_field, h.matched_value)
        for h in (
            await ctx.session.execute(
                select(PaymentScreeningHit).where(PaymentScreeningHit.payment_id == payment.id)
            )
        )
        .scalars()
        .all()
    }

    found: list[dict[str, Any]] = []
    for field_name, raw in parties:
        text = normalise_name(raw or "")
        if not text:
            continue
        for entry in entries:
            candidates = [entry.normalised_name, *(normalise_name(a) for a in entry.aliases or [])]
            best = max((name_similarity(text, c) for c in candidates if c), default=0.0)
            if best < args.threshold:
                continue
            hit = {
                "matched_field": field_name,
                "matched_value": (raw or "")[:200],
                "list_name": entry.list_name,
                "list_entry_id": entry.id,
                "match_score": round(best, 4),
                "listed_name": entry.full_name,
                "program": entry.program,
                "is_pep": entry.is_pep,
            }
            found.append(hit)
            if (field_name, (raw or "")[:200]) not in existing:
                ctx.session.add(
                    PaymentScreeningHit(
                        payment_id=payment.id,
                        matched_field=field_name,
                        matched_value=(raw or "")[:200],
                        list_name=entry.list_name,
                        list_entry_id=entry.id,
                        match_score=round(best, 4),
                        disposition="pending",
                        execution_id=ctx.execution_id,
                    )
                )

    if found:
        payment.screening_status = "hold"
        payment.status = "on_hold"
        payment.status_reason = f"{len(found)} screening hit(s) pending disposition"
    else:
        payment.screening_status = "clear"
    await ctx.session.flush()

    return {
        "payment_reference": payment.payment_reference,
        "screened_parties": [p for p, v in parties if v],
        "entries_compared": len(entries),
        "threshold": args.threshold,
        "hit_count": len(found),
        "hits": found,
        "screening_status": payment.screening_status,
        "payment_status": payment.status,
    }


class ResolveHitArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR")
    hit_id: str = Field(description="Screening hit id from get_payment")
    disposition: str = Field(description="true_hit or false_positive")
    rationale: str = Field(description="Why the hit is or is not the listed party")


@tool(
    name="resolve_screening_hit",
    description=(
        "Record the disposition of a sanctions hit. A true hit blocks the payment "
        "permanently and is reportable. A false positive needs a rationale naming what "
        "distinguishes this party from the listed one."
    ),
    args_model=ResolveHitArgs,
    category="payments",
    requires_approval=True,
    approval_risk="critical",
    writes_data=True,
    idempotent=False,
)
async def resolve_screening_hit(args: ResolveHitArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    hit = (
        (
            await ctx.session.execute(
                select(PaymentScreeningHit).where(
                    PaymentScreeningHit.id == args.hit_id,
                    PaymentScreeningHit.payment_id == payment.id,
                )
            )
        )
        .scalars()
        .first()
    )
    if hit is None:
        raise NotFoundError(f"Screening hit '{args.hit_id}' not found on this payment")
    if args.disposition not in {"true_hit", "false_positive"}:
        raise ValidationError("disposition must be 'true_hit' or 'false_positive'")
    if len(args.rationale.strip()) < 20:
        raise ValidationError(
            "A screening disposition needs a rationale a reviewer can audit (at least 20 characters)"
        )
    # A near-exact name match is not an operator's call. Below the threshold the names are
    # different enough that a documented rationale is a legitimate disposition; at or above
    # it, only the sanctions team clears the hit.
    if args.disposition == "false_positive" and hit.match_score >= STRONG_MATCH_THRESHOLD:
        raise ValidationError(
            f"Match score {hit.match_score:.2f} is at or above the strong-match threshold "
            f"of {STRONG_MATCH_THRESHOLD}; this hit must be escalated to the sanctions "
            "team rather than dismissed",
            details={"match_score": hit.match_score, "list": hit.list_name},
        )

    hit.disposition = args.disposition
    hit.rationale = args.rationale.strip()
    hit.decided_by = ctx.user_email
    hit.decided_at = datetime.now(UTC)
    hit.execution_id = ctx.execution_id
    # The session runs with autoflush off, so the counts below would read the disposition
    # this call just replaced and leave the payment on hold after its last hit was cleared.
    await ctx.session.flush()

    remaining = (
        await ctx.session.execute(
            select(func.count(PaymentScreeningHit.id)).where(
                PaymentScreeningHit.payment_id == payment.id,
                PaymentScreeningHit.disposition == "pending",
            )
        )
    ).scalar_one()
    confirmed = (
        await ctx.session.execute(
            select(func.count(PaymentScreeningHit.id)).where(
                PaymentScreeningHit.payment_id == payment.id,
                PaymentScreeningHit.disposition == "true_hit",
            )
        )
    ).scalar_one()

    if confirmed:
        payment.screening_status = "blocked"
        payment.status = "blocked"
        payment.status_reason = "Confirmed sanctions match; funds frozen and reportable"
    elif remaining == 0:
        payment.screening_status = "clear"
        payment.status = "pending"
        payment.status_reason = "Screening hits cleared as false positives"
    await ctx.session.flush()

    log.info(
        "screening_hit_resolved",
        payment=payment.payment_reference,
        disposition=args.disposition,
        score=hit.match_score,
    )
    return {
        "payment_reference": payment.payment_reference,
        "hit_id": hit.id,
        "disposition": hit.disposition,
        "pending_hits": int(remaining),
        "confirmed_hits": int(confirmed),
        "screening_status": payment.screening_status,
        "payment_status": payment.status,
        "reportable": bool(confirmed),
    }


# --- duplicates, cut-off and value dating -------------------------------------
class DuplicateArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR")
    window_hours: int = Field(default=48, ge=1, le=720, description="Look-back window")


@tool(
    name="detect_duplicate_payment",
    description=(
        "Look for an earlier payment with the same debtor, creditor, amount and currency "
        "inside the window. A duplicate is returned under AM05, not repaired."
    ),
    args_model=DuplicateArgs,
    category="payments",
)
async def detect_duplicate_payment(args: DuplicateArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    anchor = payment.submitted_at or payment.created_at or datetime.now(UTC)
    since = anchor - timedelta(hours=args.window_hours)

    candidates = (
        (
            await ctx.session.execute(
                select(PaymentInstruction).where(
                    PaymentInstruction.id != payment.id,
                    PaymentInstruction.creditor_account == payment.creditor_account,
                    PaymentInstruction.debtor_account == payment.debtor_account,
                    PaymentInstruction.currency == payment.currency,
                    PaymentInstruction.submitted_at >= since,
                    PaymentInstruction.submitted_at <= anchor,
                )
            )
        )
        .scalars()
        .all()
    )

    # Same amount to the paisa, or a same end-to-end id: either is the scheme definition of
    # a duplicate. A similar-but-different amount is a separate payment.
    matches = [
        c
        for c in candidates
        if round(c.amount, 2) == round(payment.amount, 2)
        or (payment.end_to_end_id and c.end_to_end_id == payment.end_to_end_id)
    ]
    return {
        "payment_reference": payment.payment_reference,
        "window_hours": args.window_hours,
        "candidates_examined": len(candidates),
        "duplicate_found": bool(matches),
        "duplicates": [
            {
                "payment_reference": m.payment_reference,
                "uetr": m.uetr,
                "amount": round(m.amount, 2),
                "status": m.status,
                "submitted_at": m.submitted_at.isoformat() if m.submitted_at else None,
            }
            for m in matches
        ],
        "suggested_reason_code": "AM05" if matches else None,
    }


class CutoffArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR")


@tool(
    name="check_cutoff_and_value_date",
    description=(
        "Compare the submission time against the rail's cut-off and report the value date "
        "that follows. Explains a 'late' payment that is in fact on schedule."
    ),
    args_model=CutoffArgs,
    category="payments",
)
async def check_cutoff_and_value_date(args: CutoffArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    rail = RAIL_CUTOFFS.get(payment.rail)
    if rail is None:
        raise ValidationError(
            f"No cut-off configured for rail '{payment.rail}'",
            details={"known_rails": sorted(RAIL_CUTOFFS)},
        )
    if payment.submitted_at is None:
        raise ValidationError("Payment has not been submitted, so no cut-off applies")

    from app.tools.collections import to_local

    local = to_local(payment.submitted_at)
    cutoff: time = rail["cutoff"]
    after_cutoff = local.time() > cutoff
    weekend = local.weekday() >= 5 and rail["business_days_only"]

    expected = local.date()
    if after_cutoff or weekend:
        expected = _next_business_day(expected, skip_weekends=rail["business_days_only"])
    if rail["settles"] == "t+1":
        expected = _next_business_day(expected, skip_weekends=rail["business_days_only"])

    return {
        "payment_reference": payment.payment_reference,
        "rail": payment.rail,
        "submitted_local": local.isoformat(),
        "cutoff_local": cutoff.isoformat(),
        "after_cutoff": after_cutoff,
        "non_business_day": weekend,
        "settlement_basis": rail["settles"],
        "expected_value_date": expected.isoformat(),
        "recorded_value_date": payment.value_date.isoformat() if payment.value_date else None,
        "on_schedule": payment.value_date is None or payment.value_date <= expected,
        "explanation": (
            f"Submitted {local.strftime('%H:%M')} local against a {cutoff.strftime('%H:%M')} "
            f"cut-off on {payment.rail.upper()}"
            + (", so value moves to the next business day" if after_cutoff or weekend else "")
        ),
    }


# --- investigation ------------------------------------------------------------
class OpenInvestigationArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR")
    category: str = Field(
        description=(
            "non_receipt | wrong_beneficiary | short_credit | duplicate | delay | recall | unauthorised"
        )
    )
    description: str = Field(description="What the customer or correspondent reported")
    raised_by: str = Field(default="customer", description="customer | correspondent | internal")


@tool(
    name="open_payment_investigation",
    description=(
        "Open an investigation case on a payment and set the regulatory turnaround "
        "deadline for its rail. Trace the payment first — most reports resolve without a "
        "case."
    ),
    args_model=OpenInvestigationArgs,
    category="payments",
    writes_data=True,
    idempotent=False,
)
async def open_payment_investigation(args: OpenInvestigationArgs, ctx: ToolContext) -> dict[str, Any]:
    valid = {
        "non_receipt",
        "wrong_beneficiary",
        "short_credit",
        "duplicate",
        "delay",
        "recall",
        "unauthorised",
    }
    if args.category not in valid:
        raise ValidationError(
            f"Unknown investigation category '{args.category}'",
            details={"valid": sorted(valid)},
        )
    payment = await _load_payment(ctx, args.reference)
    existing = (
        (
            await ctx.session.execute(
                select(PaymentInvestigation).where(
                    PaymentInvestigation.payment_id == payment.id,
                    PaymentInvestigation.status == "open",
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return {
            "created": False,
            "reason": "an investigation is already open on this payment",
            "case_number": existing.case_number,
            "category": existing.category,
            "tat_deadline": existing.tat_deadline.isoformat() if existing.tat_deadline else None,
        }

    tat = HARMONISED_TAT.get(payment.rail, {"deadline_days": 5, "per_day_inr": 0.0})
    anchor = payment.submitted_at or datetime.now(UTC)
    deadline = anchor + timedelta(days=int(tat["deadline_days"]))
    case_number = f"PI-{uuid.uuid4().hex[:10].upper()}"

    case = PaymentInvestigation(
        case_number=case_number,
        payment_id=payment.id,
        category=args.category,
        raised_by=args.raised_by,
        description=args.description[:4000],
        status="open",
        tat_deadline=deadline,
        opened_at=datetime.now(UTC),
        execution_id=ctx.execution_id,
    )
    ctx.session.add(case)
    await ctx.session.flush()
    log.info(
        "payment_investigation_opened",
        case=case_number,
        payment=payment.payment_reference,
        category=args.category,
    )
    return {
        "created": True,
        "case_number": case_number,
        "payment_reference": payment.payment_reference,
        "category": args.category,
        "tat_basis": tat.get("description", "correspondent SLA"),
        "tat_deadline": deadline.isoformat(),
        "compensation_per_day_inr": tat.get("per_day_inr", 0.0),
    }


class CompensationArgs(BaseModel):
    case_number: str = Field(description="Investigation case number, e.g. PI-ABC1234567")


@tool(
    name="calculate_compensation",
    description=(
        "Compute the compensation owed under the RBI Harmonised Turn Around Time for a "
        "failed transaction that was not reversed by the deadline. The compensation is due "
        "automatically, whether or not the customer asked for it."
    ),
    args_model=CompensationArgs,
    category="payments",
    writes_data=True,
)
async def calculate_compensation(args: CompensationArgs, ctx: ToolContext) -> dict[str, Any]:
    case = (
        (
            await ctx.session.execute(
                select(PaymentInvestigation).where(PaymentInvestigation.case_number == args.case_number)
            )
        )
        .scalars()
        .first()
    )
    if case is None:
        raise NotFoundError(f"Investigation '{args.case_number}' not found")
    payment = (
        (
            await ctx.session.execute(
                select(PaymentInstruction).where(PaymentInstruction.id == case.payment_id)
            )
        )
        .scalars()
        .one()
    )

    tat = HARMONISED_TAT.get(payment.rail, {"deadline_days": 5, "per_day_inr": 0.0})
    per_day = float(tat.get("per_day_inr", 0.0))
    now = datetime.now(UTC)
    # The clock stops when the case is resolved, not when it is looked at.
    stop = case.closed_at or now
    deadline = case.tat_deadline or (payment.submitted_at or now)
    overdue_days = max(0, (stop.date() - deadline.date()).days)
    due = round(overdue_days * per_day, 2)

    case.compensation_due = due
    await ctx.session.flush()

    return {
        "case_number": case.case_number,
        "payment_reference": payment.payment_reference,
        "rail": payment.rail,
        "basis": tat.get("description", "correspondent SLA"),
        "tat_deadline": deadline.isoformat(),
        "measured_to": stop.isoformat(),
        "days_beyond_deadline": overdue_days,
        "per_day_inr": per_day,
        "compensation_due_inr": due,
        "already_paid_inr": round(case.compensation_paid, 2),
        "statutory": per_day > 0,
        "note": (
            "Payable automatically under RBI DPSS Harmonised TAT"
            if per_day > 0
            else "No statutory per-day compensation on this rail; correspondent SLA applies"
        ),
    }


class ClassifyReturnArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR")


@tool(
    name="classify_return_reason",
    description=(
        "Derive the ISO 20022 external return reason code from what is actually wrong with "
        "the payment. Returns the candidate codes with the evidence for each, so a return "
        "is issued under a code the correspondent can act on."
    ),
    args_model=ClassifyReturnArgs,
    category="payments",
)
async def classify_return_reason(args: ClassifyReturnArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    validation = await validate_payment_details(ValidateArgs(reference=args.reference), ctx)
    duplicate = await detect_duplicate_payment(DuplicateArgs(reference=args.reference, window_hours=48), ctx)

    candidates: list[dict[str, Any]] = []
    if duplicate["duplicate_found"]:
        candidates.append(
            {
                "reason_code": "AM05",
                "description": RETURN_REASONS["AM05"],
                "evidence": f"{len(duplicate['duplicates'])} earlier payment(s) with the same "
                f"debtor, creditor and amount",
                "confidence": "high",
            }
        )
    for problem in validation["problems"]:
        code = problem["suggested_reason_code"]
        candidates.append(
            {
                "reason_code": code,
                "description": RETURN_REASONS.get(code, code),
                "evidence": f"{problem['field']}: {problem['problem']}",
                "confidence": "high",
            }
        )
    if payment.screening_status == "blocked":
        candidates.append(
            {
                "reason_code": "RR04",
                "description": RETURN_REASONS["RR04"],
                "evidence": "confirmed sanctions match on this payment",
                "confidence": "high",
                # A blocked payment is frozen, not returned: returning it would release
                # funds to the originator, which is itself a sanctions breach.
                "blocked": True,
            }
        )

    returnable = [c for c in candidates if not c.get("blocked")]
    return {
        "payment_reference": payment.payment_reference,
        "candidates": candidates,
        "recommended_code": returnable[0]["reason_code"] if returnable else None,
        "returnable": bool(returnable) and payment.screening_status != "blocked",
        "note": (
            "Payment is blocked by sanctions screening and must not be returned; the funds "
            "are frozen and the case is reportable."
            if payment.screening_status == "blocked"
            else ""
        ),
    }


# --- actions that change a payment --------------------------------------------
class RepairArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR")
    field_name: str = Field(description="creditor_account | creditor_agent_bic | remittance_info")
    repaired_value: str = Field(description="The corrected value")
    reason: str = Field(description="Why the change is correct, citing the source")


#: Fields a repair may touch. The originator and beneficiary *names* are deliberately absent:
#: altering them is how wire stripping is committed, and no operational reason justifies it.
REPAIRABLE_FIELDS = {
    "creditor_account",
    "creditor_agent_bic",
    "intermediary_bic",
    "remittance_info",
    "purpose_code",
}


@tool(
    name="repair_payment",
    description=(
        "Amend a formatting or routing field on a held payment. Party names cannot be "
        "repaired. Every repair records the previous value and the human who approved it."
    ),
    args_model=RepairArgs,
    category="payments",
    requires_approval=True,
    approval_risk="high",
    writes_data=True,
    idempotent=False,
)
async def repair_payment(args: RepairArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    if args.field_name not in REPAIRABLE_FIELDS:
        raise ValidationError(
            f"'{args.field_name}' cannot be repaired. Altering a party name on a payment "
            "in flight is wire stripping; a payment with the wrong beneficiary is returned "
            "and re-originated instead.",
            details={"repairable": sorted(REPAIRABLE_FIELDS)},
        )
    if payment.screening_status == "blocked":
        raise ValidationError("This payment is blocked by a confirmed sanctions match and cannot be amended")
    if payment.status == "settled":
        raise ValidationError("A settled payment cannot be repaired; raise a recall instead")

    previous = str(getattr(payment, args.field_name) or "")
    new_value = args.repaired_value.strip()

    # A repair that does not produce a valid identifier is not a repair.
    if args.field_name == "creditor_agent_bic":
        domestic = payment.rail in {"neft", "rtgs", "imps"}
        ok = ifsc_valid(new_value) if domestic else bic_valid(new_value)
        if not ok:
            raise ValidationError(
                f"'{new_value}' is not a valid {'IFSC' if domestic else 'BIC'}",
                details={"field": args.field_name},
            )
    if args.field_name == "creditor_account" and payment.rail in {"sepa", "swift"}:
        if new_value[:2].isalpha() and not iban_valid(new_value):
            raise ValidationError(
                f"'{new_value}' fails the ISO 13616 check-digit test",
                details={"field": args.field_name},
            )

    setattr(payment, args.field_name, new_value)
    payment.status = "pending"
    payment.status_reason = f"Repaired {args.field_name}; awaiting re-screening"
    # A repaired payment has changed its parties' routing, so its screening no longer holds.
    payment.screening_status = "unscreened"
    ctx.session.add(
        PaymentRepair(
            payment_id=payment.id,
            field_name=args.field_name,
            previous_value=previous[:280],
            repaired_value=new_value[:280],
            reason=args.reason[:140],
            repair_type="routing" if "bic" in args.field_name else "format",
            status="applied",
            approved_by=ctx.user_email,
            execution_id=ctx.execution_id,
        )
    )
    await ctx.session.flush()
    log.info(
        "payment_repaired",
        payment=payment.payment_reference,
        field=args.field_name,
        by=ctx.user_email,
    )
    return {
        "payment_reference": payment.payment_reference,
        "field": args.field_name,
        "previous_value": previous,
        "repaired_value": new_value,
        "status": payment.status,
        "screening_status": payment.screening_status,
        "next_step": "re-screen the payment before release",
    }


class ReturnArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR")
    reason_code: str = Field(description="ISO 20022 external return reason, e.g. AC01")
    note: str = Field(default="", description="Free-text detail for the correspondent")


@tool(
    name="issue_payment_return",
    description=(
        "Return a payment to the originator under an ISO 20022 reason code, as a pacs.004. "
        "A payment blocked by sanctions screening can never be returned."
    ),
    args_model=ReturnArgs,
    category="payments",
    requires_approval=True,
    approval_risk="high",
    writes_data=True,
    idempotent=False,
)
async def issue_payment_return(args: ReturnArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    code = args.reason_code.strip().upper()
    if code not in RETURN_REASONS:
        raise ValidationError(
            f"'{code}' is not an ISO 20022 external return reason code",
            details={"valid": sorted(RETURN_REASONS)},
        )
    # Returning a blocked payment sends frozen funds back to the originator, which is a
    # sanctions breach in itself. This is the one refusal that has no override.
    if payment.screening_status == "blocked":
        raise ValidationError(
            "This payment is blocked by a confirmed sanctions match. Blocked funds are "
            "frozen and reported; they are never returned to the originator.",
            details={"payment": payment.payment_reference},
        )
    if payment.status == "returned":
        raise ValidationError("This payment has already been returned")

    # Under OUR the debtor bears every charge, so the full amount goes back. Under SHA or
    # BEN the correspondent's charges have already been taken and cannot be recovered.
    retained = 0.0 if payment.charge_bearer == "OUR" else round(payment.charges_deducted, 2)
    returned_amount = round(payment.amount - retained, 2)

    reference = f"RET-{uuid.uuid4().hex[:10].upper()}"
    ctx.session.add(
        PaymentReturn(
            return_reference=reference,
            payment_id=payment.id,
            reason_code=code,
            reason_description=RETURN_REASONS[code],
            message_type="pacs.004",
            returned_amount=returned_amount,
            charges_retained=retained,
            status="issued",
            approved_by=ctx.user_email,
            issued_at=datetime.now(UTC),
            execution_id=ctx.execution_id,
        )
    )
    payment.status = "returned"
    payment.status_reason = f"Returned under {code}"
    await ctx.session.flush()

    log.info("payment_returned", payment=payment.payment_reference, code=code, by=ctx.user_email)
    return {
        "return_reference": reference,
        "payment_reference": payment.payment_reference,
        "message_type": "pacs.004",
        "reason_code": code,
        "reason_description": RETURN_REASONS[code],
        "original_amount": round(payment.amount, 2),
        "charges_retained": retained,
        "returned_amount": returned_amount,
        "charge_bearer": payment.charge_bearer,
        "note": args.note[:500],
    }


class ReleaseArgs(BaseModel):
    reference: str = Field(description="Payment reference or UETR")
    justification: str = Field(description="Why the payment is now safe to release")


@tool(
    name="release_payment",
    description=(
        "Release a held payment for settlement. Refuses unless screening is clear and no "
        "hit is still pending — the check is made against the payment record, not against "
        "what the request asserts."
    ),
    args_model=ReleaseArgs,
    category="payments",
    requires_approval=True,
    approval_risk="critical",
    writes_data=True,
    idempotent=False,
)
async def release_payment(args: ReleaseArgs, ctx: ToolContext) -> dict[str, Any]:
    payment = await _load_payment(ctx, args.reference)
    pending = (
        await ctx.session.execute(
            select(func.count(PaymentScreeningHit.id)).where(
                PaymentScreeningHit.payment_id == payment.id,
                PaymentScreeningHit.disposition == "pending",
            )
        )
    ).scalar_one()

    if payment.screening_status == "blocked":
        raise ValidationError("Payment is blocked by a confirmed sanctions match and cannot be released")
    if payment.screening_status == "unscreened":
        raise ValidationError("Payment has not been screened since it was last amended; screen it first")
    if int(pending):
        raise ValidationError(
            f"{int(pending)} screening hit(s) are still pending disposition",
            details={"payment": payment.payment_reference},
        )
    if payment.status == "settled":
        return {
            "released": False,
            "reason": "already settled",
            "payment_reference": payment.payment_reference,
        }

    now = datetime.now(UTC)
    payment.status = "settled"
    payment.status_reason = "Released after screening cleared"
    payment.settled_at = now
    if payment.value_date is None:
        payment.value_date = now.date()
    await ctx.session.flush()

    log.info("payment_released", payment=payment.payment_reference, by=ctx.user_email)
    return {
        "released": True,
        "payment_reference": payment.payment_reference,
        "uetr": payment.uetr,
        "amount": round(payment.amount, 2),
        "currency": payment.currency,
        "settled_at": now.isoformat(),
        "value_date": payment.value_date.isoformat(),
        "justification": args.justification[:500],
        "released_by": ctx.user_email,
    }


# --- portfolio ----------------------------------------------------------------
class PaymentSummaryArgs(BaseModel):
    days: int = Field(default=7, ge=1, le=90, description="Look-back window in days")


@tool(
    name="payment_operations_summary",
    description=(
        "Operational picture of the payment queue: volumes and value by rail and status, "
        "held and blocked payments, open investigations and how many are past their "
        "regulatory deadline."
    ),
    args_model=PaymentSummaryArgs,
    category="payments",
)
async def payment_operations_summary(args: PaymentSummaryArgs, ctx: ToolContext) -> dict[str, Any]:
    now = datetime.now(UTC)
    since = now - timedelta(days=args.days)

    rows = (
        await ctx.session.execute(
            select(
                PaymentInstruction.rail,
                PaymentInstruction.status,
                func.count(PaymentInstruction.id),
                func.sum(PaymentInstruction.amount),
            )
            .where(PaymentInstruction.created_at >= since)
            .group_by(PaymentInstruction.rail, PaymentInstruction.status)
        )
    ).all()

    by_rail: dict[str, dict[str, Any]] = {}
    total_count = 0
    total_value = 0.0
    for rail, status, count, value in rows:
        bucket = by_rail.setdefault(rail, {"rail": rail, "count": 0, "value": 0.0, "by_status": {}})
        bucket["count"] += int(count or 0)
        bucket["value"] += float(value or 0.0)
        bucket["by_status"][status] = int(count or 0)
        total_count += int(count or 0)
        total_value += float(value or 0.0)

    held = (
        await ctx.session.execute(
            select(func.count(PaymentInstruction.id)).where(PaymentInstruction.screening_status == "hold")
        )
    ).scalar_one()
    blocked = (
        await ctx.session.execute(
            select(func.count(PaymentInstruction.id)).where(PaymentInstruction.screening_status == "blocked")
        )
    ).scalar_one()
    pending_hits = (
        await ctx.session.execute(
            select(func.count(PaymentScreeningHit.id)).where(PaymentScreeningHit.disposition == "pending")
        )
    ).scalar_one()

    open_cases = (
        (await ctx.session.execute(select(PaymentInvestigation).where(PaymentInvestigation.status == "open")))
        .scalars()
        .all()
    )
    breached = [c for c in open_cases if c.tat_deadline and c.tat_deadline < now]

    return {
        "window_days": args.days,
        "payments": total_count,
        "total_value": round(total_value, 2),
        "by_rail": [
            {**b, "value": round(b["value"], 2)}
            for b in sorted(by_rail.values(), key=lambda x: x["value"], reverse=True)
        ],
        "screening": {
            "on_hold": int(held),
            "blocked": int(blocked),
            "hits_pending_disposition": int(pending_hits),
        },
        "investigations": {
            "open": len(open_cases),
            "past_tat_deadline": len(breached),
            "compensation_accrued_inr": round(sum(c.compensation_due for c in open_cases), 2),
            "oldest_open_case": min(
                (c.case_number for c in open_cases if c.opened_at),
                default=None,
                key=lambda ref: ref,
            ),
        },
    }


class CustomerPaymentsArgs(BaseModel):
    customer_id: str = Field(description="Customer number, e.g. CUS-100001")
    days: int = Field(default=30, ge=1, le=365)


@tool(
    name="list_customer_payments",
    description="List a customer's recent payments with their status and any open case.",
    args_model=CustomerPaymentsArgs,
    category="payments",
)
async def list_customer_payments(args: CustomerPaymentsArgs, ctx: ToolContext) -> dict[str, Any]:
    customer = (
        (await ctx.session.execute(select(Customer).where(Customer.customer_number == args.customer_id)))
        .scalars()
        .first()
    )
    if customer is None:
        raise NotFoundError(f"Customer '{args.customer_id}' not found")
    since = datetime.now(UTC) - timedelta(days=args.days)
    payments = (
        (
            await ctx.session.execute(
                select(PaymentInstruction)
                .where(
                    PaymentInstruction.customer_id == customer.id,
                    PaymentInstruction.created_at >= since,
                )
                .order_by(PaymentInstruction.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "customer_id": args.customer_id,
        "window_days": args.days,
        "count": len(payments),
        "payments": [
            {
                "payment_reference": p.payment_reference,
                "uetr": p.uetr,
                "rail": p.rail,
                "direction": p.direction,
                "amount": round(p.amount, 2),
                "currency": p.currency,
                "creditor_name": p.creditor_name,
                "status": p.status,
                "screening_status": p.screening_status,
                "value_date": p.value_date.isoformat() if p.value_date else None,
            }
            for p in payments
        ],
    }
