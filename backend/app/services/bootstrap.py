"""Platform bootstrap: schema, identities, agent registry, knowledge corpus, sample data."""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import IMPLEMENTED, ROADMAP
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.base import Base
from app.db.models.agents import Agent, AgentVersion
from app.db.models.banking import (
    Account,
    BureauRecord,
    Card,
    CreditApplication,
    Customer,
    DelinquencyCase,
    FAQEntry,
    Holding,
    KycCase,
    Loan,
    Portfolio,
    PriceBar,
    SanctionsEntry,
    Security,
    Transaction,
)
from app.db.models.identity import FeatureFlag, User
from app.db.models.knowledge import KnowledgeSource
from app.db.session import engine
from app.seed.corpus import KNOWLEDGE_SOURCES, SEED_DOCUMENTS
from app.seed.watchlist import INTERNAL_WATCHLIST
from app.tools.kyc import normalise_name

log = get_logger("bootstrap")


async def ensure_schema() -> None:
    """Create tables when running without Alembic (local/dev/test)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def seed_identities(session: AsyncSession) -> dict[str, Any]:
    existing = int((await session.execute(select(func.count(User.id)))).scalar_one())
    if existing:
        return {"users_created": 0}
    users = [
        (settings.bootstrap_admin_email, "Platform Administrator", ["admin"], "Technology"),
        ("operator@finops.local", "Operations Engineer", ["operator"], "Technology"),
        ("approver@finops.local", "Compliance Approver", ["approver"], "Compliance"),
        ("auditor@finops.local", "Internal Auditor", ["auditor"], "Audit"),
        ("builder@finops.local", "Agent Builder", ["agent_builder"], "Technology"),
    ]
    for email, name, roles, department in users:
        session.add(
            User(
                email=email, full_name=name, roles=roles, department=department,
                hashed_password=hash_password(settings.bootstrap_admin_password),
            )
        )
    await session.flush()
    log.info("identities_seeded", count=len(users),
             note="All seeded accounts share BOOTSTRAP_ADMIN_PASSWORD - rotate before production")
    return {"users_created": len(users)}


async def _ensure_current_version(session: AsyncSession, agent: Agent,
                                  config: dict[str, Any], changelog: str) -> None:
    """Give the agent a published version row if it has none.

    A promoted roadmap agent never had one — it was a placeholder — and the version history
    endpoints and the rollback path both assume a current version exists.
    """
    existing = (
        await session.execute(select(AgentVersion).where(AgentVersion.agent_id == agent.id))
    ).scalars().first()
    if existing is not None:
        return
    session.add(AgentVersion(agent_id=agent.id, version=agent.version or 1, config=config,
                             changelog=changelog, published=True,
                             published_at=datetime.now(UTC), published_by="system",
                             is_current=True))


async def seed_agents(session: AsyncSession) -> dict[str, Any]:
    created = updated = 0
    for spec in IMPLEMENTED:
        agent = (
            await session.execute(select(Agent).where(Agent.key == spec.key))
        ).scalar_one_or_none()
        config = spec.to_config()
        if agent is None:
            agent = Agent(
                key=spec.key, name=spec.name, description=spec.description,
                category=spec.category, availability="implemented", lifecycle_state="active",
                owner=spec.owner, owner_email=spec.owner_email, department=spec.department,
                version=1, is_builtin=True, config=config, tags=spec.tags, tools=spec.tools,
                knowledge_sources=spec.knowledge_sources, sla_latency_ms=spec.sla_latency_ms,
            )
            session.add(agent)
            await session.flush()
            await _ensure_current_version(session, agent, config,
                                          "Built-in agent definition")
            created += 1
        elif agent.availability == "coming_soon":
            # The agent has shipped since this database was seeded. The roadmap row is the
            # platform's own placeholder — not an operator's decision to disable something —
            # so it is promoted here. Without this, every environment seeded before the
            # release answers "Agent 'x' is not implemented yet" for a released agent, and
            # the only fix is editing the table by hand.
            agent.name, agent.description = spec.name, spec.description
            agent.category, agent.department = spec.category, spec.department
            agent.owner, agent.owner_email = spec.owner, spec.owner_email
            agent.availability, agent.lifecycle_state = "implemented", "active"
            agent.config, agent.tags, agent.tools = config, spec.tags, spec.tools
            agent.knowledge_sources = spec.knowledge_sources
            agent.sla_latency_ms = spec.sla_latency_ms
            agent.version = agent.version or 1
            await _ensure_current_version(session, agent, config,
                                          "Promoted from roadmap to implemented")
            log.info("agent_promoted", agent=spec.key)
            updated += 1
        elif agent.is_builtin and not agent.config:
            agent.config = config
            updated += 1

    for entry in ROADMAP:
        agent = (
            await session.execute(select(Agent).where(Agent.key == entry["key"]))
        ).scalar_one_or_none()
        if agent is None:
            session.add(
                Agent(
                    key=entry["key"], name=entry["name"], description=entry["description"],
                    category=entry["category"], availability="coming_soon",
                    lifecycle_state="disabled", owner=entry["owner"],
                    department=entry["department"], is_builtin=True,
                    config={"planned_quarter": entry["planned_quarter"]},
                    tags=["roadmap"],
                )
            )
            created += 1
    await session.flush()
    return {"agents_created": created, "agents_updated": updated}


async def seed_knowledge(session: AsyncSession) -> dict[str, Any]:
    from app.rag.pipeline import pipeline

    created_sources = 0
    for definition in KNOWLEDGE_SOURCES:
        source = (
            await session.execute(
                select(KnowledgeSource).where(KnowledgeSource.key == definition["key"])
            )
        ).scalar_one_or_none()
        if source is None:
            session.add(KnowledgeSource(**definition))
            created_sources += 1
    await session.flush()

    ingested = 0
    for document in SEED_DOCUMENTS:
        source = (
            await session.execute(
                select(KnowledgeSource).where(KnowledgeSource.key == document["source_key"])
            )
        ).scalar_one_or_none()
        if source is None:
            continue
        await pipeline.ingest_document(
            session,
            source=source,
            title=document["title"],
            content=document["content"],
            external_id=document["external_id"],
            uri=document.get("uri"),
            author=document.get("author"),
            classification=document.get("classification", "internal"),
            metadata=document.get("metadata", {}),
        )
        ingested += 1
    return {"knowledge_sources_created": created_sources, "documents_ingested": ingested}


async def seed_watchlists(session: AsyncSession) -> dict[str, Any]:
    existing = int((await session.execute(select(func.count(SanctionsEntry.id)))).scalar_one())
    if existing:
        return {"watchlist_entries_created": 0}
    for entry in INTERNAL_WATCHLIST:
        session.add(
            SanctionsEntry(
                list_name=entry["list_name"], entry_type=entry.get("entry_type", "individual"),
                full_name=entry["full_name"],
                normalised_name=normalise_name(entry["full_name"]),
                aliases=entry.get("aliases", []), date_of_birth=entry.get("date_of_birth"),
                nationality=entry.get("nationality"), program=entry.get("program"),
                position=entry.get("position"), is_pep=entry.get("is_pep", False),
                source_url=entry.get("source_url"), remarks=entry.get("remarks"),
            )
        )
    await session.flush()
    return {"watchlist_entries_created": len(INTERNAL_WATCHLIST)}


async def seed_feature_flags(session: AsyncSession) -> dict[str, Any]:
    flags = [
        ("streaming_responses", "Stream LLM tokens to the console in real time", True),
        ("llm_judge_evaluation", "Run the LLM-as-judge evaluator after each execution", True),
        ("auto_evaluate_executions", "Evaluate every successful execution automatically", False),
        ("agent_builder", "Expose the agent builder to non-admin roles", True),
        ("hybrid_retrieval", "Blend vector and keyword scores during retrieval", True),
        ("cost_hard_stop", "Abort executions that exceed the per-execution cost cap", True),
    ]
    created = 0
    for key, description, enabled in flags:
        existing = (
            await session.execute(select(FeatureFlag).where(FeatureFlag.key == key))
        ).scalar_one_or_none()
        if existing is None:
            session.add(FeatureFlag(key=key, description=description, enabled=enabled,
                                    updated_by="system"))
            created += 1
    return {"feature_flags_created": created}


async def bootstrap(session: AsyncSession) -> dict[str, Any]:
    result: dict[str, Any] = {}
    result |= await seed_identities(session)
    result |= await seed_agents(session)
    result |= await seed_feature_flags(session)
    result |= await seed_watchlists(session)
    result |= await seed_knowledge(session)
    await session.commit()
    log.info("bootstrap_complete", **result)
    return result


# --------------------------------------------------------------------------- #
# Sample banking environment (opt-in via `finops seed-banking`)                #
# --------------------------------------------------------------------------- #
FIRST_NAMES = ["Aarav", "Diya", "Vihaan", "Ananya", "Arjun", "Ishita", "Kabir", "Meera",
               "Rohan", "Sara", "Vikram", "Priya", "Karan", "Nisha", "Rahul", "Tara"]
LAST_NAMES = ["Sharma", "Patel", "Reddy", "Iyer", "Nair", "Gupta", "Kulkarni", "Bose",
              "Chatterjee", "Menon", "Desai", "Rao"]
MERCHANTS = ["BigBazaar Retail", "Swiggy Foods", "Indian Oil", "Amazon India", "Apollo Pharmacy",
             "IRCTC", "Croma Electronics", "Reliance Digital", "Uber India", "Airtel Payments"]
CATEGORIES = ["groceries", "dining", "fuel", "shopping", "healthcare", "travel", "electronics",
              "utilities", "transport", "telecom"]
COUNTRIES = ["IND", "IND", "IND", "IND", "ARE", "SGP", "GBR", "USA", "IRN", "RUS"]


async def seed_sample_banking(session: AsyncSession, *, customers: int = 24,
                              seed: int = 20260401) -> dict[str, Any]:
    """Populate a representative retail-banking dataset for evaluation environments."""
    existing = int((await session.execute(select(func.count(Customer.id)))).scalar_one())
    if existing:
        return {"skipped": True, "reason": f"{existing} customers already present"}

    rng = random.Random(seed)
    now = datetime.now(UTC)
    created_customers: list[Customer] = []

    for i in range(customers):
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        segment = rng.choices(["retail", "premier", "private", "sme"], [0.65, 0.2, 0.08, 0.07])[0]
        customer = Customer(
            customer_number=f"CUS-{100001 + i}",
            full_name=name,
            email=f"{name.lower().replace(' ', '.')}{i}@example.com",
            phone=f"+9198{rng.randint(10000000, 99999999)}",
            date_of_birth=date(rng.randint(1955, 2003), rng.randint(1, 12), rng.randint(1, 28)),
            nationality="IN",
            address_line1=f"{rng.randint(1, 400)} {rng.choice(['MG Road', 'Park Street', 'Nehru Nagar', 'Residency Road'])}",
            address_city=rng.choice(["Mumbai", "Bengaluru", "Delhi", "Chennai", "Pune"]),
            address_state=rng.choice(["Maharashtra", "Karnataka", "Delhi", "Tamil Nadu"]),
            address_postcode=str(rng.randint(110001, 700099)),
            segment=segment,
            kyc_status=rng.choices(["verified", "pending", "review"], [0.8, 0.12, 0.08])[0],
            risk_rating=rng.choices(["low", "medium", "high"], [0.7, 0.24, 0.06])[0],
            risk_score=round(rng.uniform(2, 78), 2),
            onboarded_at=now - timedelta(days=rng.randint(30, 2400)),
            relationship_manager=rng.choice(["A. Krishnan", "S. Fernandes", "R. Kapoor", None]),
            auth_pin_hash=hash_password(f"{1000 + i}"),
            security_question="What was the name of your first school?",
            security_answer_hash=hash_password("st marys"),
        )
        session.add(customer)
        created_customers.append(customer)
    await session.flush()

    account_count = txn_count = 0
    for index, customer in enumerate(created_customers):
        for kind in (["savings", "current"] if customer.segment == "sme" else ["savings"]):
            balance = round(rng.uniform(15_000, 4_500_000), 2)
            account = Account(
                account_number=f"5001{index:04d}{rng.randint(1000, 9999)}",
                customer_id=customer.id, account_type=kind, currency="INR",
                balance=balance, available_balance=balance, status="active",
                branch_code=f"BR{rng.randint(100, 999)}", ifsc=f"FNOP0{rng.randint(100000, 999999)}",
                opened_on=(customer.onboarded_at or now).date(),
                overdraft_limit=50_000 if kind == "current" else 0,
            )
            session.add(account)
            await session.flush()
            account_count += 1

            running_balance = balance
            # Structuring pattern for a small number of customers so monitoring has real hits.
            structuring = index % 11 == 3
            pass_through = index % 13 == 5
            for day_offset in range(rng.randint(45, 120)):
                for _ in range(rng.randint(0, 3)):
                    booked = now - timedelta(days=day_offset, hours=rng.randint(0, 23),
                                             minutes=rng.randint(0, 59))
                    direction = rng.choices(["debit", "credit"], [0.72, 0.28])[0]
                    amount = round(rng.uniform(120, 42_000), 2)
                    if structuring and day_offset % 17 == 0:
                        direction, amount = "credit", round(rng.uniform(910_000, 995_000), 2)
                    if pass_through and day_offset == 12:
                        direction, amount = "credit", 2_400_000.00
                    if pass_through and day_offset == 11:
                        direction, amount = "debit", round(rng.uniform(700_000, 900_000), 2)
                    running_balance += amount if direction == "credit" else -amount
                    txn = Transaction(
                        reference=f"TXN{now:%Y}{txn_count:09d}",
                        account_id=account.id, customer_id=customer.id, booked_at=booked,
                        amount=amount, currency="INR", direction=direction,
                        channel=rng.choice(["upi", "neft", "imps", "card", "atm", "rtgs"]),
                        merchant=rng.choice(MERCHANTS) if direction == "debit" else None,
                        category=rng.choice(CATEGORIES) if direction == "debit" else "transfer",
                        description=f"{direction.title()} transaction",
                        counterparty_name=rng.choice(
                            ["Zenith Trading FZE", "Orion Exports Ltd", "M. Sharma", "Cygnus LLC",
                             "Helios Metals", None]),
                        counterparty_bank=rng.choice(["HDFC", "ICICI", "Emirates NBD", "DBS", None]),
                        country=rng.choice(COUNTRIES) if rng.random() < 0.12 else "IND",
                        balance_after=round(running_balance, 2),
                        risk_score=round(rng.uniform(0, 40), 2),
                        device_id=f"DEV-{rng.randint(1000, 9999)}",
                        ip_address=f"49.{rng.randint(1, 254)}.{rng.randint(1, 254)}."
                                   f"{rng.randint(1, 254)}",
                    )
                    session.add(txn)
                    txn_count += 1
            account.balance = round(running_balance, 2)
            account.available_balance = round(running_balance - account.hold_amount, 2)

        if rng.random() < 0.7:
            limit = round(rng.choice([100_000, 250_000, 500_000, 1_000_000]), 2)
            used = round(rng.uniform(0, limit * 0.8), 2)
            statement = date.today().replace(day=1) - timedelta(days=1)
            session.add(Card(
                card_number_masked=f"****{rng.randint(1000, 9999)}",
                card_token=hashlib.sha256(f"{customer.id}-card".encode()).hexdigest()[:32],
                customer_id=customer.id, card_type="credit",
                network=rng.choice(["visa", "mastercard", "rupay"]),
                product_name=rng.choice(["Signature Rewards", "Platinum Travel", "Cashback Plus"]),
                credit_limit=limit, available_credit=round(limit - used, 2), current_balance=used,
                minimum_due=round(used * 0.05, 2), statement_date=statement,
                due_date=statement + timedelta(days=20), apr=round(rng.uniform(24, 42), 2),
                reward_points=rng.randint(0, 85_000),
                expiry=f"{rng.randint(1, 12):02d}/{rng.randint(27, 31)}",
                issued_on=(customer.onboarded_at or now).date(),
            ))
        # Customers in the reserved band always take a loan, so the delinquency spread is
        # actually realised; a random draw over a small sample routinely produces none. The
        # band sits after the credit applicants so an application profile is never distorted
        # by a forced facility.
        in_delinquency_band = _DELINQUENCY_BAND_START <= index < (
            _DELINQUENCY_BAND_START + len(_DELINQUENCY_SPREAD))
        if in_delinquency_band or rng.random() < 0.45:
            principal = round(rng.choice([300_000, 800_000, 2_500_000, 6_000_000]), 2)
            tenure = rng.choice([36, 60, 120, 240])
            rate = round(rng.uniform(8.4, 15.5), 2)
            monthly = rate / 1200
            emi = round(principal * monthly * (1 + monthly) ** tenure / ((1 + monthly) ** tenure - 1), 2)
            paid = rng.randint(0, tenure - 1)
            # The first loans take a fixed spread across the delinquency buckets so every
            # seeded environment has something in each stage for collections to work; the
            # rest follow the portfolio distribution.
            if in_delinquency_band:
                dpd = _DELINQUENCY_SPREAD[index - _DELINQUENCY_BAND_START]
            else:
                dpd = rng.choices([0, 0, 0, 15, 45, 92], [0.72, 0.1, 0.06, 0.06, 0.04, 0.02])[0]
            session.add(Loan(
                loan_number=f"LN-{rng.randint(100000, 999999)}", customer_id=customer.id,
                loan_type=rng.choice(["personal", "home", "auto", "education"]),
                principal=principal, outstanding=round(principal * (1 - paid / tenure), 2),
                interest_rate=rate, tenure_months=tenure, emi_amount=emi, emis_paid=paid,
                next_due_date=date.today() + timedelta(days=rng.randint(1, 30)),
                status="delinquent" if dpd > 30 else "active", days_past_due=dpd,
                disbursed_on=(customer.onboarded_at or now).date(),
            ))
    await session.flush()

    txn_count += await _seed_income_and_repayments(session, created_customers, rng, now)
    credit_seeded = await _seed_credit_book(session, created_customers, rng, now)
    collections_seeded = await _seed_collections_book(session, created_customers, now)

    # FAQ knowledge for the customer service agent
    faqs = [
        ("How do I block my lost credit card?",
         "Call 1800-200-3344 or use the FinOps app: Cards > Manage > Block. Blocking is immediate "
         "and a replacement card is dispatched within 5 working days.", "cards",
         ["block", "lost", "stolen", "card"]),
        ("What is the minimum balance for a savings account?",
         "Metro and urban branches require an average monthly balance of INR 10,000; semi-urban "
         "INR 5,000; rural INR 2,500. Salary accounts have no minimum balance requirement.",
         "accounts", ["minimum", "balance", "savings", "amb"]),
        ("How long do NEFT transfers take?",
         "NEFT settles in half-hourly batches on all working days and typically credits within "
         "30 minutes. IMPS and UPI are instant and available 24x7.", "payments",
         ["neft", "imps", "upi", "transfer", "time"]),
        ("How can I get a duplicate loan statement?",
         "Loan statements are available in the app under Loans > Statements, or by writing to "
         "loans@finops.local from your registered email. Delivery is within 2 working days.",
         "loans", ["statement", "loan", "duplicate"]),
        ("What are the credit card late payment charges?",
         "Late payment fee is INR 500 for balances up to INR 10,000, INR 750 up to INR 25,000 and "
         "INR 1,200 above that, plus applicable finance charges from the transaction date.",
         "cards", ["late", "payment", "charges", "fee"]),
        ("How do I update my registered mobile number?",
         "Visit any branch with photo ID, or use the app under Profile > Contact details with "
         "Aadhaar OTP verification. Changes take effect after a 24-hour cooling period.",
         "accounts", ["mobile", "number", "update", "contact"]),
        ("What should I do about an unauthorised transaction?",
         "Report within 3 working days for zero liability under RBI rules. Use the app "
         "(Transactions > Report a problem) or call 1800-200-3344. A dispute case is raised and "
         "provisional credit is issued within 10 working days.", "fraud",
         ["unauthorised", "fraud", "dispute", "unauthorized"]),
    ]
    for question, answer, category, keywords in faqs:
        session.add(FAQEntry(question=question, answer=answer, category=category,
                             keywords=keywords))

    # Instrument master, price history and portfolios for investment research
    instruments = [
        ("RELIANCE", "Reliance Industries Ltd", "Energy", "Refining & Petrochemicals", 2850.0),
        ("TCS", "Tata Consultancy Services Ltd", "Technology", "IT Services", 3980.0),
        ("HDFCBANK", "HDFC Bank Ltd", "Financials", "Private Sector Bank", 1680.0),
        ("INFY", "Infosys Ltd", "Technology", "IT Services", 1820.0),
        ("ITC", "ITC Ltd", "Consumer Staples", "Diversified FMCG", 465.0),
        ("SUNPHARMA", "Sun Pharmaceutical Industries Ltd", "Healthcare", "Pharmaceuticals", 1720.0),
        ("LT", "Larsen & Toubro Ltd", "Industrials", "Engineering & Construction", 3620.0),
        ("NIFTY50", "Nifty 50 Index", "Index", "Broad Market", 24500.0),
    ]
    for symbol, name, sector, industry, price in instruments:
        session.add(Security(
            symbol=symbol, name=name, exchange="NSE",
            asset_class="index" if symbol == "NIFTY50" else "equity",
            sector=sector, industry=industry, currency="INR", country="IND",
            last_price=price, last_price_at=now,
            fundamentals={} if symbol == "NIFTY50" else {
                "period": "FY2025",
                "revenue": round(price * 1e6 * rng.uniform(0.8, 1.4), 0),
                "revenue_prior": round(price * 1e6 * rng.uniform(0.7, 1.2), 0),
                "gross_profit": round(price * 1e6 * rng.uniform(0.25, 0.5), 0),
                "operating_income": round(price * 1e6 * rng.uniform(0.12, 0.3), 0),
                "net_income": round(price * 1e6 * rng.uniform(0.08, 0.2), 0),
                "net_income_prior": round(price * 1e6 * rng.uniform(0.06, 0.18), 0),
                "total_assets": round(price * 1e6 * rng.uniform(1.5, 3.0), 0),
                "total_equity": round(price * 1e6 * rng.uniform(0.6, 1.6), 0),
                "total_debt": round(price * 1e6 * rng.uniform(0.1, 1.2), 0),
                "current_assets": round(price * 1e6 * rng.uniform(0.4, 1.0), 0),
                "current_liabilities": round(price * 1e6 * rng.uniform(0.3, 0.8), 0),
                "interest_expense": round(price * 1e6 * rng.uniform(0.005, 0.04), 0),
                "pe_ratio": round(rng.uniform(14, 42), 2),
                "price_to_book": round(rng.uniform(1.2, 9.5), 2),
                "ev_to_ebitda": round(rng.uniform(8, 28), 2),
                "dividend_yield": round(rng.uniform(0.2, 3.4), 2),
                "market_cap": round(price * rng.uniform(1e9, 2e10), 0),
            },
        ))

    bars = 0
    for symbol, _, _, _, price in instruments:
        level = price * 0.82
        for offset in range(400, -1, -1):
            bar_date = date.today() - timedelta(days=offset)
            if bar_date.weekday() >= 5:
                continue
            drift = rng.gauss(0.0004, 0.013)
            level = max(level * (1 + drift), 1.0)
            high = level * (1 + abs(rng.gauss(0, 0.006)))
            low = level * (1 - abs(rng.gauss(0, 0.006)))
            session.add(PriceBar(
                symbol=symbol, bar_date=bar_date, open=round(level * (1 + rng.gauss(0, 0.003)), 2),
                high=round(high, 2), low=round(low, 2), close=round(level, 2),
                volume=round(rng.uniform(2e5, 9e6), 0), source="sample_generator",
            ))
            bars += 1

    portfolio = Portfolio(
        portfolio_code="PF-BALANCED-01", customer_id=created_customers[0].id,
        name="Balanced Growth Mandate", strategy="balanced", base_currency="INR",
        cash_balance=480_000.0, benchmark="NIFTY50", risk_profile="moderate",
        mandate={"max_single_stock_pct": 20, "min_cash_pct": 2, "excluded_sectors": ["Tobacco"]},
    )
    session.add(portfolio)
    await session.flush()
    for symbol, _, sector, _, price in instruments[:6]:
        session.add(Holding(
            portfolio_id=portfolio.id, symbol=symbol,
            quantity=round(rng.uniform(50, 900), 0), average_cost=round(price * rng.uniform(0.7, 1.1), 2),
            currency="INR", asset_class="equity", sector=sector,
            opened_on=date.today() - timedelta(days=rng.randint(40, 700)),
        ))

    session.add(KycCase(
        case_number="KYC-2026-0001",
        applicant_name=created_customers[1].full_name,
        applicant_email=created_customers[1].email,
        applicant_phone=created_customers[1].phone,
        date_of_birth=created_customers[1].date_of_birth,
        nationality="IN",
        declared_address={
            "line1": created_customers[1].address_line1,
            "city": created_customers[1].address_city,
            "state": created_customers[1].address_state,
            "postcode": created_customers[1].address_postcode,
            "country": "India",
        },
        status="in_progress",
    ))

    await session.commit()
    return {
        "customers": len(created_customers),
        "accounts": account_count,
        "transactions": txn_count,
        "faqs": len(faqs),
        "instruments": len(instruments),
        "price_bars": bars,
        "portfolios": 1,
        "kyc_cases": 1,
        **credit_seeded,
        **collections_seeded,
    }


# --- credit and collections sample data ---------------------------------------
async def _seed_income_and_repayments(session: AsyncSession, customers: list[Customer],
                                      rng: random.Random, now: datetime) -> int:
    """Monthly salary credits and loan repayments.

    Affordability assessment verifies declared income against salary credits, and promise
    performance is checked against categorised repayments, so both have to exist in the
    ledger for those tools to return anything but "no evidence".
    """
    created = 0
    for customer in customers:
        accounts = (await session.execute(
            select(Account).where(Account.customer_id == customer.id,
                                  Account.account_type == "savings")
        )).scalars().all()
        if not accounts:
            continue
        account = accounts[0]
        # Credit applicants must have salary credits consistent with what they declared,
        # otherwise every affordability check reports a spurious income variance.
        profile_index = customers.index(customer)
        if profile_index < len(_APPLICATION_PROFILES):
            declared = float(_APPLICATION_PROFILES[profile_index]["income"])
            salary = round(declared * rng.uniform(0.97, 1.03), 2)
        else:
            salary = round(rng.choice([45_000, 65_000, 92_000, 140_000, 210_000]) *
                           rng.uniform(0.95, 1.05), 2)
        loans = (await session.execute(
            select(Loan).where(Loan.customer_id == customer.id)
        )).scalars().all()

        for month in range(6):
            booked = now - timedelta(days=30 * month + rng.randint(0, 2))
            session.add(Transaction(
                reference=f"SAL{booked:%Y%m}{rng.randint(100000, 999999)}",
                account_id=account.id, customer_id=customer.id, booked_at=booked,
                amount=salary, currency="INR", direction="credit", channel="neft",
                category="salary", description="Salary credit",
                counterparty_name="Employer Payroll", country="IND",
            ))
            created += 1
            for loan in loans:
                # A delinquent loan stops paying; that is what makes it delinquent.
                if loan.days_past_due > 30 and month < max(1, loan.days_past_due // 30):
                    continue
                due = booked + timedelta(days=5)
                session.add(Transaction(
                    reference=f"EMI{due:%Y%m}{rng.randint(100000, 999999)}",
                    account_id=account.id, customer_id=customer.id, booked_at=due,
                    amount=loan.emi_amount, currency="INR", direction="debit", channel="ach",
                    category="loan_repayment", description=f"EMI {loan.loan_number}",
                    counterparty_name="FinOps Bank", country="IND",
                ))
                created += 1
    await session.flush()
    return created


#: Application profiles chosen so underwriting has a clean approve, a thin file, a
#: high-FOIR case and a policy knockout to work with.
#: Days past due assigned to the reserved customers, one per collections bucket.
_DELINQUENCY_SPREAD = [12, 40, 75, 130, 220]

#: Where that band starts. It sits immediately after the credit applicants.
_DELINQUENCY_BAND_START = 5

_APPLICATION_PROFILES = [
    {"product": "personal_loan", "amount": 800_000, "tenure": 60, "income": 145_000,
     "expenses": 42_000, "employment": "salaried", "months": 96, "score": 782,
     "dpd": 0, "enquiries": 1, "utilisation": 18.0, "purpose": "Home renovation"},
    {"product": "auto_loan", "amount": 1_200_000, "tenure": 60, "income": 190_000,
     "expenses": 55_000, "employment": "salaried", "months": 54, "score": 741,
     "dpd": 0, "enquiries": 2, "utilisation": 31.0, "purpose": "Vehicle purchase",
     "collateral_type": "vehicle", "collateral_value": 1_500_000},
    {"product": "personal_loan", "amount": 2_000_000, "tenure": 72, "income": 78_000,
     "expenses": 38_000, "employment": "self_employed", "months": 20, "score": 668,
     "dpd": 35, "enquiries": 7, "utilisation": 88.0, "purpose": "Business working capital"},
    {"product": "personal_loan", "amount": 450_000, "tenure": 48, "income": 61_000,
     "expenses": 26_000, "employment": "salaried", "months": 8, "score": 596,
     "dpd": 75, "enquiries": 9, "utilisation": 94.0, "purpose": "Debt consolidation",
     "write_offs": 1},
    {"product": "home_loan", "amount": 5_500_000, "tenure": 240, "income": 320_000,
     "expenses": 90_000, "employment": "professional", "months": 132, "score": 806,
     "dpd": 0, "enquiries": 1, "utilisation": 9.0, "purpose": "Property purchase",
     "collateral_type": "residential_property", "collateral_value": 7_500_000},
]


async def _seed_credit_book(session: AsyncSession, customers: list[Customer],
                            rng: random.Random, now: datetime) -> dict[str, Any]:
    """Bureau records for everyone, and a spread of applications to underwrite."""
    applicant_ids = {
        customers[index].id
        for index in range(min(len(_APPLICATION_PROFILES), len(customers)))
    }

    bureau = 0
    for customer in customers:
        if customer.id in applicant_ids:
            continue   # seeded below from the application profile
        base = rng.choices([790, 745, 705, 660, 610], [0.22, 0.28, 0.24, 0.16, 0.10])[0]
        score = max(300, min(900, base + rng.randint(-25, 25)))
        worst_dpd = 0 if score >= 720 else rng.choice([0, 15, 35, 65, 95])
        session.add(BureauRecord(
            customer_id=customer.id, bureau="CIBIL", score=score,
            accounts_total=rng.randint(2, 11), accounts_open=rng.randint(1, 7),
            accounts_delinquent=1 if worst_dpd >= 30 else 0,
            worst_dpd_24m=worst_dpd, enquiries_6m=rng.randint(0, 8),
            oldest_account_months=rng.randint(14, 190),
            total_outstanding=round(rng.uniform(50_000, 2_400_000), 2),
            total_sanctioned=round(rng.uniform(200_000, 4_000_000), 2),
            revolving_utilisation_pct=round(rng.uniform(3, 95), 2),
            monthly_obligations=round(rng.uniform(0, 60_000), 2),
            write_offs=1 if score < 620 and rng.random() < 0.4 else 0,
            settled_accounts=1 if score < 650 and rng.random() < 0.3 else 0,
            pulled_at=now - timedelta(days=rng.randint(1, 20)),
            reference=f"CIBIL-{rng.randint(10**9, 10**10 - 1)}", source="database",
        ))
        bureau += 1

    applications = 0
    for index, profile in enumerate(_APPLICATION_PROFILES):
        if index >= len(customers):
            break
        customer = customers[index]
        # An application is only accepted from a verified customer.
        customer.kyc_status = "verified"
        customer.date_of_birth = date(1985 + index, 4, 12)
        session.add(BureauRecord(
            customer_id=customer.id, bureau="CIBIL", score=profile["score"],
            accounts_total=rng.randint(3, 9), accounts_open=rng.randint(2, 6),
            accounts_delinquent=1 if profile["dpd"] >= 30 else 0,
            worst_dpd_24m=profile["dpd"], enquiries_6m=profile["enquiries"],
            oldest_account_months=max(12, profile["months"]),
            total_outstanding=round(profile["income"] * 6, 2),
            total_sanctioned=round(profile["income"] * 12, 2),
            revolving_utilisation_pct=profile["utilisation"],
            monthly_obligations=round(profile["income"] * 0.12, 2),
            write_offs=profile.get("write_offs", 0),
            pulled_at=now - timedelta(days=rng.randint(1, 12)),
            reference=f"CIBIL-{rng.randint(10**9, 10**10 - 1)}", source="database",
        ))
        bureau += 1
        session.add(CreditApplication(
            application_number=f"APP-{100001 + index}",
            customer_id=customer.id, product=profile["product"],
            requested_amount=float(profile["amount"]), tenure_months=profile["tenure"],
            purpose=profile["purpose"],
            declared_monthly_income=float(profile["income"]),
            declared_monthly_expenses=float(profile["expenses"]),
            employment_type=profile["employment"], employment_months=profile["months"],
            collateral_type=profile.get("collateral_type"),
            collateral_value=float(profile.get("collateral_value", 0)),
            channel="branch", status="submitted",
            submitted_at=now - timedelta(days=rng.randint(0, 6)),
        ))
        applications += 1

    await session.flush()
    return {"bureau_records": bureau, "credit_applications": applications}


async def _seed_collections_book(session: AsyncSession, customers: list[Customer],
                                 now: datetime) -> dict[str, Any]:
    """Open a case for every delinquent loan, with a spread of the controls that
    constrain treatment: a cease-contact instruction, an open dispute, a withdrawn
    consent."""
    from app.tools.collections import asset_classification, bucket_for_dpd

    loans = (await session.execute(
        select(Loan).where(Loan.days_past_due > 0).order_by(Loan.days_past_due.desc())
    )).scalars().all()

    cases = 0
    # Scarcest controls first: a small seeded book must still exercise each one.
    controls = ["", "cease", "dispute", "no_consent", ""]
    for index, loan in enumerate(loans):
        classification = asset_classification(loan.days_past_due)
        control = controls[index % len(controls)]
        case = DelinquencyCase(
            case_number=f"COL-{100001 + index}",
            customer_id=loan.customer_id, facility_type="loan", facility_id=loan.id,
            facility_reference=loan.loan_number, outstanding=loan.outstanding,
            amount_overdue=round(loan.emi_amount * max(1, loan.days_past_due // 30), 2),
            minimum_due=loan.emi_amount, days_past_due=loan.days_past_due,
            bucket=bucket_for_dpd(loan.days_past_due),
            asset_classification=classification["classification"],
            status="open",
            contact_consent=control != "no_consent",
            cease_contact=control == "cease",
            dispute_open=control == "dispute",
            opened_at=now - timedelta(days=min(loan.days_past_due, 90)),
        )
        session.add(case)
        cases += 1

    await session.flush()
    return {"delinquency_cases": cases}
