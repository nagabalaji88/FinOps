"""Credit risk tools: underwriting, PD/LGD/EAD modelling, pricing and covenant monitoring.

The models here are the published ones, implemented rather than approximated:

* a **logistic scorecard** with points-to-double-the-odds scaling, the industry standard
  for retail origination — the score maps to a probability of default through
  ``score = offset + factor * ln(odds)``;
* **FOIR** (fixed obligation to income ratio), the RBI's affordability measure, computed
  from verified obligations plus the proposed instalment;
* the **Basel III IRB capital formula** for retail exposures, including the prescribed
  asset correlation, the 99.9% confidence level and the 12.5 multiplier;
* **risk-based pricing** built up from cost of funds, operating cost, expected loss and
  the capital charge, so the rate a customer is offered is explainable line by line.

Every input comes from the application, the bureau record or the customer's own ledger.
Nothing is assumed: a missing input is reported, not defaulted.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, timedelta
from statistics import NormalDist
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.errors import NotFoundError, ValidationError
from app.db.models.banking import (
    Account,
    BureauRecord,
    Card,
    CreditApplication,
    CreditDecision,
    Customer,
    Loan,
    Transaction,
)
from app.tools.base import ToolContext, tool

# --- model constants ----------------------------------------------------------
POLICY_VERSION = "CP-2026.1"
MODEL_VERSION = "PD-RETAIL-1.3"

#: Scorecard calibration. 600 points at 50:1 good:bad odds, doubling every 40 points.
BASE_SCORE = 600.0
BASE_ODDS = 50.0
PDO = 40.0
FACTOR = PDO / math.log(2)
OFFSET = BASE_SCORE - FACTOR * math.log(BASE_ODDS)

#: Weight of evidence coefficients, one per characteristic. Positive points reduce risk.
#: Each band is (inclusive_lower, exclusive_upper, points).
# fmt: off
# The scorecard is read as a table: each band is (lower, upper, points), and keeping the
# bands on one line per row is what lets a reviewer check the cutoffs against the policy
# document. One value per line would make that impossible.
SCORECARD: dict[str, dict[str, Any]] = {
    "bureau_score": {
        "label": "Bureau score",
        "bands": [(0, 300, -120), (300, 600, -80), (600, 650, -40), (650, 700, -5),
                  (700, 750, 25), (750, 800, 55), (800, 901, 80)],
        "missing_points": -60,
    },
    "worst_dpd_24m": {
        "label": "Worst delinquency in 24 months (days)",
        "bands": [(0, 1, 45), (1, 30, 5), (30, 60, -35), (60, 90, -70), (90, 10_000, -110)],
        "missing_points": -20,
    },
    "revolving_utilisation_pct": {
        "label": "Revolving utilisation",
        "bands": [(0, 10, 30), (10, 30, 20), (30, 50, 5), (50, 75, -20), (75, 90, -45),
                  (90, 10_000, -70)],
        "missing_points": -10,
    },
    "enquiries_6m": {
        "label": "Credit enquiries in 6 months",
        "bands": [(0, 1, 20), (1, 3, 10), (3, 6, -15), (6, 10, -35), (10, 10_000, -55)],
        "missing_points": 0,
    },
    "oldest_account_months": {
        "label": "Credit history length",
        "bands": [(0, 12, -35), (12, 36, -10), (36, 72, 15), (72, 120, 30), (120, 10_000, 40)],
        "missing_points": -25,
    },
    "employment_months": {
        "label": "Employment stability",
        "bands": [(0, 6, -40), (6, 12, -15), (12, 36, 10), (36, 84, 25), (84, 10_000, 35)],
        "missing_points": -15,
    },
    "foir_pct": {
        "label": "Fixed obligation to income ratio",
        "bands": [(0, 30, 40), (30, 40, 20), (40, 50, 0), (50, 60, -35), (60, 10_000, -75)],
        "missing_points": -30,
    },
    "write_offs": {
        "label": "Written-off accounts",
        "bands": [(0, 1, 20), (1, 2, -60), (2, 10_000, -120)],
        "missing_points": 0,
    },
}
# fmt: on

#: Segment adjustments applied after the scorecard, in points.
EMPLOYMENT_ADJUSTMENT = {
    "salaried": 10,
    "self_employed": -10,
    "professional": 5,
    "business": -5,
    "retired": -15,
    "student": -30,
}

#: Basel III IRB, retail "other" exposures.
IRB_CORRELATION_MIN = 0.03
IRB_CORRELATION_MAX = 0.16
IRB_K_FACTOR = 35.0
IRB_CONFIDENCE = 0.999
IRB_CAPITAL_MULTIPLIER = 12.5

#: Foundation-IRB unsecured senior LGD, and the recovery assumptions that move it.
LGD_UNSECURED = 0.45
LGD_FLOOR = 0.05
COLLATERAL_HAIRCUT = {
    "property": 0.25,
    "residential_property": 0.20,
    "commercial_property": 0.35,
    "gold": 0.10,
    "fixed_deposit": 0.02,
    "vehicle": 0.40,
    "securities": 0.25,
}

#: Pricing build-up, annualised percentages.
COST_OF_FUNDS_PCT = 6.85  # marginal cost of funds based lending rate reference
OPERATING_COST_PCT = 1.40
TARGET_RETURN_ON_CAPITAL = 0.15
MIN_RATE_PCT = 8.50
MAX_RATE_PCT = 28.00

#: Hard policy rules. A knockout is never overridden by a good score.
MIN_AGE = 21
MAX_AGE_AT_MATURITY = 70
MIN_BUREAU_SCORE = 620
MAX_FOIR_PCT = 60.0
MAX_UNSECURED_EXPOSURE = 5_000_000.0
MAX_TENURE_MONTHS = {
    "personal_loan": 84,
    "auto_loan": 84,
    "home_loan": 360,
    "business_loan": 120,
    "credit_card": 0,
    "gold_loan": 36,
}

_NORMAL = NormalDist()


# --- shared maths -------------------------------------------------------------
def emi(principal: float, annual_rate_pct: float, months: int) -> float:
    """Standard amortising instalment. Zero-rate loans divide evenly."""
    if months <= 0:
        raise ValidationError("Tenure must be at least one month")
    if annual_rate_pct <= 0:
        return round(principal / months, 2)
    r = annual_rate_pct / 100.0 / 12.0
    growth = (1 + r) ** months
    return round(principal * r * growth / (growth - 1), 2)


def score_to_pd(score: float) -> float:
    """Invert the scorecard scaling to a probability of default."""
    odds = math.exp((score - OFFSET) / FACTOR)
    return min(0.9999, max(0.0001, 1.0 / (1.0 + odds)))


def _band_points(characteristic: str, value: float | None) -> tuple[float, str]:
    spec = SCORECARD[characteristic]
    if value is None:
        return float(spec["missing_points"]), "missing"
    for lower, upper, points in spec["bands"]:
        if lower <= value < upper:
            return float(points), f"[{lower}, {upper})"
    return float(spec["missing_points"]), "out_of_range"


def basel_irb_capital(pd: float, lgd: float, exposure: float) -> dict[str, float]:
    """Basel III IRB capital requirement for a retail exposure.

    K = LGD * [ N( (N^-1(PD) + sqrt(R) * N^-1(0.999)) / sqrt(1-R) ) - PD ]
    R = 0.03 * (1-e^-35PD)/(1-e^-35) + 0.16 * [1 - (1-e^-35PD)/(1-e^-35)]
    """
    pd = min(max(pd, 0.0003), 0.9999)  # the 0.03% regulatory PD floor
    decay = (1 - math.exp(-IRB_K_FACTOR * pd)) / (1 - math.exp(-IRB_K_FACTOR))
    correlation = IRB_CORRELATION_MIN * decay + IRB_CORRELATION_MAX * (1 - decay)
    conditional = _NORMAL.cdf(
        (_NORMAL.inv_cdf(pd) + math.sqrt(correlation) * _NORMAL.inv_cdf(IRB_CONFIDENCE))
        / math.sqrt(1 - correlation)
    )
    capital_requirement = lgd * (conditional - pd)
    capital_requirement = max(capital_requirement, 0.0)
    return {
        "asset_correlation": round(correlation, 6),
        "conditional_default_rate": round(conditional, 6),
        "capital_requirement_k": round(capital_requirement, 6),
        "capital_charge": round(capital_requirement * exposure, 2),
        "risk_weighted_assets": round(capital_requirement * IRB_CAPITAL_MULTIPLIER * exposure, 2),
        "risk_weight_pct": round(capital_requirement * IRB_CAPITAL_MULTIPLIER * 100, 2),
    }


def grade_for_pd(pd: float) -> str:
    """Master rating scale. Boundaries are the midpoints of the usual PD bands."""
    for grade, ceiling in (
        ("AAA", 0.0010),
        ("AA", 0.0025),
        ("A", 0.0060),
        ("BBB", 0.0150),
        ("BB", 0.0400),
        ("B", 0.0900),
        ("CCC", 0.1800),
        ("CC", 0.3000),
    ):
        if pd <= ceiling:
            return grade
    return "C"


# --- helpers ------------------------------------------------------------------
async def _application(ctx: ToolContext, reference: str) -> CreditApplication:
    stmt = select(CreditApplication).where(
        (CreditApplication.application_number == reference) | (CreditApplication.id == reference)
    )
    application = (await ctx.session.execute(stmt)).scalar_one_or_none()
    if application is None:
        raise NotFoundError(f"Credit application '{reference}' not found")
    return application


async def _customer(ctx: ToolContext, customer_id: str) -> Customer:
    customer = (
        await ctx.session.execute(select(Customer).where(Customer.id == customer_id))
    ).scalar_one_or_none()
    if customer is None:
        raise NotFoundError("Customer not found for this application")
    return customer


def _age(dob: date | None) -> int | None:
    if dob is None:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


# --- tools --------------------------------------------------------------------
class ApplicationArgs(BaseModel):
    application: str = Field(description="Application number or id")


@tool(
    "get_credit_application",
    "Fetch a credit application with the applicant's profile and existing exposure.",
    ApplicationArgs,
    category="credit",
)
async def get_credit_application(args: ApplicationArgs, ctx: ToolContext) -> dict[str, Any]:
    application = await _application(ctx, args.application)
    customer = await _customer(ctx, application.customer_id)

    loans = (
        (
            await ctx.session.execute(
                select(Loan).where(Loan.customer_id == customer.id, Loan.status == "active")
            )
        )
        .scalars()
        .all()
    )
    cards = (
        (
            await ctx.session.execute(
                select(Card).where(Card.customer_id == customer.id, Card.status == "active")
            )
        )
        .scalars()
        .all()
    )

    existing_emi = round(sum(loan.emi_amount for loan in loans), 2)
    card_minimums = round(sum(card.minimum_due for card in cards), 2)
    return {
        "application": {
            "number": application.application_number,
            "product": application.product,
            "requested_amount": application.requested_amount,
            "currency": application.currency,
            "tenure_months": application.tenure_months,
            "purpose": application.purpose,
            "channel": application.channel,
            "status": application.status,
            "declared_monthly_income": application.declared_monthly_income,
            "declared_monthly_expenses": application.declared_monthly_expenses,
            "employment_type": application.employment_type,
            "employment_months": application.employment_months,
            "collateral_type": application.collateral_type,
            "collateral_value": application.collateral_value,
            "submitted_at": application.submitted_at.isoformat() if application.submitted_at else None,
        },
        "applicant": {
            "customer_number": customer.customer_number,
            "full_name": customer.full_name,
            "age": _age(customer.date_of_birth),
            "segment": customer.segment,
            "kyc_status": customer.kyc_status,
            "risk_rating": customer.risk_rating,
            "relationship_since": customer.onboarded_at.date().isoformat() if customer.onboarded_at else None,
        },
        "existing_exposure": {
            "active_loans": len(loans),
            "loan_outstanding": round(sum(loan.outstanding for loan in loans), 2),
            "monthly_emi": existing_emi,
            "active_cards": len(cards),
            "card_limit": round(sum(card.credit_limit for card in cards), 2),
            "card_balance": round(sum(card.current_balance for card in cards), 2),
            "card_minimum_due": card_minimums,
            "total_monthly_obligation": round(existing_emi + card_minimums, 2),
        },
    }


class BureauArgs(BaseModel):
    customer_id: str = Field(description="Customer id")
    bureau: str = Field(default="CIBIL", description="Bureau to read")
    max_age_days: int = Field(default=30, ge=1, le=365, description="Reject a pull older than this")


@tool(
    "pull_credit_bureau",
    "Read the most recent credit bureau record for the applicant.",
    BureauArgs,
    category="credit",
)
async def pull_credit_bureau(args: BureauArgs, ctx: ToolContext) -> dict[str, Any]:
    record = (
        await ctx.session.execute(
            select(BureauRecord)
            .where(BureauRecord.customer_id == args.customer_id, BureauRecord.bureau == args.bureau)
            .order_by(BureauRecord.pulled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if record is None:
        return {
            "available": False,
            "bureau": args.bureau,
            "reason": "no bureau record on file for this customer",
            "action": "a fresh pull is required before a decision can be made",
        }

    pulled_at = record.pulled_at
    if pulled_at.tzinfo is None:
        pulled_at = pulled_at.replace(tzinfo=UTC)
    age_days = (datetime.now(UTC) - pulled_at).days
    return {
        "available": True,
        "bureau": record.bureau,
        "score": record.score,
        "scale": f"{record.score_scale_min}-{record.score_scale_max}",
        "pulled_at": pulled_at.isoformat(),
        "age_days": age_days,
        "stale": age_days > args.max_age_days,
        "reference": record.reference,
        "source": record.source,
        "accounts_total": record.accounts_total,
        "accounts_open": record.accounts_open,
        "accounts_delinquent": record.accounts_delinquent,
        "worst_dpd_24m": record.worst_dpd_24m,
        "enquiries_6m": record.enquiries_6m,
        "oldest_account_months": record.oldest_account_months,
        "total_outstanding": record.total_outstanding,
        "total_sanctioned": record.total_sanctioned,
        "revolving_utilisation_pct": record.revolving_utilisation_pct,
        "monthly_obligations": record.monthly_obligations,
        "write_offs": record.write_offs,
        "settled_accounts": record.settled_accounts,
    }


async def _verified_monthly_income(ctx: ToolContext, customer: Customer) -> tuple[float | None, dict[str, Any]]:
    """Monthly income evidenced by salary credits, and how it was established.

    Returns `None` when the ledger cannot evidence an income, so the caller can decide
    whether to fall back to the declared figure or treat the income as unknown.
    """
    since = datetime.now(UTC) - timedelta(days=180)
    account_ids = (
        (await ctx.session.execute(select(Account.id).where(Account.customer_id == customer.id)))
        .scalars()
        .all()
    )
    if not account_ids:
        return None, {"method": "declared", "reason": "no accounts held with the bank"}

    credits = (
        (
            await ctx.session.execute(
                select(Transaction).where(
                    Transaction.account_id.in_(account_ids),
                    Transaction.booked_at >= since,
                    Transaction.direction == "credit",
                )
            )
        )
        .scalars()
        .all()
    )
    salary = [
        t
        for t in credits
        if (t.category or "").lower() in {"salary", "income"} or "salary" in (t.description or "").lower()
    ]
    if not salary:
        return None, {"method": "declared", "reason": "no salary credits identified"}

    months = max(1, len({(t.booked_at.year, t.booked_at.month) for t in salary}))
    return round(sum(abs(t.amount) for t in salary) / months, 2), {
        "method": "salary_credits",
        "credits_found": len(salary),
        "months_observed": months,
        "window_days": 180,
    }


async def _existing_monthly_obligations(ctx: ToolContext, customer: Customer) -> float:
    """Committed monthly outgo: instalments on live loans plus card minimum dues."""
    loans = (
        (
            await ctx.session.execute(
                select(Loan).where(Loan.customer_id == customer.id, Loan.status == "active")
            )
        )
        .scalars()
        .all()
    )
    cards = (
        (
            await ctx.session.execute(
                select(Card).where(Card.customer_id == customer.id, Card.status == "active")
            )
        )
        .scalars()
        .all()
    )
    return round(sum(loan.emi_amount for loan in loans) + sum(card.minimum_due for card in cards), 2)


class AffordabilityArgs(BaseModel):
    application: str = Field(description="Application number or id")
    proposed_rate_pct: float = Field(
        default=0.0,
        ge=0,
        le=60,
        description="Rate to price the instalment at; 0 uses the indicative product rate",
    )
    verify_income_from_ledger: bool = Field(
        default=True, description="Cross-check declared income against salary credits"
    )


@tool(
    "assess_affordability",
    "Compute FOIR and disposable income for the requested facility, verifying declared "
    "income against the customer's own salary credits.",
    AffordabilityArgs,
    category="credit",
)
async def assess_affordability(args: AffordabilityArgs, ctx: ToolContext) -> dict[str, Any]:
    application = await _application(ctx, args.application)
    customer = await _customer(ctx, application.customer_id)

    verified_income, evidence = (
        await _verified_monthly_income(ctx, customer)
        if args.verify_income_from_ledger
        else (None, {"method": "declared"})
    )

    declared = application.declared_monthly_income
    income = verified_income if verified_income is not None else declared
    if income <= 0:
        raise ValidationError("No income available: neither declared nor verifiable")

    existing_obligations = await _existing_monthly_obligations(ctx, customer)

    rate = args.proposed_rate_pct or _indicative_rate(application.product)
    proposed_emi = emi(application.requested_amount, rate, application.tenure_months)

    foir = (existing_obligations + proposed_emi) / income * 100
    disposable = income - application.declared_monthly_expenses - existing_obligations - proposed_emi

    # The affordable instalment is what is left inside the FOIR cap.
    headroom = max(0.0, income * MAX_FOIR_PCT / 100 - existing_obligations)

    return {
        "income": {
            "declared_monthly": declared,
            "verified_monthly": verified_income,
            "used_for_assessment": round(income, 2),
            "variance_pct": round((declared - verified_income) / verified_income * 100, 2)
            if verified_income
            else None,
            "evidence": evidence,
        },
        "obligations": {
            "existing_monthly": existing_obligations,
            "declared_expenses": application.declared_monthly_expenses,
            "proposed_emi": proposed_emi,
            "priced_at_rate_pct": rate,
        },
        "foir_pct": round(foir, 2),
        "foir_cap_pct": MAX_FOIR_PCT,
        "within_cap": foir <= MAX_FOIR_PCT,
        "disposable_income": round(disposable, 2),
        "max_affordable_emi": round(headroom, 2),
        "max_affordable_principal": round(_principal_for_emi(headroom, rate, application.tenure_months), 2),
    }


def _indicative_rate(product: str) -> float:
    return {
        "personal_loan": 14.5,
        "auto_loan": 9.75,
        "home_loan": 8.65,
        "business_loan": 15.5,
        "gold_loan": 11.0,
        "credit_card": 36.0,
    }.get(product, 14.5)


def _principal_for_emi(instalment: float, annual_rate_pct: float, months: int) -> float:
    """Invert the amortisation formula: the largest principal that instalment supports."""
    if instalment <= 0 or months <= 0:
        return 0.0
    if annual_rate_pct <= 0:
        return instalment * months
    r = annual_rate_pct / 100.0 / 12.0
    growth = (1 + r) ** months
    return instalment * (growth - 1) / (r * growth)


class ScoreArgs(BaseModel):
    application: str = Field(description="Application number or id")
    foir_pct: float | None = Field(
        default=None,
        description="FOIR from assess_affordability; omit to skip the affordability characteristic",
    )


@tool(
    "score_credit_risk",
    "Run the origination scorecard and convert the score to a probability of default.",
    ScoreArgs,
    category="credit",
)
async def score_credit_risk(args: ScoreArgs, ctx: ToolContext) -> dict[str, Any]:
    application = await _application(ctx, args.application)
    bureau = (
        await ctx.session.execute(
            select(BureauRecord)
            .where(BureauRecord.customer_id == application.customer_id)
            .order_by(BureauRecord.pulled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    values: dict[str, float | None] = {
        "bureau_score": float(bureau.score) if bureau else None,
        "worst_dpd_24m": float(bureau.worst_dpd_24m) if bureau else None,
        "revolving_utilisation_pct": bureau.revolving_utilisation_pct if bureau else None,
        "enquiries_6m": float(bureau.enquiries_6m) if bureau else None,
        "oldest_account_months": float(bureau.oldest_account_months) if bureau else None,
        "employment_months": float(application.employment_months),
        "foir_pct": args.foir_pct,
        "write_offs": float(bureau.write_offs) if bureau else None,
    }

    contributions: list[dict[str, Any]] = []
    total_points = 0.0
    for characteristic, value in values.items():
        points, band = _band_points(characteristic, value)
        total_points += points
        contributions.append(
            {
                "characteristic": characteristic,
                "label": SCORECARD[characteristic]["label"],
                "value": value,
                "band": band,
                "points": points,
            }
        )

    adjustment = EMPLOYMENT_ADJUSTMENT.get(application.employment_type, 0)
    contributions.append(
        {
            "characteristic": "employment_type",
            "label": "Employment type",
            "value": application.employment_type,
            "band": "segment",
            "points": float(adjustment),
        }
    )

    score = BASE_SCORE + total_points + adjustment
    pd = score_to_pd(score)
    return {
        "score": round(score, 1),
        "base_score": BASE_SCORE,
        "scaling": {
            "base_odds": BASE_ODDS,
            "points_to_double_odds": PDO,
            "factor": round(FACTOR, 4),
            "offset": round(OFFSET, 4),
        },
        "probability_of_default": round(pd, 6),
        "probability_of_default_pct": round(pd * 100, 3),
        "risk_grade": grade_for_pd(pd),
        "contributions": sorted(contributions, key=lambda c: c["points"]),
        "missing_characteristics": [c["characteristic"] for c in contributions if c["band"] == "missing"],
        "bureau_available": bureau is not None,
        "model_version": MODEL_VERSION,
    }


class LgdArgs(BaseModel):
    application: str = Field(description="Application number or id")
    exposure: float | None = Field(
        default=None, description="Exposure at default; defaults to the requested amount"
    )


@tool(
    "estimate_loss_given_default",
    "Estimate LGD from collateral cover after regulatory haircuts.",
    LgdArgs,
    category="credit",
)
async def estimate_loss_given_default(args: LgdArgs, ctx: ToolContext) -> dict[str, Any]:
    application = await _application(ctx, args.application)
    exposure = args.exposure if args.exposure is not None else application.requested_amount
    if exposure <= 0:
        raise ValidationError("Exposure must be positive")

    collateral_type = (application.collateral_type or "").lower().replace(" ", "_")
    haircut = COLLATERAL_HAIRCUT.get(collateral_type)
    if not collateral_type or application.collateral_value <= 0:
        lgd = LGD_UNSECURED
        detail = {"secured": False, "basis": "foundation IRB senior unsecured"}
    else:
        if haircut is None:
            haircut = 0.50  # unrecognised collateral gets the most conservative haircut
        realisable = application.collateral_value * (1 - haircut)
        cover = min(1.0, realisable / exposure)
        lgd = max(LGD_FLOOR, LGD_UNSECURED * (1 - cover))
        detail = {
            "secured": True,
            "collateral_type": collateral_type,
            "collateral_value": application.collateral_value,
            "haircut_pct": round(haircut * 100, 2),
            "realisable_value": round(realisable, 2),
            "coverage_ratio": round(cover, 4),
            "loan_to_value_pct": round(exposure / application.collateral_value * 100, 2),
        }

    return {
        "loss_given_default": round(lgd, 4),
        "loss_given_default_pct": round(lgd * 100, 2),
        "exposure_at_default": round(exposure, 2),
        "floor_applied": lgd <= LGD_FLOOR + 1e-9,
        **detail,
    }


class ExpectedLossArgs(BaseModel):
    probability_of_default: float = Field(ge=0, le=1)
    loss_given_default: float = Field(ge=0, le=1)
    exposure_at_default: float = Field(gt=0)


@tool(
    "calculate_expected_loss",
    "Expected loss and Basel III IRB capital for a retail exposure.",
    ExpectedLossArgs,
    category="credit",
)
async def calculate_expected_loss(args: ExpectedLossArgs, ctx: ToolContext) -> dict[str, Any]:
    expected_loss = args.probability_of_default * args.loss_given_default * args.exposure_at_default
    capital = basel_irb_capital(
        args.probability_of_default, args.loss_given_default, args.exposure_at_default
    )
    return {
        "expected_loss": round(expected_loss, 2),
        "expected_loss_pct_of_exposure": round(expected_loss / args.exposure_at_default * 100, 4),
        "inputs": {
            "probability_of_default": args.probability_of_default,
            "loss_given_default": args.loss_given_default,
            "exposure_at_default": args.exposure_at_default,
        },
        "basel_irb": capital,
        "framework": "Basel III IRB, retail other exposures, 99.9% confidence",
    }


class PricingArgs(BaseModel):
    application: str = Field(description="Application number or id")
    probability_of_default: float = Field(ge=0, le=1)
    loss_given_default: float = Field(ge=0, le=1)
    exposure: float | None = Field(default=None)


@tool(
    "price_facility",
    "Build the risk-based rate from cost of funds, operating cost, expected loss and the capital charge.",
    PricingArgs,
    category="credit",
)
async def price_facility(args: PricingArgs, ctx: ToolContext) -> dict[str, Any]:
    application = await _application(ctx, args.application)
    exposure = args.exposure if args.exposure is not None else application.requested_amount
    if exposure <= 0:
        raise ValidationError("Exposure must be positive")

    expected_loss_pct = args.probability_of_default * args.loss_given_default * 100
    capital = basel_irb_capital(args.probability_of_default, args.loss_given_default, exposure)
    capital_charge_pct = capital["capital_requirement_k"] * TARGET_RETURN_ON_CAPITAL * 100

    indicative = _indicative_rate(application.product)
    build_up = {
        "cost_of_funds_pct": COST_OF_FUNDS_PCT,
        "operating_cost_pct": OPERATING_COST_PCT,
        "expected_loss_pct": round(expected_loss_pct, 4),
        "capital_charge_pct": round(capital_charge_pct, 4),
    }
    raw_rate = sum(build_up.values())
    rate = min(MAX_RATE_PCT, max(MIN_RATE_PCT, raw_rate))

    return {
        "recommended_rate_pct": round(rate, 2),
        "unclamped_rate_pct": round(raw_rate, 4),
        "floor_pct": MIN_RATE_PCT,
        "ceiling_pct": MAX_RATE_PCT,
        "build_up": build_up,
        "indicative_product_rate_pct": indicative,
        "premium_to_indicative_pct": round(rate - indicative, 2),
        "instalment_at_recommended_rate": emi(exposure, rate, application.tenure_months),
        "target_return_on_capital": TARGET_RETURN_ON_CAPITAL,
    }


class PolicyArgs(BaseModel):
    application: str = Field(description="Application number or id")
    bureau_score: int | None = Field(
        default=None, description="Overrides the stored bureau score; normally omitted"
    )
    foir_pct: float | None = Field(default=None, description="FOIR from assess_affordability")


@tool(
    "check_credit_policy",
    "Apply the hard credit policy rules. A knockout is never overridden by a good score.",
    PolicyArgs,
    category="credit",
)
async def check_credit_policy(args: PolicyArgs, ctx: ToolContext) -> dict[str, Any]:
    application = await _application(ctx, args.application)
    customer = await _customer(ctx, application.customer_id)
    checks: list[dict[str, Any]] = []

    def record(rule: str, passed: bool, detail: str, knockout: bool = True) -> None:
        checks.append({"rule": rule, "passed": passed, "detail": detail, "knockout": knockout})

    age = _age(customer.date_of_birth)
    record("minimum_age", age is not None and age >= MIN_AGE, f"applicant age {age}, minimum {MIN_AGE}")
    if age is not None:
        age_at_maturity = age + math.ceil(application.tenure_months / 12)
        record(
            "age_at_maturity",
            age_at_maturity <= MAX_AGE_AT_MATURITY,
            f"{age_at_maturity} at maturity, maximum {MAX_AGE_AT_MATURITY}",
        )

    record("kyc_verified", customer.kyc_status == "verified", f"KYC status is '{customer.kyc_status}'")
    record(
        "not_sanctioned",
        not customer.sanctions_flag,
        "sanctions flag set on the customer" if customer.sanctions_flag else "no sanctions flag",
    )

    max_tenure = MAX_TENURE_MONTHS.get(application.product, 84)
    record(
        "maximum_tenure",
        application.tenure_months <= max_tenure,
        f"{application.tenure_months} months requested, maximum {max_tenure} for {application.product}",
    )

    # Read the bureau rather than trusting the caller to pass it: a policy check that
    # silently fails because an argument was omitted is worse than no check.
    score = args.bureau_score
    if score is None:
        stored = (
            await ctx.session.execute(
                select(BureauRecord)
                .where(BureauRecord.customer_id == customer.id)
                .order_by(BureauRecord.pulled_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        score = stored.score if stored else None
    if score is not None:
        record(
            "minimum_bureau_score",
            score >= MIN_BUREAU_SCORE,
            f"bureau score {score}, minimum {MIN_BUREAU_SCORE}",
        )
    else:
        record("minimum_bureau_score", False, "no bureau record on file; a current pull is required")

    # Same principle as the bureau score above: compute the ratio rather than knock the
    # application out because an optional argument was left off. An omitted argument is not
    # evidence of unaffordability, and treating it as one declined every application whose
    # caller happened to skip `assess_affordability` first.
    foir = args.foir_pct
    basis = "supplied by the caller"
    if foir is None:
        income, _ = await _verified_monthly_income(ctx, customer)
        if income is None:
            income = application.declared_monthly_income
            basis = "computed from declared income at the indicative product rate"
        else:
            basis = "computed from salary credits at the indicative product rate"
        if income > 0:
            obligations = await _existing_monthly_obligations(ctx, customer)
            proposed = emi(
                application.requested_amount,
                _indicative_rate(application.product),
                application.tenure_months,
            )
            foir = (obligations + proposed) / income * 100
    if foir is not None:
        record(
            "maximum_foir",
            foir <= MAX_FOIR_PCT,
            f"FOIR {foir:.2f}%, cap {MAX_FOIR_PCT}% ({basis})",
        )
    else:
        record("maximum_foir", False, "no income declared or evidenced, so FOIR cannot be established")

    loans = (
        await ctx.session.execute(
            select(func.coalesce(func.sum(Loan.outstanding), 0.0)).where(
                Loan.customer_id == customer.id, Loan.status == "active"
            )
        )
    ).scalar_one()
    unsecured = float(loans) + (application.requested_amount if not application.collateral_type else 0.0)
    record(
        "unsecured_exposure_cap",
        unsecured <= MAX_UNSECURED_EXPOSURE,
        f"unsecured exposure would be {unsecured:,.0f}, cap {MAX_UNSECURED_EXPOSURE:,.0f}",
    )

    failed = [c for c in checks if not c["passed"] and c["knockout"]]
    return {
        "policy_version": POLICY_VERSION,
        "checks": checks,
        "passed": not failed,
        "knockouts": [c["rule"] for c in failed],
        "decision_hint": "decline" if failed else "continue",
    }


class LimitArgs(BaseModel):
    application: str = Field(description="Application number or id")
    max_affordable_principal: float = Field(ge=0)
    risk_grade: str = Field(description="Grade from score_credit_risk")


@tool(
    "recommend_limit",
    "Recommend the sanctioned amount from affordability, grade caps and the request.",
    LimitArgs,
    category="credit",
)
async def recommend_limit(args: LimitArgs, ctx: ToolContext) -> dict[str, Any]:
    application = await _application(ctx, args.application)
    grade_cap_multiple = {
        "AAA": 24,
        "AA": 20,
        "A": 18,
        "BBB": 15,
        "BB": 12,
        "B": 8,
        "CCC": 4,
        "CC": 2,
        "C": 0,
    }.get(args.risk_grade.upper(), 6)
    income_cap = application.declared_monthly_income * grade_cap_multiple

    candidates = {
        "requested": application.requested_amount,
        "affordability_cap": round(args.max_affordable_principal, 2),
        "grade_income_multiple_cap": round(income_cap, 2),
    }
    if application.collateral_value > 0:
        ltv = {"home_loan": 0.80, "auto_loan": 0.85, "gold_loan": 0.75}.get(application.product, 0.70)
        candidates["collateral_ltv_cap"] = round(application.collateral_value * ltv, 2)

    recommended = round(min(candidates.values()), 2)
    binding = min(candidates, key=lambda key: candidates[key])
    return {
        "recommended_amount": recommended,
        "requested_amount": application.requested_amount,
        "shortfall": round(max(0.0, application.requested_amount - recommended), 2),
        "binding_constraint": binding,
        "candidates": candidates,
        "grade_income_multiple": grade_cap_multiple,
    }


class CovenantArgs(BaseModel):
    customer_id: str = Field(description="Customer id")
    lookback_days: int = Field(default=180, ge=30, le=730)


@tool(
    "evaluate_covenants",
    "Check post-disbursement covenants on the customer's existing facilities.",
    CovenantArgs,
    category="credit",
)
async def evaluate_covenants(args: CovenantArgs, ctx: ToolContext) -> dict[str, Any]:
    loans = (
        (await ctx.session.execute(select(Loan).where(Loan.customer_id == args.customer_id))).scalars().all()
    )
    if not loans:
        return {"facilities": 0, "breaches": [], "status": "no facilities to monitor"}

    since = datetime.now(UTC) - timedelta(days=args.lookback_days)
    account_ids = (
        (await ctx.session.execute(select(Account.id).where(Account.customer_id == args.customer_id)))
        .scalars()
        .all()
    )
    credits = 0.0
    if account_ids:
        credits = float(
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
    months = max(1, args.lookback_days // 30)
    average_monthly_inflow = credits / months
    total_emi = sum(loan.emi_amount for loan in loans)

    breaches: list[dict[str, Any]] = []
    for loan in loans:
        if loan.days_past_due > 0:
            breaches.append(
                {
                    "facility": loan.loan_number,
                    "covenant": "payment_discipline",
                    "severity": "high" if loan.days_past_due >= 30 else "medium",
                    "detail": f"{loan.days_past_due} days past due",
                }
            )
    coverage = average_monthly_inflow / total_emi if total_emi else None
    if coverage is not None and coverage < 1.5:
        breaches.append(
            {
                "facility": "portfolio",
                "covenant": "debt_service_coverage",
                "severity": "high" if coverage < 1.2 else "medium",
                "detail": f"inflow/EMI coverage {coverage:.2f}x, covenant 1.50x",
            }
        )

    return {
        "facilities": len(loans),
        "total_outstanding": round(sum(loan.outstanding for loan in loans), 2),
        "total_monthly_emi": round(total_emi, 2),
        "average_monthly_inflow": round(average_monthly_inflow, 2),
        "debt_service_coverage": round(coverage, 2) if coverage is not None else None,
        "breaches": breaches,
        "status": "breached" if breaches else "compliant",
        "window_days": args.lookback_days,
    }


class DecisionArgs(BaseModel):
    application: str = Field(description="Application number or id")
    decision: str = Field(description="approve | decline | refer")
    approved_amount: float = Field(default=0.0, ge=0)
    approved_tenure_months: int = Field(default=0, ge=0)
    approved_rate_pct: float = Field(default=0.0, ge=0, le=60)
    probability_of_default: float = Field(default=0.0, ge=0, le=1)
    loss_given_default: float = Field(default=0.0, ge=0, le=1)
    risk_grade: str | None = Field(default=None)
    foir_pct: float = Field(default=0.0, ge=0)
    reason_codes: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    scorecard: dict[str, Any] = Field(default_factory=dict)


@tool(
    "record_credit_decision",
    "Record the underwriting decision against the application. Requires human approval.",
    DecisionArgs,
    category="credit",
    writes_data=True,
    requires_approval=True,
    approval_risk="high",
    idempotent=False,
)
async def record_credit_decision(args: DecisionArgs, ctx: ToolContext) -> dict[str, Any]:
    if args.decision not in {"approve", "decline", "refer"}:
        raise ValidationError("decision must be approve, decline or refer")
    application = await _application(ctx, args.application)
    if args.decision == "approve" and args.approved_amount <= 0:
        raise ValidationError("An approval must carry a sanctioned amount")
    if args.decision == "decline" and not args.reason_codes:
        raise ValidationError("A decline must carry at least one reason code")

    exposure = args.approved_amount or application.requested_amount
    expected_loss = args.probability_of_default * args.loss_given_default * exposure
    capital = basel_irb_capital(args.probability_of_default, args.loss_given_default, exposure)

    decision = CreditDecision(
        application_id=application.id,
        customer_id=application.customer_id,
        decision=args.decision,
        approved_amount=args.approved_amount,
        approved_tenure_months=args.approved_tenure_months or application.tenure_months,
        approved_rate_pct=args.approved_rate_pct,
        risk_grade=args.risk_grade,
        probability_of_default=args.probability_of_default,
        loss_given_default=args.loss_given_default,
        exposure_at_default=exposure,
        expected_loss=round(expected_loss, 2),
        risk_weighted_assets=capital["risk_weighted_assets"],
        foir_pct=args.foir_pct,
        reason_codes=args.reason_codes,
        conditions=args.conditions,
        policy_version=POLICY_VERSION,
        model_version=MODEL_VERSION,
        scorecard=args.scorecard,
        decided_by=ctx.user_email,
        execution_id=ctx.execution_id,
    )
    ctx.session.add(decision)
    application.status = {"approve": "approved", "decline": "declined", "refer": "referred"}[args.decision]
    await ctx.session.flush()

    return {
        "decision_id": decision.id,
        "application": application.application_number,
        "decision": args.decision,
        "application_status": application.status,
        "approved_amount": args.approved_amount,
        "approved_rate_pct": args.approved_rate_pct,
        "instalment": emi(args.approved_amount, args.approved_rate_pct, decision.approved_tenure_months)
        if args.decision == "approve" and args.approved_amount
        else 0.0,
        "risk_grade": args.risk_grade,
        "expected_loss": decision.expected_loss,
        "risk_weighted_assets": decision.risk_weighted_assets,
        "reason_codes": args.reason_codes,
        "conditions": args.conditions,
        "policy_version": POLICY_VERSION,
        "model_version": MODEL_VERSION,
        "decided_by": ctx.user_email,
    }


class PortfolioArgs(BaseModel):
    product: str | None = Field(default=None, description="Restrict to one product")
    days: int = Field(default=180, ge=1, le=1095)


@tool(
    "summarise_credit_portfolio",
    "Aggregate decisions and outstanding exposure into a portfolio risk view.",
    PortfolioArgs,
    category="credit",
)
async def summarise_credit_portfolio(args: PortfolioArgs, ctx: ToolContext) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=args.days)
    stmt = select(CreditDecision).where(CreditDecision.created_at >= since)
    decisions = (await ctx.session.execute(stmt)).scalars().all()

    loans_stmt = select(Loan).where(Loan.status == "active")
    loans = (await ctx.session.execute(loans_stmt)).scalars().all()
    if args.product:
        loans = [loan for loan in loans if loan.loan_type == args.product]

    by_grade: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        grade = decision.risk_grade or "unrated"
        bucket = by_grade.setdefault(grade, {"count": 0, "exposure": 0.0, "expected_loss": 0.0, "rwa": 0.0})
        bucket["count"] += 1
        bucket["exposure"] += decision.exposure_at_default
        bucket["expected_loss"] += decision.expected_loss
        bucket["rwa"] += decision.risk_weighted_assets

    outstanding = sum(loan.outstanding for loan in loans)
    npa = [loan for loan in loans if loan.days_past_due >= 90]
    return {
        "window_days": args.days,
        "decisions": {
            "total": len(decisions),
            "approved": sum(1 for d in decisions if d.decision == "approve"),
            "declined": sum(1 for d in decisions if d.decision == "decline"),
            "referred": sum(1 for d in decisions if d.decision == "refer"),
            "approval_rate_pct": round(
                sum(1 for d in decisions if d.decision == "approve") / len(decisions) * 100, 2
            )
            if decisions
            else 0.0,
        },
        "by_grade": {
            grade: {
                "count": data["count"],
                "exposure": round(data["exposure"], 2),
                "expected_loss": round(data["expected_loss"], 2),
                "risk_weighted_assets": round(data["rwa"], 2),
            }
            for grade, data in sorted(by_grade.items())
        },
        "book": {
            "active_loans": len(loans),
            "outstanding": round(outstanding, 2),
            "npa_accounts": len(npa),
            "npa_outstanding": round(sum(loan.outstanding for loan in npa), 2),
            "gross_npa_pct": round(sum(loan.outstanding for loan in npa) / outstanding * 100, 2)
            if outstanding
            else 0.0,
        },
    }


def _reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"
