"""Core banking system of record that the agents' tools read and write.

These are real operational tables (customers, accounts, transactions, cases). The
agents query them through tools; nothing is synthesised at request time.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDMixin


class Customer(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "customers"

    customer_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True, default=None)
    phone: Mapped[str | None] = mapped_column(String(40), index=True, default=None)
    date_of_birth: Mapped[date | None] = mapped_column(Date, default=None)
    nationality: Mapped[str] = mapped_column(String(80), default="IN")
    address_line1: Mapped[str | None] = mapped_column(String(255), default=None)
    address_city: Mapped[str | None] = mapped_column(String(120), default=None)
    address_state: Mapped[str | None] = mapped_column(String(120), default=None)
    address_postcode: Mapped[str | None] = mapped_column(String(24), default=None)
    address_country: Mapped[str] = mapped_column(String(80), default="India")
    segment: Mapped[str] = mapped_column(String(40), default="retail")  # retail|premier|private|sme
    kyc_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    risk_rating: Mapped[str] = mapped_column(String(24), default="low", index=True)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    pep_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    sanctions_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    onboarded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    relationship_manager: Mapped[str | None] = mapped_column(String(160), default=None)
    # security material used by the Customer Service agent's authentication tool
    auth_pin_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    security_question: Mapped[str | None] = mapped_column(String(255), default=None)
    security_answer_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    failed_auth_attempts: Mapped[int] = mapped_column(Integer, default=0)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    accounts: Mapped[list[Account]] = relationship(back_populates="customer")


class Account(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "accounts"

    account_number: Mapped[str] = mapped_column(String(34), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    account_type: Mapped[str] = mapped_column(String(40), default="savings")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    available_balance: Mapped[float] = mapped_column(Float, default=0.0)
    hold_amount: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    branch_code: Mapped[str | None] = mapped_column(String(24), default=None)
    ifsc: Mapped[str | None] = mapped_column(String(24), default=None)
    opened_on: Mapped[date | None] = mapped_column(Date, default=None)
    overdraft_limit: Mapped[float] = mapped_column(Float, default=0.0)

    customer: Mapped[Customer] = relationship(back_populates="accounts")


class Transaction(Base, UUIDMixin):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_txn_account_time", "account_id", "booked_at"),
        Index("ix_txn_flags", "is_flagged", "booked_at"),
    )

    reference: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[str] = mapped_column(String(36), index=True)
    booked_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    direction: Mapped[str] = mapped_column(String(8))  # debit|credit
    channel: Mapped[str] = mapped_column(String(32), default="upi")
    merchant: Mapped[str | None] = mapped_column(String(200), default=None)
    category: Mapped[str | None] = mapped_column(String(80), index=True, default=None)
    description: Mapped[str] = mapped_column(String(400), default="")
    counterparty_name: Mapped[str | None] = mapped_column(String(200), default=None)
    counterparty_account: Mapped[str | None] = mapped_column(String(48), default=None)
    counterparty_bank: Mapped[str | None] = mapped_column(String(120), default=None)
    country: Mapped[str] = mapped_column(String(3), default="IND")
    status: Mapped[str] = mapped_column(String(24), default="posted")
    balance_after: Mapped[float | None] = mapped_column(Float, default=None)
    is_flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    device_id: Mapped[str | None] = mapped_column(String(80), default=None)
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Card(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "cards"

    card_number_masked: Mapped[str] = mapped_column(String(24), index=True)
    card_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str | None] = mapped_column(String(36), default=None)
    card_type: Mapped[str] = mapped_column(String(24), default="credit")
    network: Mapped[str] = mapped_column(String(24), default="visa")
    product_name: Mapped[str] = mapped_column(String(120), default="Signature Rewards")
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    credit_limit: Mapped[float] = mapped_column(Float, default=0.0)
    available_credit: Mapped[float] = mapped_column(Float, default=0.0)
    current_balance: Mapped[float] = mapped_column(Float, default=0.0)
    minimum_due: Mapped[float] = mapped_column(Float, default=0.0)
    statement_date: Mapped[date | None] = mapped_column(Date, default=None)
    due_date: Mapped[date | None] = mapped_column(Date, default=None)
    apr: Mapped[float] = mapped_column(Float, default=36.0)
    reward_points: Mapped[int] = mapped_column(Integer, default=0)
    expiry: Mapped[str | None] = mapped_column(String(7), default=None)
    issued_on: Mapped[date | None] = mapped_column(Date, default=None)


class Loan(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "loans"

    loan_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    loan_type: Mapped[str] = mapped_column(String(40), default="personal")
    principal: Mapped[float] = mapped_column(Float, default=0.0)
    outstanding: Mapped[float] = mapped_column(Float, default=0.0)
    interest_rate: Mapped[float] = mapped_column(Float, default=10.5)
    tenure_months: Mapped[int] = mapped_column(Integer, default=60)
    emi_amount: Mapped[float] = mapped_column(Float, default=0.0)
    emis_paid: Mapped[int] = mapped_column(Integer, default=0)
    next_due_date: Mapped[date | None] = mapped_column(Date, default=None)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    days_past_due: Mapped[int] = mapped_column(Integer, default=0)
    disbursed_on: Mapped[date | None] = mapped_column(Date, default=None)
    collateral: Mapped[str | None] = mapped_column(String(255), default=None)


class Ticket(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "tickets"

    ticket_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    subject: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(60), default="general")
    priority: Mapped[str] = mapped_column(String(16), default="medium", index=True)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    channel: Mapped[str] = mapped_column(String(32), default="ai_agent")
    sentiment: Mapped[str | None] = mapped_column(String(24), default=None)
    assigned_team: Mapped[str | None] = mapped_column(String(120), default=None)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    escalation_reason: Mapped[str | None] = mapped_column(Text, default=None)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    sla_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    notes: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)


class FAQEntry(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "faq_entries"

    question: Mapped[str] = mapped_column(String(500), index=True)
    answer: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(80), default="general", index=True)
    keywords: Mapped[list[str]] = mapped_column(JSONType, default=list)
    product: Mapped[str | None] = mapped_column(String(80), default=None)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)


# --- KYC --------------------------------------------------------------------
class KycCase(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "kyc_cases"

    case_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    applicant_name: Mapped[str] = mapped_column(String(200))
    applicant_email: Mapped[str | None] = mapped_column(String(255), default=None)
    applicant_phone: Mapped[str | None] = mapped_column(String(40), default=None)
    date_of_birth: Mapped[date | None] = mapped_column(Date, default=None)
    nationality: Mapped[str] = mapped_column(String(80), default="IN")
    declared_address: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="in_progress", index=True)
    decision: Mapped[str | None] = mapped_column(String(32), default=None)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    risk_band: Mapped[str] = mapped_column(String(16), default="low")
    sanctions_hits: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    pep_hits: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    face_match_score: Mapped[float | None] = mapped_column(Float, default=None)
    address_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    report_uri: Mapped[str | None] = mapped_column(String(1000), default=None)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    decided_by: Mapped[str | None] = mapped_column(String(255), default=None)

    documents: Mapped[list[KycDocument]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )


class KycDocument(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "kyc_documents"

    case_id: Mapped[str] = mapped_column(ForeignKey("kyc_cases.id", ondelete="CASCADE"), index=True)
    doc_type: Mapped[str] = mapped_column(String(40))  # passport|pan|aadhaar|selfie|utility_bill
    file_name: Mapped[str] = mapped_column(String(255), default="")
    mime_type: Mapped[str] = mapped_column(String(80), default="image/jpeg")
    artifact_uri: Mapped[str | None] = mapped_column(String(1000), default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    ocr_engine: Mapped[str | None] = mapped_column(String(60), default=None)
    ocr_text: Mapped[str | None] = mapped_column(Text, default=None)
    extracted_fields: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    classification_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    verification_status: Mapped[str] = mapped_column(String(32), default="pending")
    verification_notes: Mapped[list[str]] = mapped_column(JSONType, default=list)

    case: Mapped[KycCase] = relationship(back_populates="documents")


class SanctionsEntry(Base, UUIDMixin, TimestampMixin):
    """Watchlist records loaded from OFAC/UN/EU/internal lists."""

    __tablename__ = "sanctions_entries"
    __table_args__ = (Index("ix_sanctions_name_norm", "normalised_name"),)

    list_name: Mapped[str] = mapped_column(String(60), index=True)  # OFAC_SDN|UN|EU|RBI|PEP|INTERNAL
    entry_type: Mapped[str] = mapped_column(String(24), default="individual")
    full_name: Mapped[str] = mapped_column(String(300), index=True)
    normalised_name: Mapped[str] = mapped_column(String(300), index=True)
    aliases: Mapped[list[str]] = mapped_column(JSONType, default=list)
    date_of_birth: Mapped[str | None] = mapped_column(String(40), default=None)
    nationality: Mapped[str | None] = mapped_column(String(80), default=None)
    program: Mapped[str | None] = mapped_column(String(120), default=None)
    position: Mapped[str | None] = mapped_column(String(200), default=None)
    is_pep: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_url: Mapped[str | None] = mapped_column(String(500), default=None)
    listed_on: Mapped[date | None] = mapped_column(Date, default=None)
    remarks: Mapped[str | None] = mapped_column(Text, default=None)


# --- AML --------------------------------------------------------------------
class AmlAlert(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "aml_alerts"

    alert_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(String(36), index=True)
    account_id: Mapped[str | None] = mapped_column(String(36), default=None)
    rule_code: Mapped[str] = mapped_column(String(40), index=True)
    rule_name: Mapped[str] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(16), default="medium", index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    transaction_ids: Mapped[list[str]] = mapped_column(JSONType, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    detected_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    case_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)


class AmlCase(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "aml_cases"

    case_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    typologies: Mapped[list[str]] = mapped_column(JSONType, default=list)
    alert_ids: Mapped[list[str]] = mapped_column(JSONType, default=list)
    timeline: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    narrative: Mapped[str | None] = mapped_column(Text, default=None)
    investigator: Mapped[str | None] = mapped_column(String(255), default=None)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    sar_filed: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    disposition: Mapped[str | None] = mapped_column(String(60), default=None)


class SarReport(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "sar_reports"

    sar_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    customer_id: Mapped[str] = mapped_column(String(36), index=True)
    filing_institution: Mapped[str] = mapped_column(String(200), default="FinOps Bank Ltd")
    regulator: Mapped[str] = mapped_column(String(80), default="FIU-IND")
    suspicious_amount: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    activity_start: Mapped[date | None] = mapped_column(Date, default=None)
    activity_end: Mapped[date | None] = mapped_column(Date, default=None)
    typologies: Mapped[list[str]] = mapped_column(JSONType, default=list)
    narrative: Mapped[str] = mapped_column(Text, default="")
    supporting_transactions: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    artifact_uri: Mapped[str | None] = mapped_column(String(1000), default=None)
    approved_by: Mapped[str | None] = mapped_column(String(255), default=None)
    filed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    execution_id: Mapped[str | None] = mapped_column(String(36), default=None)


# --- Investment research ----------------------------------------------------
class Security(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "securities"

    symbol: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    exchange: Mapped[str] = mapped_column(String(24), default="NSE")
    asset_class: Mapped[str] = mapped_column(String(32), default="equity")
    sector: Mapped[str | None] = mapped_column(String(80), index=True, default=None)
    industry: Mapped[str | None] = mapped_column(String(120), default=None)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    country: Mapped[str] = mapped_column(String(3), default="IND")
    cik: Mapped[str | None] = mapped_column(String(16), default=None)
    isin: Mapped[str | None] = mapped_column(String(16), default=None)
    last_price: Mapped[float | None] = mapped_column(Float, default=None)
    last_price_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    fundamentals: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Portfolio(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "portfolios"

    portfolio_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    name: Mapped[str] = mapped_column(String(200))
    strategy: Mapped[str] = mapped_column(String(80), default="balanced")
    base_currency: Mapped[str] = mapped_column(String(3), default="INR")
    cash_balance: Mapped[float] = mapped_column(Float, default=0.0)
    benchmark: Mapped[str] = mapped_column(String(60), default="NIFTY50")
    risk_profile: Mapped[str] = mapped_column(String(24), default="moderate")
    mandate: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    holdings: Mapped[list[Holding]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )


class Holding(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "holdings"

    portfolio_id: Mapped[str] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    average_cost: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    asset_class: Mapped[str] = mapped_column(String(32), default="equity")
    sector: Mapped[str | None] = mapped_column(String(80), default=None)
    opened_on: Mapped[date | None] = mapped_column(Date, default=None)

    portfolio: Mapped[Portfolio] = relationship(back_populates="holdings")


class PriceBar(Base, UUIDMixin):
    __tablename__ = "price_bars"
    __table_args__ = (Index("ix_price_symbol_date", "symbol", "bar_date", unique=True),)

    symbol: Mapped[str] = mapped_column(String(24), index=True)
    bar_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(40), default="internal")


class ResearchNote(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "research_notes"

    symbol: Mapped[str | None] = mapped_column(String(24), index=True, default=None)
    title: Mapped[str] = mapped_column(String(300))
    thesis: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str | None] = mapped_column(String(24), default=None)
    target_price: Mapped[float | None] = mapped_column(Float, default=None)
    horizon_months: Mapped[int] = mapped_column(Integer, default=12)
    conviction: Mapped[str] = mapped_column(String(16), default="medium")
    risks: Mapped[list[str]] = mapped_column(JSONType, default=list)
    catalysts: Mapped[list[str]] = mapped_column(JSONType, default=list)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    analyst: Mapped[str | None] = mapped_column(String(160), default=None)
    execution_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    artifact_uri: Mapped[str | None] = mapped_column(String(1000), default=None)
