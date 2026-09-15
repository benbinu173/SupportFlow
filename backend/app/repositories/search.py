"""Search predicates - the one place a user-supplied term becomes SQL.

Every search in the API is built here, for the same reason every tenant query starts at
`TenantScopedRepository._select`: isolation and search are both properties that hold
only if they hold *everywhere*, and the way either breaks is a second implementation
written from memory. A `q` parameter assembled at a route is a `q` parameter that has
forgotten the escape, or the tenant arm, or the internal-note filter.

Three things this module is careful about:

**Search narrows; it never widens.** `ticket_search_predicate` returns one disjunction,
and every caller ANDs it with the caller's scope predicate. There is no code path here
that returns a predicate which could be used *instead of* a scope. That is the property
`tests/api/test_search.py` asserts directly, because "a query parameter that grants
access" is the classic form of this bug.

**A search term is data, not syntax.** `escape_like` handles the LIKE metacharacters,
and `websearch_to_tsquery` parses the full-text term on the server rather than by
string-splicing - so a term containing a quote, a backslash, or a lone `-` is a term,
not a syntax error or an injection. `websearch_to_tsquery` specifically, rather than
`plainto_tsquery`, because it cannot raise on malformed input and it understands the
quoted phrases a support agent actually types.

**The index expression is imported, not restated.** `TICKET_FTS_EXPRESSION` and
`MESSAGE_FTS_EXPRESSION` are defined by the models that declare the indexes over them.
An expression index is matched to a query by the expression, so a query that spelled it
slightly differently - `to_tsvector('english', subject || ' ' || description)`, the
prettier form - would silently scan instead of seeking. Sharing the constant makes that
divergence impossible rather than merely discouraged.
"""

from typing import Any

from sqlalchemy import (
    ColumnClause,
    ColumnElement,
    Text,
    cast,
    exists,
    func,
    literal,
    literal_column,
    or_,
    select,
)

from app.models.customer import Customer
from app.models.message import MESSAGE_FTS_EXPRESSION, Message
from app.models.ticket import TICKET_FTS_EXPRESSION, Ticket

# The text-search configuration, as an expression rather than a string.
#
# `literal_column` and not `"'english'::regconfig"` — SQLAlchemy's PostgreSQL dialect
# wraps `websearch_to_tsquery` in a construct that coerces its first argument to a
# `regconfig`, and a Python string coerced that way becomes a *bind parameter* of type
# REGCONFIG. That is not a cosmetic distinction: a bind parameter cannot be rendered by
# `literal_binds`, so the whole statement becomes uncompilable outside a live
# connection - which is exactly how the Phase N `EXPLAIN` script found it. A
# `literal_column` is already an expression, so the coercion leaves it alone and it
# renders verbatim, which is also what matching the index expression requires.
_TS_CONFIG: ColumnClause[Any] = literal_column("'english'::regconfig")


def escape_like(term: str) -> str:
    """Escape the wildcards in a user-supplied search term.

    SQLAlchemy parameterizes the value, so this is not about injection — a `%` in a
    bind parameter is data and nothing more. It is about *meaning*: `%` and `_` are
    LIKE metacharacters, so a customer searching for `50%` would otherwise match every
    record in the table, and one searching for `a_b` would match `aXb`. The backslash
    is escaped first, since it is the escape character itself and escaping it later
    would double up on the escapes added before it.

    Moved here from `customer_repository.py` in Phase N, unchanged. It was already
    generic - it takes a string and returns a string and knows nothing about customers -
    and the ticket search needed the same function, which is the moment a helper stops
    belonging to one caller. Every caller must also pass `escape="\\"` to `ilike`, or
    the backslashes this adds are treated as literal characters instead of escapes.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_pattern(term: str) -> str:
    """A substring pattern for `ILIKE ... ESCAPE '\\'`, with the term escaped.

    The one place the surrounding `%` characters are added, so no caller can produce a
    pattern that anchors differently - or forgets the escape on one of its two arms.
    """
    return f"%{escape_like(term.strip())}%"


def _fts(expression: str, term: str) -> ColumnElement[bool]:
    """`<expression> @@ websearch_to_tsquery('english', :term)`, in stored form.

    `literal_column` rather than `text()` because the left side is used as an *operand*
    rather than as a whole fragment: `text()` produces a `TextClause`, which SQLAlchemy
    accepts in a `WHERE` at runtime but does not type as a boolean expression, so mypy
    is right to object to it and a `cast` would be a lie in the annotation to silence a
    complaint that is really about which construct was chosen. `literal_column` is
    inserted verbatim with no quoting or processing — which is exactly what an index
    expression needs — and `@@` is spelled with `.op()` because SQLAlchemy has no
    dedicated operator for a tsvector match.

    The expression is interpolated rather than bound because it *is* SQL: it names
    columns of the table being queried. It is never client input — the two callers below
    pass module-level constants declared by the models that own the indexes.
    """
    return literal_column(expression).op("@@")(func.websearch_to_tsquery(_TS_CONFIG, term))


def ticket_search_predicate(term: str, *, include_internal: bool) -> ColumnElement[bool]:
    """Spec §14's search, as one predicate over the four fields it names.

    A ticket matches when **any** of these hold:

    * its subject or description matches the full-text term - served by `ix_tickets_fts`;
    * its number, read as text, equals the term exactly - so `q=1042` finds ticket #1042,
      and matches `1042` and not `10420`;
    * one of its messages matches, subject to `include_internal`;
    * its customer's name or email matches as a substring - served by the two trigram
      indexes, since a B-tree cannot answer `%term%`.

    **`include_internal` is the phase's one real hazard.** A customer holds
    `MESSAGE_LIST` and reaches their own ticket, so without this the message arm would
    let them find a ticket by typing a phrase that appears only in a note they cannot
    read - search becoming the way around a filter that every other route applies. The
    caller passes `context.has(Permission.MESSAGE_READ_INTERNAL)`, so the arm is dropped
    entirely for a portal account rather than filtered afterwards.

    Dropped, not filtered: an internal-note row that is fetched and then discarded is a
    row that was read, and the version of this bug that matters is the one where the
    filter is applied one query too late.

    The customer arm is scoped to the ticket's own organization as well as to its
    customer id. The id is already a foreign key so the second condition cannot fail
    today; it is written out because the `EXISTS` is a correlated subquery that
    `TenantScopedRepository` cannot reach into, and the module's contract is that a
    tenant predicate is either applied structurally or written in as many words.
    """
    term = term.strip()
    pattern = like_pattern(term)

    # The ticket's own text, in the index's stored form.
    own_text = _fts(TICKET_FTS_EXPRESSION, term)

    # The number arm. `cast(..., Text)` rather than a Python `int(term)` parse: a term
    # that is not a number is not an error, it is simply a term that matches no number,
    # and parsing would turn `q=abc` into a 422 for a search that has three other arms
    # it could have matched in. `CAST(number AS TEXT)` is the parse tree `number::text`
    # denotes, so this is the plan the same cast would get.
    by_number = cast(Ticket.number, Text()) == term

    message_arms = [
        Message.ticket_id == Ticket.id,
        Message.organization_id == Ticket.organization_id,
        _fts(MESSAGE_FTS_EXPRESSION, term),
    ]
    if not include_internal:
        message_arms.append(Message.is_internal.is_(False))

    by_message = exists(
        select(literal(1))
        .select_from(Message)
        .where(*message_arms)
        # Explicit rather than relying on auto-correlation: the subquery must see the
        # *enclosing* ticket's id rather than its own copy of the table, and a correlated
        # subquery that silently became an uncorrelated one would still parse.
        .correlate(Ticket)
    )

    by_customer = exists(
        select(literal(1))
        .select_from(Customer)
        .where(
            Customer.id == Ticket.customer_id,
            Customer.organization_id == Ticket.organization_id,
            or_(
                Customer.name.ilike(pattern, escape="\\"),
                Customer.email.ilike(pattern, escape="\\"),
            ),
        )
        .correlate(Ticket)
    )

    return or_(own_text, by_number, by_message, by_customer)
