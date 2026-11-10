"""Email verification for new accounts, with legacy access preserved.

Revision ID: e73a61b24f09
Revises: d64f197a3c20
"""

import sqlalchemy as sa
from alembic import op

revision = "e73a61b24f09"
down_revision = "d64f197a3c20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Existing users keep access. This is a compatibility baseline, not historical
    # evidence that they clicked a verification email. New rows have no default.
    op.execute("UPDATE users SET email_verified_at = CURRENT_TIMESTAMP")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'email_verified'")


def downgrade() -> None:
    op.drop_column("users", "email_verified_at")
    # PostgreSQL enum labels remain so historical audit rows can still be read.
