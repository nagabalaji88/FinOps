"""Platform settings an operator can change without a deploy.

Holds the model selections the admin view writes. Credentials do not go here -- those keep
using ``stored_secrets``, which is encrypted at rest.

Revision ID: 4c81f0a7b2d9
Revises: 937904a33105
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "4c81f0a7b2d9"
down_revision: Union[str, None] = "937904a33105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_platform_settings_key"), "platform_settings", ["key"], unique=True)
    # TimestampMixin indexes created_at on every table; omitting it here leaves the schema
    # drifting from the models, which `alembic check` fails on.
    op.create_index(
        op.f("ix_platform_settings_created_at"), "platform_settings", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_platform_settings_created_at"), table_name="platform_settings")
    op.drop_index(op.f("ix_platform_settings_key"), table_name="platform_settings")
    op.drop_table("platform_settings")
