"""Credit risk and collections tables

Adds the lending domain: credit applications, bureau records and decisions for the Credit
Risk agent, and delinquency cases, contact attempts, promises to pay and repayment plans
for the Collections agent.

Revision ID: a1b42d971924
Revises: e6e124c8f403
Create Date: 2026-08-09 00:34:45.942188+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

import app.db.base  # noqa: F401 - UTCDateTime is referenced by the column definitions

revision: str = 'a1b42d971924'
down_revision: Union[str, None] = 'e6e124c8f403'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('bureau_records',
    sa.Column('customer_id', sa.String(length=36), nullable=False),
    sa.Column('bureau', sa.String(length=40), nullable=False),
    sa.Column('score', sa.Integer(), nullable=False),
    sa.Column('score_scale_min', sa.Integer(), nullable=False),
    sa.Column('score_scale_max', sa.Integer(), nullable=False),
    sa.Column('accounts_total', sa.Integer(), nullable=False),
    sa.Column('accounts_open', sa.Integer(), nullable=False),
    sa.Column('accounts_delinquent', sa.Integer(), nullable=False),
    sa.Column('worst_dpd_24m', sa.Integer(), nullable=False),
    sa.Column('enquiries_6m', sa.Integer(), nullable=False),
    sa.Column('oldest_account_months', sa.Integer(), nullable=False),
    sa.Column('total_outstanding', sa.Float(), nullable=False),
    sa.Column('total_sanctioned', sa.Float(), nullable=False),
    sa.Column('revolving_utilisation_pct', sa.Float(), nullable=False),
    sa.Column('monthly_obligations', sa.Float(), nullable=False),
    sa.Column('write_offs', sa.Integer(), nullable=False),
    sa.Column('settled_accounts', sa.Integer(), nullable=False),
    sa.Column('pulled_at', app.db.base.UTCDateTime(timezone=True), nullable=False),
    sa.Column('reference', sa.String(length=64), nullable=True),
    sa.Column('source', sa.String(length=24), nullable=False),
    sa.Column('raw', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('bureau_records', schema=None) as batch_op:
        batch_op.create_index('ix_bureau_customer_time', ['customer_id', 'pulled_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_bureau_records_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_bureau_records_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_bureau_records_pulled_at'), ['pulled_at'], unique=False)

    op.create_table('credit_applications',
    sa.Column('application_number', sa.String(length=32), nullable=False),
    sa.Column('customer_id', sa.String(length=36), nullable=False),
    sa.Column('product', sa.String(length=40), nullable=False),
    sa.Column('requested_amount', sa.Float(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('tenure_months', sa.Integer(), nullable=False),
    sa.Column('purpose', sa.String(length=200), nullable=True),
    sa.Column('declared_monthly_income', sa.Float(), nullable=False),
    sa.Column('declared_monthly_expenses', sa.Float(), nullable=False),
    sa.Column('employment_type', sa.String(length=32), nullable=False),
    sa.Column('employment_months', sa.Integer(), nullable=False),
    sa.Column('collateral_type', sa.String(length=60), nullable=True),
    sa.Column('collateral_value', sa.Float(), nullable=False),
    sa.Column('co_applicant_id', sa.String(length=36), nullable=True),
    sa.Column('channel', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('submitted_at', app.db.base.UTCDateTime(timezone=True), nullable=True),
    sa.Column('attributes', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('credit_applications', schema=None) as batch_op:
        batch_op.create_index('ix_credit_app_status', ['status', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_credit_applications_application_number'), ['application_number'], unique=True)
        batch_op.create_index(batch_op.f('ix_credit_applications_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_credit_applications_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_credit_applications_status'), ['status'], unique=False)

    op.create_table('delinquency_cases',
    sa.Column('case_number', sa.String(length=32), nullable=False),
    sa.Column('customer_id', sa.String(length=36), nullable=False),
    sa.Column('facility_type', sa.String(length=24), nullable=False),
    sa.Column('facility_id', sa.String(length=36), nullable=False),
    sa.Column('facility_reference', sa.String(length=48), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('outstanding', sa.Float(), nullable=False),
    sa.Column('amount_overdue', sa.Float(), nullable=False),
    sa.Column('minimum_due', sa.Float(), nullable=False),
    sa.Column('days_past_due', sa.Integer(), nullable=False),
    sa.Column('bucket', sa.String(length=16), nullable=False),
    sa.Column('asset_classification', sa.String(length=24), nullable=False),
    sa.Column('strategy', sa.String(length=40), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('assigned_to', sa.String(length=160), nullable=True),
    sa.Column('last_contacted_at', app.db.base.UTCDateTime(timezone=True), nullable=True),
    sa.Column('next_action_at', app.db.base.UTCDateTime(timezone=True), nullable=True),
    sa.Column('contact_consent', sa.Boolean(), nullable=False),
    sa.Column('cease_contact', sa.Boolean(), nullable=False),
    sa.Column('dispute_open', sa.Boolean(), nullable=False),
    sa.Column('hardship_flag', sa.Boolean(), nullable=False),
    sa.Column('opened_at', app.db.base.UTCDateTime(timezone=True), nullable=True),
    sa.Column('resolved_at', app.db.base.UTCDateTime(timezone=True), nullable=True),
    sa.Column('attributes', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('delinquency_cases', schema=None) as batch_op:
        batch_op.create_index('ix_delinquency_bucket', ['bucket', 'status'], unique=False)
        batch_op.create_index(batch_op.f('ix_delinquency_cases_case_number'), ['case_number'], unique=True)
        batch_op.create_index(batch_op.f('ix_delinquency_cases_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_delinquency_cases_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delinquency_cases_days_past_due'), ['days_past_due'], unique=False)
        batch_op.create_index(batch_op.f('ix_delinquency_cases_facility_id'), ['facility_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_delinquency_cases_status'), ['status'], unique=False)

    op.create_table('contact_attempts',
    sa.Column('case_id', sa.String(length=36), nullable=False),
    sa.Column('customer_id', sa.String(length=36), nullable=False),
    sa.Column('channel', sa.String(length=24), nullable=False),
    sa.Column('direction', sa.String(length=12), nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('attempted_at', app.db.base.UTCDateTime(timezone=True), nullable=False),
    sa.Column('local_hour', sa.Integer(), nullable=False),
    sa.Column('agent_name', sa.String(length=160), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('execution_id', sa.String(length=36), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['case_id'], ['delinquency_cases.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('contact_attempts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_contact_attempts_attempted_at'), ['attempted_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_contact_attempts_case_id'), ['case_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_contact_attempts_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_contact_attempts_execution_id'), ['execution_id'], unique=False)
        batch_op.create_index('ix_contact_case_time', ['case_id', 'attempted_at'], unique=False)

    op.create_table('credit_decisions',
    sa.Column('application_id', sa.String(length=36), nullable=False),
    sa.Column('customer_id', sa.String(length=36), nullable=False),
    sa.Column('decision', sa.String(length=24), nullable=False),
    sa.Column('approved_amount', sa.Float(), nullable=False),
    sa.Column('approved_tenure_months', sa.Integer(), nullable=False),
    sa.Column('approved_rate_pct', sa.Float(), nullable=False),
    sa.Column('risk_grade', sa.String(length=8), nullable=True),
    sa.Column('probability_of_default', sa.Float(), nullable=False),
    sa.Column('loss_given_default', sa.Float(), nullable=False),
    sa.Column('exposure_at_default', sa.Float(), nullable=False),
    sa.Column('expected_loss', sa.Float(), nullable=False),
    sa.Column('risk_weighted_assets', sa.Float(), nullable=False),
    sa.Column('foir_pct', sa.Float(), nullable=False),
    sa.Column('reason_codes', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('conditions', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('policy_version', sa.String(length=24), nullable=False),
    sa.Column('model_version', sa.String(length=24), nullable=False),
    sa.Column('scorecard', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('decided_by', sa.String(length=160), nullable=True),
    sa.Column('execution_id', sa.String(length=36), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['credit_applications.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('credit_decisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_credit_decisions_application_id'), ['application_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_credit_decisions_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_credit_decisions_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_credit_decisions_decision'), ['decision'], unique=False)
        batch_op.create_index(batch_op.f('ix_credit_decisions_execution_id'), ['execution_id'], unique=False)

    op.create_table('promises_to_pay',
    sa.Column('case_id', sa.String(length=36), nullable=False),
    sa.Column('customer_id', sa.String(length=36), nullable=False),
    sa.Column('amount', sa.Float(), nullable=False),
    sa.Column('promised_date', sa.Date(), nullable=False),
    sa.Column('channel', sa.String(length=24), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('settled_amount', sa.Float(), nullable=False),
    sa.Column('settled_on', sa.Date(), nullable=True),
    sa.Column('captured_by', sa.String(length=160), nullable=True),
    sa.Column('execution_id', sa.String(length=36), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['case_id'], ['delinquency_cases.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('promises_to_pay', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_promises_to_pay_case_id'), ['case_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_promises_to_pay_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_promises_to_pay_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_promises_to_pay_execution_id'), ['execution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_promises_to_pay_promised_date'), ['promised_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_promises_to_pay_status'), ['status'], unique=False)

    op.create_table('repayment_plans',
    sa.Column('plan_number', sa.String(length=32), nullable=False),
    sa.Column('case_id', sa.String(length=36), nullable=False),
    sa.Column('customer_id', sa.String(length=36), nullable=False),
    sa.Column('plan_type', sa.String(length=40), nullable=False),
    sa.Column('instalment_amount', sa.Float(), nullable=False),
    sa.Column('instalments', sa.Integer(), nullable=False),
    sa.Column('frequency', sa.String(length=16), nullable=False),
    sa.Column('first_payment_date', sa.Date(), nullable=True),
    sa.Column('total_payable', sa.Float(), nullable=False),
    sa.Column('concession_type', sa.String(length=40), nullable=True),
    sa.Column('concession_value', sa.Float(), nullable=False),
    sa.Column('affordability', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('approved_by', sa.String(length=160), nullable=True),
    sa.Column('execution_id', sa.String(length=36), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', app.db.base.UTCDateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['case_id'], ['delinquency_cases.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('repayment_plans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_repayment_plans_case_id'), ['case_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_repayment_plans_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_repayment_plans_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_repayment_plans_execution_id'), ['execution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_repayment_plans_plan_number'), ['plan_number'], unique=True)
        batch_op.create_index(batch_op.f('ix_repayment_plans_status'), ['status'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('repayment_plans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_repayment_plans_status'))
        batch_op.drop_index(batch_op.f('ix_repayment_plans_plan_number'))
        batch_op.drop_index(batch_op.f('ix_repayment_plans_execution_id'))
        batch_op.drop_index(batch_op.f('ix_repayment_plans_customer_id'))
        batch_op.drop_index(batch_op.f('ix_repayment_plans_created_at'))
        batch_op.drop_index(batch_op.f('ix_repayment_plans_case_id'))

    op.drop_table('repayment_plans')
    with op.batch_alter_table('promises_to_pay', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_promises_to_pay_status'))
        batch_op.drop_index(batch_op.f('ix_promises_to_pay_promised_date'))
        batch_op.drop_index(batch_op.f('ix_promises_to_pay_execution_id'))
        batch_op.drop_index(batch_op.f('ix_promises_to_pay_customer_id'))
        batch_op.drop_index(batch_op.f('ix_promises_to_pay_created_at'))
        batch_op.drop_index(batch_op.f('ix_promises_to_pay_case_id'))

    op.drop_table('promises_to_pay')
    with op.batch_alter_table('credit_decisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_credit_decisions_execution_id'))
        batch_op.drop_index(batch_op.f('ix_credit_decisions_decision'))
        batch_op.drop_index(batch_op.f('ix_credit_decisions_customer_id'))
        batch_op.drop_index(batch_op.f('ix_credit_decisions_created_at'))
        batch_op.drop_index(batch_op.f('ix_credit_decisions_application_id'))

    op.drop_table('credit_decisions')
    with op.batch_alter_table('contact_attempts', schema=None) as batch_op:
        batch_op.drop_index('ix_contact_case_time')
        batch_op.drop_index(batch_op.f('ix_contact_attempts_execution_id'))
        batch_op.drop_index(batch_op.f('ix_contact_attempts_customer_id'))
        batch_op.drop_index(batch_op.f('ix_contact_attempts_case_id'))
        batch_op.drop_index(batch_op.f('ix_contact_attempts_attempted_at'))

    op.drop_table('contact_attempts')
    with op.batch_alter_table('delinquency_cases', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_delinquency_cases_status'))
        batch_op.drop_index(batch_op.f('ix_delinquency_cases_facility_id'))
        batch_op.drop_index(batch_op.f('ix_delinquency_cases_days_past_due'))
        batch_op.drop_index(batch_op.f('ix_delinquency_cases_customer_id'))
        batch_op.drop_index(batch_op.f('ix_delinquency_cases_created_at'))
        batch_op.drop_index(batch_op.f('ix_delinquency_cases_case_number'))
        batch_op.drop_index('ix_delinquency_bucket')

    op.drop_table('delinquency_cases')
    with op.batch_alter_table('credit_applications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_credit_applications_status'))
        batch_op.drop_index(batch_op.f('ix_credit_applications_customer_id'))
        batch_op.drop_index(batch_op.f('ix_credit_applications_created_at'))
        batch_op.drop_index(batch_op.f('ix_credit_applications_application_number'))
        batch_op.drop_index('ix_credit_app_status')

    op.drop_table('credit_applications')
    with op.batch_alter_table('bureau_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bureau_records_pulled_at'))
        batch_op.drop_index(batch_op.f('ix_bureau_records_customer_id'))
        batch_op.drop_index(batch_op.f('ix_bureau_records_created_at'))
        batch_op.drop_index('ix_bureau_customer_time')

    op.drop_table('bureau_records')
