"""notification type: sla_breached

Adds `'sla_breached'` to the `notification_type` PostgreSQL enum (Phase Q).

**This migration is hand-written, and that is the important thing about it.** Alembic's
autogenerate does not detect enum *member* additions. `NotificationType` gained a member,
the model module changed, and `alembic revision --autogenerate` against it produces an
empty revision with no error — so the failure mode is a Phase that ships, deploys, and
then raises `InvalidTextRepresentation` the first time an SLA breach notification is
written, in the one code path (a scheduled sweep) nobody is watching. `alembic check`
after this revision is the mechanical proof that nothing else was missed.

**Why the value exists at all.** §26 names "SLA warning" and not "SLA breach". Phase Q
decided the breach is a second alert rather than a correction of the first, which needs a
notification type of its own; `app/services/notification_service.py` records the argument.
Reusing `'sla_warning'` for both would leave a client unable to tell "you have 20 minutes"
from "you are 40 minutes late", and those call for different responses.

**The new value lands at the end of the enum, not next to `'sla_warning'`.** PostgreSQL
appends; it cannot insert into the middle without rewriting the type. The declaration
order in `app/models/enums.py` therefore differs from the database's, and that is
harmless because nothing orders by this column — notifications are ordered by `created_at`
(ADR-024). Worth knowing before somebody reads the enum in `psql` and finds the order
surprising.

**A transaction, and why that is safe here.** PostgreSQL 12 and later allow `ALTER TYPE …
ADD VALUE` inside a transaction block; the restriction is that the new value cannot be
*used* before that transaction commits. This migration does not use it — it adds no rows,
backfills nothing, and touches no table. The value is available to the next transaction
that runs, which is the first SLA broadcast.

**Irreversible, and the downgrade is a documented no-op rather than a refusal.** PostgreSQL
has no `ALTER TYPE … DROP VALUE`: removing an enum member requires recreating the type and
rewriting every column that uses it, which is a table rewrite this project is not going to
perform in a downgrade. The alternative — deleting every `'sla_breached'` notification row
and then recreating the type — destroys data to undo a schema change, and PostgreSQL would
reject the rewrite anyway while any surviving row still held the value.

So there is genuinely nothing to do, and `downgrade()` says so and returns. **This was a
`raise` in the first draft and that was wrong**, which is worth recording because the
reasoning is not obvious: the *sentence* was honest, but the effect was that `downgrade
base` — the command `tests/integration/test_migrations.py` runs to prove the schema can be
torn down and rebuilt — stopped working for every migration in the chain, in order to
report a fact about one value. A downgrade that cannot run is not a more honest downgrade;
it is a broken one. The honesty belongs in this docstring, where it costs nobody a working
teardown.

Nothing is left dangling by doing nothing: `downgrade base` drops the `notifications`
table, and an enum type is dropped with the table that used it. The value survives only
while the schema does.

**Which forces the upgrade to be idempotent, and that is the second half of this file.**
A downgrade that leaves the label behind means an `upgrade` that runs again will find it
there — so the statement is `ADD VALUE IF NOT EXISTS` and not `ADD VALUE`. Without it the
round-trip the previous two tests perform (`upgrade head`, `downgrade <earlier>`, `upgrade
head`) fails on the second upgrade with `DuplicateObject: enum label "sla_breached" already
exists`, in a database where nothing is actually wrong. That is not a hypothetical: the
suite caught exactly this, and it is the reason the pair of decisions — no-op downgrade and
idempotent upgrade — have to be made together. An irreversible step and a reversible-looking
one-liner are the same decision seen twice.

`IF NOT EXISTS` is also right on its own terms, independently of the downgrade: it makes
this migration safe to run against a database where somebody applied the `ALTER TYPE` by
hand while debugging, which is the kind of thing that otherwise fails a deploy at 2am for
a reason the error message does not explain.

Revision ID: 51815e0bbd16
Revises: a49a5939bf77
Create Date: 2026-09-16 09:41:02.118455

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "51815e0bbd16"
down_revision: str | Sequence[str] | None = "a49a5939bf77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUM_NAME = "notification_type"
NEW_VALUE = "sla_breached"


def upgrade() -> None:
    """Add the enum member, or find it already there.

    Written as raw SQL because Alembic has no operation for altering an enum — and because
    spelling it out is the point: `op.execute` on a statement this specific is reviewable,
    while a helper would hide the one thing a reader of this file needs to confirm.

    `IF NOT EXISTS` is load-bearing rather than defensive; the module docstring has the
    argument. The short version is that `downgrade` cannot remove the label, so an upgrade
    after a downgrade is a re-run and has to survive one.
    """
    op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{NEW_VALUE}'")


def downgrade() -> None:
    """Do nothing, and mean it. See the docstring.

    Not an oversight and not a stub: there is no statement to run. A reader who expected
    `op.execute("ALTER TYPE … DROP VALUE …")` here should find this sentence instead of
    concluding that somebody forgot.
    """
