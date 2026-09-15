"""message full-text search index

Adds `ix_messages_fts`, a GIN index over `to_tsvector('english', body)`. Phase N is the
first phase that searches a message body (spec §14), and until it existed the `messages`
table had indexes on `ticket_id` and `created_at` and nothing on the column being
searched — so every message arm of every search was a sequential scan.

The expression is imported from `app.models.message` rather than copied, which is a
departure from how the baseline migration writes `ix_tickets_fts`. The baseline had to
restate its expression because the model had been corrected to match the catalog after
autogenerate had already run; here the constant is the single source for both, so a
migration that restated it could only drift. `MESSAGE_FTS_EXPRESSION` is what
`app/repositories/search.py` queries with, and an expression index is matched to a
query by the expression — a migration that spelled it differently from the query would
produce an index nothing ever uses, which is a failure that looks exactly like success.

Reversible: the downgrade drops the index and nothing else. No data is touched in either
direction, so the round-trip is exact.

Revision ID: e672102955a8
Revises: bd8e646eeff8
Create Date: 2026-09-15 10:04:49.380023

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.models.message import MESSAGE_FTS_EXPRESSION

# revision identifiers, used by Alembic.
revision: str = "e672102955a8"
down_revision: str | Sequence[str] | None = "bd8e646eeff8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the message-body full-text index."""
    op.create_index(
        "ix_messages_fts",
        "messages",
        [sa.literal_column(MESSAGE_FTS_EXPRESSION)],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    """Drop it.

    No `postgresql_using` here: it is an argument to `create_index`'s DDL, not part of an
    index's identity, and `DROP INDEX` does not take an access method. The baseline
    migration passes it on the tickets drop, which is harmless — Alembic ignores it —
    but it is noise that reads like it matters.
    """
    op.drop_index("ix_messages_fts", table_name="messages")
