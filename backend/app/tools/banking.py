"""Customer service tools operating on the core banking tables."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.core.errors import NotFoundError, ValidationError
from app.core.security import hash_password, verify_password
from app.db.models.banking import Account, Card, Customer, FAQEntry, Loan, Ticket, Transaction
from app.tools.base import ToolContext, tool

AUTH_STATE_KEY = "authenticated_customer_id"
MAX_AUTH_ATTEMPTS = 3


def _require_auth(ctx: ToolContext, customer_id: str | None = None) -> str:
    authed = ctx.state.get(AUTH_STATE_KEY)
    if not authed:
        raise ValidationError(
            "Customer is not authenticated. Call authenticate_customer first.",
            details={"required_tool": "authenticate_customer"},
        )
    if customer_id and customer_id != authed:
        raise ValidationError(
            "Requested customer does not match the authenticated session",
            details={"authenticated_customer_id": authed},
        )
    return authed


def _mask_account(number: str) -> str:
    return f"****{number[-4:]}" if len(number) > 4 else "****"


# --- authentication ----------------------------------------------------------
class AuthenticateArgs(BaseModel):
    identifier: str = Field(description="Customer number, registered email or phone number")
    pin: str | None = Field(default=None, description="Telephone banking PIN")
    security_answer: str | None = Field(default=None, description="Answer to the security question")


@tool(
    "authenticate_customer",
    "Authenticate a customer by customer number, email or phone plus PIN or security answer. "
    "Must be called before any account, card or loan tool.",
    AuthenticateArgs,
    category="banking",
    writes_data=True,
    idempotent=False,
    timeout_seconds=15,
)
async def authenticate_customer(args: AuthenticateArgs, ctx: ToolContext) -> dict[str, Any]:
    session = ctx.session
    ident = args.identifier.strip()
    customer = (
        await session.execute(
            select(Customer).where(
                or_(
                    Customer.customer_number == ident,
                    func.lower(Customer.email) == ident.lower(),
                    Customer.phone == ident,
                )
            )
        )
    ).scalar_one_or_none()
    if customer is None:
        raise NotFoundError("No customer found for that identifier")
    if customer.failed_auth_attempts >= MAX_AUTH_ATTEMPTS:
        return {"authenticated": False, "reason": "locked",
                "message": "Account is locked after repeated failed attempts. Escalate to a human."}

    verified = False
    method = None
    if args.pin and customer.auth_pin_hash:
        verified = verify_password(args.pin, customer.auth_pin_hash)
        method = "pin"
    if not verified and args.security_answer and customer.security_answer_hash:
        verified = verify_password(args.security_answer.strip().lower(),
                                   customer.security_answer_hash)
        method = "security_question"

    if not verified:
        customer.failed_auth_attempts += 1
        await session.flush()
        return {
            "authenticated": False,
            "reason": "invalid_credentials",
            "attempts_remaining": max(MAX_AUTH_ATTEMPTS - customer.failed_auth_attempts, 0),
            "security_question": customer.security_question,
        }

    customer.failed_auth_attempts = 0
    ctx.state[AUTH_STATE_KEY] = customer.id
    await session.flush()
    return {
        "authenticated": True,
        "method": method,
        "customer_id": customer.id,
        "customer_number": customer.customer_number,
        "full_name": customer.full_name,
        "segment": customer.segment,
        "kyc_status": customer.kyc_status,
        "relationship_manager": customer.relationship_manager,
    }


# --- account lookup -----------------------------------------------------------
class LookupArgs(BaseModel):
    customer_id: str | None = Field(default=None, description="Authenticated customer id")


@tool(
    "lookup_customer_accounts",
    "List all accounts held by the authenticated customer with type, status and branch.",
    LookupArgs,
    category="banking",
)
async def lookup_customer_accounts(args: LookupArgs, ctx: ToolContext) -> dict[str, Any]:
    customer_id = _require_auth(ctx, args.customer_id)
    accounts = (
        await ctx.session.execute(select(Account).where(Account.customer_id == customer_id))
    ).scalars().all()
    return {
        "count": len(accounts),
        "accounts": [
            {
                "account_id": a.id,
                "account_number_masked": _mask_account(a.account_number),
                "type": a.account_type,
                "currency": a.currency,
                "status": a.status,
                "branch_code": a.branch_code,
                "ifsc": a.ifsc,
                "opened_on": a.opened_on.isoformat() if a.opened_on else None,
            }
            for a in accounts
        ],
    }


class BalanceArgs(BaseModel):
    account_id: str | None = Field(default=None, description="Specific account id; omit for all")


@tool(
    "get_account_balance",
    "Return current, available and held balances for one or all of the customer's accounts.",
    BalanceArgs,
    category="banking",
)
async def get_account_balance(args: BalanceArgs, ctx: ToolContext) -> dict[str, Any]:
    customer_id = _require_auth(ctx)
    stmt = select(Account).where(Account.customer_id == customer_id)
    if args.account_id:
        stmt = stmt.where(Account.id == args.account_id)
    accounts = (await ctx.session.execute(stmt)).scalars().all()
    if not accounts:
        raise NotFoundError("No matching account for this customer")
    return {
        "balances": [
            {
                "account_id": a.id,
                "account_number_masked": _mask_account(a.account_number),
                "type": a.account_type,
                "currency": a.currency,
                "balance": round(a.balance, 2),
                "available_balance": round(a.available_balance, 2),
                "hold_amount": round(a.hold_amount, 2),
                "overdraft_limit": round(a.overdraft_limit, 2),
                "as_of": datetime.now(UTC).isoformat(),
            }
            for a in accounts
        ]
    }


class TransactionArgs(BaseModel):
    account_id: str | None = Field(default=None)
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=20, ge=1, le=200)
    min_amount: float | None = Field(default=None)
    search: str | None = Field(default=None, description="Merchant or description filter")


@tool(
    "get_recent_transactions",
    "Retrieve recent posted transactions for the customer with optional amount/text filters.",
    TransactionArgs,
    category="banking",
)
async def get_recent_transactions(args: TransactionArgs, ctx: ToolContext) -> dict[str, Any]:
    customer_id = _require_auth(ctx)
    since = datetime.now(UTC) - timedelta(days=args.days)
    stmt = (
        select(Transaction)
        .where(Transaction.customer_id == customer_id, Transaction.booked_at >= since)
        .order_by(Transaction.booked_at.desc())
        .limit(args.limit)
    )
    if args.account_id:
        stmt = stmt.where(Transaction.account_id == args.account_id)
    if args.min_amount is not None:
        stmt = stmt.where(func.abs(Transaction.amount) >= args.min_amount)
    if args.search:
        pattern = f"%{args.search}%"
        stmt = stmt.where(
            or_(Transaction.merchant.ilike(pattern), Transaction.description.ilike(pattern))
        )
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return {
        "count": len(rows),
        "window_days": args.days,
        "transactions": [
            {
                "reference": t.reference,
                "booked_at": t.booked_at.isoformat(),
                "amount": round(t.amount, 2),
                "currency": t.currency,
                "direction": t.direction,
                "channel": t.channel,
                "merchant": t.merchant,
                "category": t.category,
                "description": t.description,
                "status": t.status,
                "balance_after": t.balance_after,
                "flagged": t.is_flagged,
            }
            for t in rows
        ],
    }


class CardArgs(BaseModel):
    card_type: str | None = Field(default=None, description="credit or debit")


@tool(
    "get_credit_card_details",
    "Return the customer's cards with limits, balances, dues, APR and reward points.",
    CardArgs,
    category="banking",
)
async def get_credit_card_details(args: CardArgs, ctx: ToolContext) -> dict[str, Any]:
    customer_id = _require_auth(ctx)
    stmt = select(Card).where(Card.customer_id == customer_id)
    if args.card_type:
        stmt = stmt.where(Card.card_type == args.card_type)
    cards = (await ctx.session.execute(stmt)).scalars().all()
    return {
        "count": len(cards),
        "cards": [
            {
                "card_id": c.id,
                "masked_number": c.card_number_masked,
                "type": c.card_type,
                "network": c.network,
                "product": c.product_name,
                "status": c.status,
                "credit_limit": round(c.credit_limit, 2),
                "available_credit": round(c.available_credit, 2),
                "current_balance": round(c.current_balance, 2),
                "minimum_due": round(c.minimum_due, 2),
                "statement_date": c.statement_date.isoformat() if c.statement_date else None,
                "due_date": c.due_date.isoformat() if c.due_date else None,
                "apr_percent": c.apr,
                "reward_points": c.reward_points,
                "expiry": c.expiry,
            }
            for c in cards
        ],
    }


class LoanArgs(BaseModel):
    status: str | None = Field(default=None, description="active, closed or delinquent")


@tool(
    "get_loan_details",
    "Return the customer's loans with outstanding principal, EMI, tenure and delinquency.",
    LoanArgs,
    category="banking",
)
async def get_loan_details(args: LoanArgs, ctx: ToolContext) -> dict[str, Any]:
    customer_id = _require_auth(ctx)
    stmt = select(Loan).where(Loan.customer_id == customer_id)
    if args.status:
        stmt = stmt.where(Loan.status == args.status)
    loans = (await ctx.session.execute(stmt)).scalars().all()
    return {
        "count": len(loans),
        "loans": [
            {
                "loan_number": loan.loan_number,
                "type": loan.loan_type,
                "principal": round(loan.principal, 2),
                "outstanding": round(loan.outstanding, 2),
                "interest_rate_percent": loan.interest_rate,
                "tenure_months": loan.tenure_months,
                "emi_amount": round(loan.emi_amount, 2),
                "emis_paid": loan.emis_paid,
                "next_due_date": loan.next_due_date.isoformat() if loan.next_due_date else None,
                "status": loan.status,
                "days_past_due": loan.days_past_due,
                "collateral": loan.collateral,
            }
            for loan in loans
        ],
    }


# --- servicing ---------------------------------------------------------------
class TicketArgs(BaseModel):
    subject: str = Field(max_length=280)
    description: str
    category: str = Field(default="general")
    priority: str = Field(default="medium", description="low|medium|high|urgent")
    sentiment: str | None = Field(default=None)


@tool(
    "create_support_ticket",
    "Create a support ticket in the servicing system for follow-up by a human team.",
    TicketArgs,
    category="banking",
    writes_data=True,
    idempotent=False,
    requires_approval=False,
)
async def create_support_ticket(args: TicketArgs, ctx: ToolContext) -> dict[str, Any]:
    customer_id = ctx.state.get(AUTH_STATE_KEY)
    sla_hours = {"urgent": 4, "high": 8, "medium": 24, "low": 72}.get(args.priority, 24)
    ticket = Ticket(
        ticket_number=f"TKT-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        customer_id=customer_id,
        subject=args.subject,
        description=args.description,
        category=args.category,
        priority=args.priority,
        sentiment=args.sentiment,
        channel="ai_agent",
        execution_id=ctx.execution_id,
        assigned_team={"card": "Cards Servicing", "loan": "Retail Lending",
                       "fraud": "Fraud Operations"}.get(args.category, "Customer Care"),
        sla_due_at=datetime.now(UTC) + timedelta(hours=sla_hours),
        notes=[{"at": datetime.now(UTC).isoformat(), "by": "ai_agent",
                "note": "Ticket raised by AI customer service agent"}],
    )
    ctx.session.add(ticket)
    await ctx.session.flush()
    return {
        "ticket_number": ticket.ticket_number,
        "status": ticket.status,
        "priority": ticket.priority,
        "assigned_team": ticket.assigned_team,
        "sla_due_at": ticket.sla_due_at.isoformat(),
    }


class EscalateArgs(BaseModel):
    reason: str
    urgency: str = Field(default="high")
    ticket_number: str | None = None


@tool(
    "escalate_to_human",
    "Escalate the conversation to a human specialist team, optionally linking a ticket. "
    "Requires human approval before it takes effect.",
    EscalateArgs,
    category="banking",
    writes_data=True,
    idempotent=False,
    requires_approval=True,
    approval_risk="high",
)
async def escalate_to_human(args: EscalateArgs, ctx: ToolContext) -> dict[str, Any]:
    customer_id = ctx.state.get(AUTH_STATE_KEY)
    ticket: Ticket | None = None
    if args.ticket_number:
        ticket = (
            await ctx.session.execute(
                select(Ticket).where(Ticket.ticket_number == args.ticket_number)
            )
        ).scalar_one_or_none()
    if ticket is None:
        ticket = Ticket(
            ticket_number=f"ESC-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
            customer_id=customer_id,
            subject=f"Escalation: {args.reason[:200]}",
            description=args.reason,
            category="escalation",
            priority="urgent" if args.urgency == "critical" else "high",
            execution_id=ctx.execution_id,
        )
        ctx.session.add(ticket)
    ticket.escalated = True
    ticket.escalation_reason = args.reason
    ticket.assigned_team = "Tier 2 Specialist Desk"
    ticket.status = "escalated"
    ticket.notes = [*(ticket.notes or []),
                    {"at": datetime.now(UTC).isoformat(), "by": ctx.user_email or "ai_agent",
                     "note": f"Escalated ({args.urgency}): {args.reason}"}]
    await ctx.session.flush()
    return {"escalated": True, "ticket_number": ticket.ticket_number,
            "assigned_team": ticket.assigned_team, "urgency": args.urgency}


class FaqArgs(BaseModel):
    query: str
    category: str | None = None
    limit: int = Field(default=3, ge=1, le=10)


@tool(
    "search_faq",
    "Search the curated banking FAQ knowledge base for policy and product answers.",
    FaqArgs,
    category="banking",
)
async def search_faq(args: FaqArgs, ctx: ToolContext) -> dict[str, Any]:
    terms = [t for t in re.findall(r"[a-z0-9]{3,}", args.query.lower())]
    stmt = select(FAQEntry)
    if args.category:
        stmt = stmt.where(FAQEntry.category == args.category)
    entries = (await ctx.session.execute(stmt)).scalars().all()
    scored: list[tuple[float, FAQEntry]] = []
    for entry in entries:
        haystack = f"{entry.question} {entry.answer} {' '.join(entry.keywords or [])}".lower()
        score = sum(haystack.count(term) for term in terms)
        if score:
            scored.append((score / (1 + len(haystack) / 2000), entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[: args.limit]
    for _, entry in top:
        entry.hit_count += 1
    await ctx.session.flush()
    return {
        "count": len(top),
        "results": [
            {"question": e.question, "answer": e.answer, "category": e.category,
             "product": e.product, "score": round(s, 4)}
            for s, e in top
        ],
    }


class SentimentArgs(BaseModel):
    text: str


NEGATIVE = {"angry", "furious", "terrible", "worst", "unacceptable", "fraud", "scam", "cheated",
            "disgusted", "frustrated", "annoyed", "complaint", "useless", "horrible", "stuck",
            "delay", "failed", "wrong", "ridiculous", "escalate", "sue", "ombudsman"}
POSITIVE = {"thanks", "thank", "great", "excellent", "helpful", "appreciate", "good", "resolved",
            "happy", "perfect", "wonderful", "quick"}
URGENT = {"urgent", "immediately", "asap", "emergency", "blocked", "stolen", "unauthorised",
          "unauthorized", "lost"}


@tool(
    "detect_sentiment",
    "Score customer sentiment and urgency from their message to drive escalation decisions.",
    SentimentArgs,
    category="analysis",
)
async def detect_sentiment(args: SentimentArgs, ctx: ToolContext) -> dict[str, Any]:
    words = set(re.findall(r"[a-z']+", args.text.lower()))
    neg = len(words & NEGATIVE)
    pos = len(words & POSITIVE)
    urgent = len(words & URGENT)
    exclamations = args.text.count("!")
    caps_ratio = sum(1 for c in args.text if c.isupper()) / max(len(args.text), 1)
    score = (pos - neg) / max(pos + neg, 1)
    if caps_ratio > 0.3 or exclamations >= 3:
        score -= 0.2
    label = "positive" if score > 0.25 else "negative" if score < -0.25 else "neutral"
    urgency = "high" if urgent or score < -0.6 else "medium" if score < -0.25 else "low"
    result = {
        "label": label,
        "score": round(max(min(score, 1.0), -1.0), 3),
        "urgency": urgency,
        "signals": {"negative_terms": neg, "positive_terms": pos, "urgent_terms": urgent,
                    "exclamations": exclamations, "caps_ratio": round(caps_ratio, 3)},
        "escalation_recommended": urgency == "high",
    }
    ctx.state["sentiment"] = result
    return result
