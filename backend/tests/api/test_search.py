"""Phase N — search, filtering, and sorting on the two list routes.

Spec §14 asks for search across ticket number, subject, description, customer name,
customer email, and message content, with filtering, sorting, and date ranges. No new
routes were added for it: `GET /tickets` and `GET /customers` gained parameters. That
makes this file the whole of Phase N's surface, so it is written around the four claims
that would be cheapest to get wrong rather than around the parameter list.

**Search narrows and never widens.** `q` is a disjunction with four arms, and it is
ANDed with the caller's row scope. The failure mode is obvious once named: a query
parameter that can return a row the caller could not otherwise reach is a query
parameter that grants access. `test_search_cannot_widen_an_agents_row_scope` states it
for the row scope.

**An internal note's words are internal.** A customer holds `MESSAGE_LIST` and reaches
their own ticket, so the message arm of the search would otherwise let them find a
ticket by typing a phrase that appears only in a note they cannot read — search
becoming the way around the filter every other route applies. This is the phase's one
real hazard, and it is asserted from both sides: the customer must not find it, the
agent must.

**LIKE metacharacters are data.** A term is not a pattern. `%` and `_` in a search box
are characters a customer typed, not instructions.

**A page boundary is stable.** Sorting by anything but a unique key means offset
pagination repeats and skips rows, and that only shows up under load. Asserted by
paging over keys that are deliberately all equal.

Terms are searched with `q=`, not `q=%`-style, and the tests use words that appear
nowhere else in the fixture data, because a term that also matches the default
description would pass for the wrong reason.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import CUSTOMERS, TICKETS

pytestmark = pytest.mark.integration


def search_tickets(org: Any, **params: Any) -> list[dict[str, Any]]:
    """`GET /tickets` with the given query parameters, asserted to have succeeded."""
    response = org.get(TICKETS, params=params)
    assert response.status_code == 200, response.text
    return list(response.json())


def search_customers(org: Any, **params: Any) -> list[dict[str, Any]]:
    """`GET /customers` with the given query parameters."""
    response = org.get(CUSTOMERS, params=params)
    assert response.status_code == 200, response.text
    return list(response.json())


def ids(rows: list[dict[str, Any]]) -> set[str]:
    return {row["id"] for row in rows}


def post_message(org: Any, ticket_id: str, body: str) -> dict[str, Any]:
    """A customer-facing reply on the ticket."""
    response = org.post(f"{TICKETS}/{ticket_id}/messages", json={"body": body})
    assert response.status_code == 201, response.text
    return dict(response.json())


def post_note(org: Any, ticket_id: str, body: str) -> dict[str, Any]:
    """A staff-only internal note on the ticket."""
    response = org.post(f"{TICKETS}/{ticket_id}/notes", json={"body": body})
    assert response.status_code == 201, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# The four arms of the predicate, one at a time
# ---------------------------------------------------------------------------


def test_a_subject_word_finds_the_ticket(client: TestClient, register_org: Any) -> None:
    """The full-text arm, over the subject."""
    org = register_org()
    customer = org.add_customer()
    wanted = org.add_ticket(customer["id"], subject="The telescope array is misaligned")
    org.add_ticket(customer["id"], subject="Something unrelated")

    found = search_tickets(org, q="telescope")

    assert ids(found) == {wanted["id"]}


def test_a_description_word_finds_the_ticket(client: TestClient, register_org: Any) -> None:
    """The same arm over the description, which is the column a customer writes into."""
    org = register_org()
    customer = org.add_customer()
    wanted = org.add_ticket(
        customer["id"], subject="Urgent", description="The dome shutter will not retract."
    )
    org.add_ticket(customer["id"], subject="Unrelated", description="Nothing to report.")

    found = search_tickets(org, q="shutter")

    assert ids(found) == {wanted["id"]}


def test_a_ticket_number_finds_that_ticket_and_not_its_neighbours(
    client: TestClient, register_org: Any
) -> None:
    """`q=1` is ticket #1, not #10, #11, or #100.

    The number arm is an exact match on the number read as text, rather than a
    substring — so a search for a short number cannot return a page of unrelated
    tickets whose numbers merely start with it.

    The customer is named explicitly and given a digit-free email, because the
    auto-generated one is `customer1@...` and the customer arm would match it. That is
    correct behaviour rather than a leak — see the test below — but it would make this
    test pass or fail for a reason that has nothing to do with the number arm.
    """
    org = register_org()
    customer = org.add_customer(name="Fox Mulder", email="lookout@fbi.example")
    created = [org.add_ticket(customer["id"]) for _ in range(3)]
    numbers = [ticket["number"] for ticket in created]
    assert numbers == [1, 2, 3], "numbers are per-organization, so a fresh org starts at 1"

    assert ids(search_tickets(org, q="1")) == {created[0]["id"]}
    assert ids(search_tickets(org, q="3")) == {created[2]["id"]}
    assert search_tickets(org, q="13") == [], "no substring matching on the number"


def test_a_term_matching_a_customers_email_returns_all_of_their_tickets(
    client: TestClient, register_org: Any
) -> None:
    """Searching an email is searching a person, and a person has more than one ticket.

    Recorded because it is the one arm whose result set is not one-ticket-shaped, and so
    the one most likely to read as a bug when it is met. §14 asks for customer email as a
    search field; returning every ticket that customer raised is what that means.
    """
    org = register_org()
    customer = org.add_customer(name="Fox Mulder", email="lookout@fbi.example")
    created = [org.add_ticket(customer["id"], subject="Unrelated subject") for _ in range(3)]
    other = org.add_customer(name="Dana Scully", email="elsewhere@fbi.example")
    org.add_ticket(other["id"], subject="Unrelated subject")

    assert ids(search_tickets(org, q="lookout@fbi.example")) == ids(created)


def test_a_customer_name_finds_their_tickets(client: TestClient, register_org: Any) -> None:
    """The customer arm, by name — the field a support desk actually searches by."""
    org = register_org()
    wanted_customer = org.add_customer(name="Fox Mulder")
    other_customer = org.add_customer(name="Dana Scully")
    wanted = org.add_ticket(wanted_customer["id"], subject="Unrelated subject")
    org.add_ticket(other_customer["id"], subject="Unrelated subject")

    found = search_tickets(org, q="Mulder")

    assert ids(found) == {wanted["id"]}


def test_a_customer_email_finds_their_tickets(client: TestClient, register_org: Any) -> None:
    """The other half of the customer arm."""
    org = register_org()
    wanted_customer = org.add_customer(name="Fox Mulder", email="fox@fbi.example")
    other_customer = org.add_customer(name="Dana Scully", email="dana@fbi.example")
    wanted = org.add_ticket(wanted_customer["id"], subject="Unrelated subject")
    org.add_ticket(other_customer["id"], subject="Unrelated subject")

    found = search_tickets(org, q="fox@fbi.example")

    assert ids(found) == {wanted["id"]}


def test_a_word_from_a_message_finds_its_ticket(client: TestClient, register_org: Any) -> None:
    """The message arm, for a term that is in no other field of the ticket.

    Without this the desk cannot find a ticket by the thing a customer said in the
    conversation, which is where the detail usually is.
    """
    org = register_org()
    customer = org.add_customer()
    wanted = org.add_ticket(customer["id"], subject="Unrelated subject")
    other = org.add_ticket(customer["id"], subject="Unrelated subject")
    post_message(org, wanted["id"], "The readings mention a magnetometer fault.")
    post_message(org, other["id"], "All nominal here.")

    found = search_tickets(org, q="magnetometer")

    assert ids(found) == {wanted["id"]}


def test_a_term_matching_nothing_is_an_empty_list(client: TestClient, register_org: Any) -> None:
    """A term nobody wrote is an empty result, not an error and not everything."""
    org = register_org()
    customer = org.add_customer()
    org.add_ticket(customer["id"], subject="The telescope array is misaligned")

    assert search_tickets(org, q="zzqqxx") == []


# ---------------------------------------------------------------------------
# A term is data, not a pattern
# ---------------------------------------------------------------------------


def test_percent_is_a_literal_character_not_a_wildcard(
    client: TestClient, register_org: Any
) -> None:
    """`q=100%` finds the customer named `100% Cotton`, and only that one.

    Unescaped, the `%` would be a LIKE wildcard matching every customer in the
    organization — a search that returns a full page for a term nobody has.
    """
    org = register_org()
    literal = org.add_customer(name="100% Cotton", email="literal@search.example")
    org.add_customer(name="100 Cotton", email="plain@search.example")

    found = search_customers(org, q="100%")

    assert ids(found) == {literal["id"]}


def test_underscore_is_a_literal_character_not_a_wildcard(
    client: TestClient, register_org: Any
) -> None:
    """`_` matches one character in LIKE, so `a_b` would otherwise find `aXb`."""
    org = register_org()
    literal = org.add_customer(name="a_b", email="underscore@search.example")
    org.add_customer(name="aXb", email="other@search.example")

    found = search_customers(org, q="a_b")

    assert ids(found) == {literal["id"]}


def test_a_backslash_in_a_term_is_a_character(client: TestClient, register_org: Any) -> None:
    """The escape character is escaped first, or it doubles up on the escapes added after it.

    A term of a single backslash is the case that catches the wrong ordering: it would
    become `\\\\` and then be read as a pattern containing an escape with nothing to
    escape.
    """
    org = register_org()
    literal = org.add_customer(name="back\\slash", email="backslash@search.example")
    org.add_customer(name="backslash", email="nobackslash@search.example")

    found = search_customers(org, q="back\\slash")

    assert ids(found) == {literal["id"]}


# ---------------------------------------------------------------------------
# Search narrows; it never widens
# ---------------------------------------------------------------------------


def test_search_cannot_widen_an_agents_row_scope(client: TestClient, register_org: Any) -> None:
    """An agent searching a term finds it only on tickets that are theirs.

    This is the property the whole module exists to protect. The term below is in the
    colleague's ticket and nowhere else, so a search that returned it would be a search
    that grants access — the classic form of "a query parameter that bypasses
    authorization".
    """
    org = register_org()
    customer = org.add_customer()

    colleague = org.add_user("agent")
    holder = org.add_user("agent")

    other_agents_ticket = org.add_ticket(customer["id"], subject="A magnetometer fault")
    assigned = org.post(
        f"{TICKETS}/{other_agents_ticket['id']}/assign",
        json={"assigned_agent_id": colleague.user_id},
    )
    assert assigned.status_code == 200, assigned.text

    own_ticket = org.add_ticket(customer["id"], subject="A spectrograph fault")
    assigned = org.post(
        f"{TICKETS}/{own_ticket['id']}/assign", json={"assigned_agent_id": holder.user_id}
    )
    assert assigned.status_code == 200, assigned.text

    # The holder cannot reach the colleague's ticket even unfiltered, so `q` must not
    # become the way to. Both are checked: an empty result for the search, and the same
    # 404 the ticket route gives when asked directly.
    assert search_tickets(holder, q="magnetometer") == []
    assert holder.get(f"{TICKETS}/{other_agents_ticket['id']}").status_code == 404

    assert ids(search_tickets(holder, q="spectrograph")) == {own_ticket["id"]}


def test_an_internal_note_does_not_surface_a_ticket_to_the_customer_who_owns_it(
    client: TestClient, register_org: Any
) -> None:
    """The phase's one real hazard.

    The customer owns the ticket, so without the internal filter the message arm would
    hand them the note's words back. The word below appears in the note and nowhere
    else, including in the ticket's own subject and description — so a hit could only
    have come from the note.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"], subject="Unrelated", description="Nothing to report.")
    portal = org.add_portal_user(customer["id"])
    post_note(org, ticket["id"], "Suspect the pyrometer is miscalibrated.")

    # The portal account reaches the ticket itself, so the refusal is about the search
    # and not about the ticket being out of scope.
    assert portal.get(f"{TICKETS}/{ticket['id']}").status_code == 200
    assert search_tickets(portal, q="pyrometer") == []


def test_the_same_internal_note_does_surface_the_ticket_to_an_agent(
    client: TestClient, register_org: Any
) -> None:
    """The other half of the same rule, so the filter is narrow rather than merely present.

    A filter that hid notes from everyone would pass the test above while quietly making
    notes unsearchable for the people they are written for.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"], subject="Unrelated", description="Nothing to report.")
    post_note(org, ticket["id"], "Suspect the pyrometer is miscalibrated.")

    assert ids(search_tickets(org, q="pyrometer")) == {ticket["id"]}


def test_a_customer_finds_their_own_ticket_but_not_anothers(
    client: TestClient, register_org: Any
) -> None:
    """The `own` scope, with a term both tickets share.

    The term is in the subject of both, so an empty-or-both answer would mean the scope
    is not being applied to the search at all.
    """
    org = register_org()
    mine = org.add_customer(name="Fox Mulder")
    theirs = org.add_customer(name="Dana Scully")
    portal = org.add_portal_user(mine["id"])

    my_ticket = org.add_ticket(mine["id"], subject="A magnetometer fault")
    org.add_ticket(theirs["id"], subject="A magnetometer fault")

    assert ids(search_tickets(portal, q="magnetometer")) == {my_ticket["id"]}


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def test_tickets_sort_by_number_in_both_directions(client: TestClient, register_org: Any) -> None:
    org = register_org()
    customer = org.add_customer()
    created = [org.add_ticket(customer["id"]) for _ in range(4)]

    ascending = search_tickets(org, sort="number", order="asc")
    descending = search_tickets(org, sort="number", order="desc")

    assert [t["number"] for t in ascending] == [1, 2, 3, 4]
    assert [t["number"] for t in descending] == [4, 3, 2, 1]
    assert ids(created) == ids(ascending)


def test_sorting_by_priority_puts_urgent_first(client: TestClient, register_org: Any) -> None:
    """§14's "sort by priority", by the enum's declaration order rather than by text.

    The PostgreSQL enum is ordered `LOW < MEDIUM < HIGH < URGENT`, so `order=desc` leads
    with `URGENT`. Sorting the label alphabetically would put `HIGH` first, which is the
    mistake this pins.
    """
    org = register_org()
    customer = org.add_customer()
    for priority in ("low", "urgent", "medium", "high"):
        org.add_ticket(customer["id"], priority=priority)

    descending = search_tickets(org, sort="priority", order="desc")
    ascending = search_tickets(org, sort="priority", order="asc")

    assert [t["priority"] for t in descending] == ["urgent", "high", "medium", "low"]
    assert [t["priority"] for t in ascending] == ["low", "medium", "high", "urgent"]


def test_tickets_default_to_newest_first(client: TestClient, register_org: Any) -> None:
    """The default is `created_at desc`, unchanged by Phase N's new parameters."""
    org = register_org()
    customer = org.add_customer()
    created = [org.add_ticket(customer["id"]) for _ in range(3)]

    assert [t["id"] for t in search_tickets(org)] == [t["id"] for t in reversed(created)]


def test_customers_sort_by_name(client: TestClient, register_org: Any) -> None:
    """`/customers` gained `name` alongside `created_at`, which stays the default."""
    org = register_org()
    org.add_customer(name="Charlie")
    org.add_customer(name="Alice")
    org.add_customer(name="Bob")
    # A portal user creates a customer of its own, so the expected names are drawn from
    # the response rather than hardcoded.
    org.add_user("agent")

    names = [c["name"] for c in search_customers(org, sort="name", order="asc")]
    assert names == sorted(names)
    assert {"Alice", "Bob", "Charlie"} <= set(names)


def test_customers_default_to_newest_first(client: TestClient, register_org: Any) -> None:
    org = register_org()
    created = [org.add_customer(name=name) for name in ("Alice", "Bob", "Charlie")]

    assert [c["id"] for c in search_customers(org)] == [c["id"] for c in reversed(created)]


@pytest.mark.parametrize(
    ("path", "params"),
    [
        (TICKETS, {"sort": "nonsense"}),
        (TICKETS, {"order": "sideways"}),
        (TICKETS, {"sort": "priority", "order": ""}),
        (CUSTOMERS, {"sort": "priority"}),
        (CUSTOMERS, {"order": "up"}),
    ],
    ids=["ticket-sort", "ticket-order", "ticket-empty-order", "customer-sort", "customer-order"],
)
def test_a_sort_key_outside_the_enum_is_refused(
    client: TestClient, register_org: Any, path: str, params: dict[str, str]
) -> None:
    """The sort parameters are enums, so a bad one is FastAPI's own 422.

    This is the reason they are enums rather than free strings: the set of valid keys is
    declared once, validated before the route body runs, and documented in the OpenAPI
    schema — none of which a string compared against a dict would give.
    """
    org = register_org()

    response = org.get(path, params=params)

    assert response.status_code == 422, response.text


def test_the_customer_sort_keys_are_deliberately_only_two(
    client: TestClient, register_org: Any
) -> None:
    """`priority` is a ticket's field, so it is a 422 on customers rather than ignored.

    A route that silently accepted and dropped an unknown sort would return an ordering
    the caller did not ask for and did not get told about.
    """
    org = register_org()

    assert org.get(CUSTOMERS, params={"sort": "priority"}).status_code == 422


# ---------------------------------------------------------------------------
# Pagination and date ranges
# ---------------------------------------------------------------------------


def test_pages_over_equal_sort_keys_do_not_repeat_or_skip_a_row(
    client: TestClient, register_org: Any
) -> None:
    """A page boundary is stable when every row shares a sort key.

    All six tickets have the default priority, so `sort=priority` gives six equal keys
    and the ordering is decided entirely by the appended unique tiebreak. Without it
    PostgreSQL is free to return them in any order per query, and offset pagination
    silently repeats some rows and skips others — a bug that only appears under load,
    which is why it is asserted rather than assumed.
    """
    org = register_org()
    customer = org.add_customer()
    created = [org.add_ticket(customer["id"]) for _ in range(6)]
    assert len({t["priority"] for t in created}) == 1, "the fixture must give equal sort keys"

    first = search_tickets(org, sort="priority", order="desc", limit=3, offset=0)
    second = search_tickets(org, sort="priority", order="desc", limit=3, offset=3)

    assert len(first) == 3 and len(second) == 3
    assert ids(first) & ids(second) == set(), "a row appeared on both pages"
    assert ids(first) | ids(second) == ids(created), "a row appeared on neither page"


def test_created_after_is_inclusive_and_created_before_is_exclusive(
    client: TestClient, register_org: Any
) -> None:
    """The bounds are half-open, so a caller can walk adjacent windows without gaps
    or double-counting.

    Both directions are checked at the same boundary: the middle ticket is *in* the
    window that starts at its own `created_at` and *out* of the window that ends there.
    Passing the microsecond-precision timestamp straight back is what makes the boundary
    exact — a bound truncated to the second would land on the wrong side.
    """
    org = register_org()
    customer = org.add_customer()
    created = [org.add_ticket(customer["id"]) for _ in range(3)]
    boundary = created[1]["created_at"]

    starting_here = search_tickets(org, created_after=boundary)
    ending_here = search_tickets(org, created_before=boundary)

    assert ids(starting_here) == {created[1]["id"], created[2]["id"]}
    assert ids(ending_here) == {created[0]["id"]}


def test_a_date_window_that_excludes_everything_returns_nothing(
    client: TestClient, register_org: Any
) -> None:
    """An inverted range is an empty list rather than an error or the whole table."""
    org = register_org()
    customer = org.add_customer()
    org.add_ticket(customer["id"])
    boundary = org.add_ticket(customer["id"])["created_at"]

    found = search_tickets(org, created_after=boundary, created_before=boundary)

    assert found == []


def test_date_ranges_and_tenancy_compose(client: TestClient, register_org: Any) -> None:
    """A window drawn from another organization's clock admits only the caller's own rows.

    A timestamp is not tenant data, so a bound taken from a second organization is not
    itself a leak — but the result set must still be the caller's rows and nothing else.
    """
    mine = register_org()
    theirs = register_org()
    my_customer = mine.add_customer()
    their_customer = theirs.add_customer()
    created = [mine.add_ticket(my_customer["id"]) for _ in range(3)]
    theirs.add_ticket(their_customer["id"])

    # The other organization's clock is ahead, so this window is wide enough to catch
    # anything of theirs that a scoping mistake would let through.
    ahead = theirs.get(TICKETS).json()[0]["created_at"]

    own = search_tickets(mine, created_before=ahead)
    assert ids(own) <= ids(created)
    assert not (ids(own) & {t["id"] for t in theirs.get(TICKETS).json()})


# ---------------------------------------------------------------------------
# The tri-state assignee filter
# ---------------------------------------------------------------------------


def test_unassigned_returns_only_tickets_with_nobody_on_them(
    client: TestClient, register_org: Any
) -> None:
    """The queue with no owner — the parameter Phase I-K's docstring promised.

    `assigned_agent_id=null` is not expressible over a query string, so it arrives as an
    explicit boolean rather than a `"null"` sentinel that would have to be parsed back
    out of a string, losing the UUID validation along the way.
    """
    org = register_org()
    customer = org.add_customer()
    agent = org.add_user("agent")

    assigned = org.add_ticket(customer["id"], subject="Someone is on this")
    org.post(f"{TICKETS}/{assigned['id']}/assign", json={"assigned_agent_id": agent.user_id})
    unassigned = org.add_ticket(customer["id"], subject="Nobody is on this")

    assert ids(search_tickets(org, unassigned=True)) == {unassigned["id"]}

    by_agent = search_tickets(org, assigned_agent_id=agent.user_id)
    assert ids(by_agent) == {assigned["id"]}

    # Neither parameter is no filter at all, which is a different question from both.
    assert ids(search_tickets(org)) == {assigned["id"], unassigned["id"]}


def test_asking_for_an_assignee_and_for_unassigned_is_refused(
    client: TestClient, register_org: Any
) -> None:
    """`422`, rather than silently honouring one of the two.

    An empty result would look like "this agent has nothing in the unassigned queue",
    which is a true-sounding answer to a question the caller did not ask.
    """
    org = register_org()
    agent = org.add_user("agent")

    response = org.get(TICKETS, params={"assigned_agent_id": agent.user_id, "unassigned": "true"})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_assignee_filter_still_validates_the_id(client: TestClient, register_org: Any) -> None:
    """The reason it is a boolean and not a sentinel: a UUID is still a UUID."""
    org = register_org()

    assert org.get(TICKETS, params={"assigned_agent_id": "not-a-uuid"}).status_code == 422
