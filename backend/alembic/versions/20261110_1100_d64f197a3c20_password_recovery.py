"""Account recovery: credential versions and the password-reset audit action.

Revision ID: d64f197a3c20
Revises: 51815e0bbd16
"""

import sqlalchemy as sa
from alembic import op

revision = "d64f197a3c20"
down_revision = "51815e0bbd16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "refresh_tokens",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'password_reset'")


def downgrade() -> None:
    op.drop_column("refresh_tokens", "token_version")
    op.drop_column("users", "token_version")
    # PostgreSQL cannot remove a single enum label without rewriting audit history.
    # Keeping it matches the earlier enum migration; upgrade remains idempotent.
