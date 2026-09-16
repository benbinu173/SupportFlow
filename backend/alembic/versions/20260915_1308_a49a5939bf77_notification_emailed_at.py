"""notification emailed_at

Adds `notifications.emailed_at`, the flag that makes email delivery at-least-once
instead of at-least-once-and-possibly-duplicated (Phase P).

The notification row is written inside the request's transaction and the email is sent
later by a worker. That worker acknowledges its messages *after* the task finishes
(`task_acks_late`), which is what stops a killed process from silently losing an email —
and the price of that choice is redelivery: a worker that dies after sending but before
acknowledging is handed the same task again. `emailed_at` is what the task checks before
sending and stamps after, narrowing "any redelivery duplicates mail" to "a crash between
the send and this mark does".

It also gives the delivery backlog a query. `emailed_at IS NULL` is exactly the set of
notifications whose email never went out — whether because the broker was down when the
request committed, or because every retry failed — which is what a sweep would need.

Nullable with no default and no backfill: every existing row is a notification that was
written before email delivery existed, and NULL is the honest description of it.

Reversible: the downgrade drops the column and nothing else. No data is touched in either
direction, so the round-trip is exact.

Revision ID: a49a5939bf77
Revises: e672102955a8
Create Date: 2026-09-15 13:08:27.254376

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a49a5939bf77"
down_revision: str | Sequence[str] | None = "e672102955a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the delivery flag."""
    op.add_column(
        "notifications",
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the delivery flag.

    Dropping it forgets which notifications were emailed, not that they were. The rows
    themselves are unaffected, and the in-app notifications they represent are the
    product feature — the email was only ever a second way to learn about one.
    """
    op.drop_column("notifications", "emailed_at")
