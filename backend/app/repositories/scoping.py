"""Row scope — how many rows *within* a tenant an actor's query may reach.

Tenant isolation and row scope are different questions and this module answers only the
second. `TenantScopedRepository` guarantees an agent cannot see another organization's
tickets; this guarantees an agent cannot see a colleague's. Both are needed, and
conflating them is how a system ends up with a filter that looks like it does both.

Why a mapping and a predicate rather than a second set of routes
---------------------------------------------------------------
`docs/requirements.md` §3 qualifies several capabilities with `own` or `assigned`. The
capability is one — "view ticket detail" — and four roles hold it; what differs is how
many rows it reaches. That cannot be decided at the route, because at the route there
is no row yet. It is decided here, where the query is built.

The failure this module exists to prevent is the quiet one: a scope that fails *open*.
`WHERE customer_id = NULL` is not false, it is unknown — so it matches no rows, which
looks correct, until someone rewrites the predicate with an `or_` and it starts matching
every row in the organization. So an unresolvable scope is written out explicitly as
`false()`, and `tests/security/test_row_scopes.py` asserts the resulting empty page.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import ColumnElement, false, true
from sqlalchemy.orm import InstrumentedAttribute

from app.core.permissions import RowScope
from app.core.tenancy import TenantContext
from app.models.enums import UserRole


def row_scope_predicate(
    context: TenantContext,
    scopes: Mapping[UserRole, RowScope],
    *,
    owner_column: InstrumentedAttribute[Any],
    assignee_column: InstrumentedAttribute[Any],
) -> ColumnElement[bool]:
    """The predicate that narrows a query to the rows `context` may reach.

    Takes the columns rather than a model, so it applies to any resource that
    distinguishes "the party this belongs to" from "the staff member working on it" —
    which today is tickets, and will be attachments and notifications.

    `owner_column` is compared against the caller's `customer_id` and `assignee_column`
    against their `user_id`; both come from the authenticated user's row, so neither
    can be influenced by the request. `ORGANIZATION` adds no predicate at all, which is
    correct: the repository has already filtered to the tenant, and that filter is the
    one thing a caller cannot remove.
    """
    scope = context.scope_for(scopes)

    if scope is RowScope.ORGANIZATION:
        return true()

    if scope is RowScope.ASSIGNED:
        # `user_id` is always present — it is what authenticated the request — so this
        # branch has no unresolvable case. An agent with no tickets assigned gets an
        # empty page, which is a real and common state, not a failure.
        return assignee_column == context.user_id

    # RowScope.OWN. A portal caller with no linked customer record is refused
    # explicitly rather than left to SQL's three-valued logic. The distinction matters
    # because `NULL = NULL` being *unknown* is the reason this looks like it works: the
    # behaviour is right by accident, and would stop being right the moment the
    # predicate was composed differently.
    if context.customer_id is None:
        return false()

    return owner_column == context.customer_id
