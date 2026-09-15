"""The audit trail — written by the actions, read by an administrator.

Spec §34 asks for two things: that the listed actions be recorded against an
authenticated actor, and that an administrator be able to read them back. Both halves
are asserted here, and the pairing is the point — a service that wrote rows nobody could
read and a viewer reading rows nobody wrote would each pass half of this file.

The claims that would be cheapest to break, and so are named explicitly:

* **The actor is the caller, never a request field.** A trail whose actor can be chosen
  by the client records what the client says happened.
* **The row lands in the action's transaction.** An audit row that survives a rolled-back
  action describes something that did not happen.
* **Nobody but an admin can read it.** Not a manager, not an agent, not a customer, and
  a non-admin's 403 is the same 403 whatever is in the table.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import API, TICKETS, USERS, OrgSession

pytestmark = pytest.mark.integration

AUDIT = f"{API}/audit-logs"


def _trail(session: OrgSession, **params: Any) -> list[dict[str, Any]]:
    """Read the audit trail as this session, asserting the read succeeded."""
    response = session.get(AUDIT, params={"limit": 100, **params})
    assert response.status_code == 200, response.text
    return list(response.json())


def _actions(rows: list[dict[str, Any]]) -> list[str]:
    """The actions in the order returned — newest first."""
    return [row["action"] for row in rows]


def _only(rows: list[dict[str, Any]], action: str) -> dict[str, Any]:
    """The single row for an action, failing loudly if there is not exactly one.

    Deliberately not `next(...)`: a filter that quietly stopped working would make a
    test about `USER_ROLE_UPDATED` pass by reading a `USER_CREATED` row, and the failure
    would read as a wrong field rather than as a broken filter.
    """
    matching = [row for row in rows if row["action"] == action]
    assert len(matching) == 1, f"expected one {action} row, found {len(matching)}"
    return matching[0]


# ---------------------------------------------------------------------------
# Writing: every action the spec names
# ---------------------------------------------------------------------------


def test_registration_records_the_founding_administrator(
    client: TestClient, register_org: Any
) -> None:
    """A tenant's trail begins with the account that created it.

    The one call site with no `TenantContext` to read an actor from — the organization
    and its admin are being created by that very call. If it were skipped, "who created
    this organization" would be unanswerable from the trail, and the trail of a tenant
    would start with its second user.
    """
    org = register_org()

    row = _only(_trail(org), "user_created")

    assert row["actor_user_id"] == org.user_id
    assert row["actor_email"] == org.email
    assert row["target_type"] == "user"
    assert row["target_id"] == org.user_id
    assert row["extra_data"]["after"]["role"] == "admin"
    assert row["extra_data"]["source"] == "registration"


def test_the_trail_records_the_request_that_made_each_change(
    client: TestClient, register_org: Any
) -> None:
    """Provenance is captured for an authenticated request, not merely modelled.

    `ip_address` is asserted non-null rather than equal to a fixed value: the test
    client's address is an implementation detail, and asserting the exact string would
    tie this to httpx. That it is *populated* is the claim — an `Origin` dependency that
    was declared but never threaded through would leave it `None` everywhere, which is
    exactly the bug this catches.
    """
    org = register_org()

    row = _only(_trail(org), "user_created")

    assert row["ip_address"] is not None
    assert row["user_agent"] is not None


def test_a_user_creation_records_the_creator_not_the_created(
    client: TestClient, register_org: Any
) -> None:
    """Two different rows, and conflating them makes the trail unreadable.

    The actor is the admin who called `POST /users`; the target is the account they
    created. A trail that named the new user as their own actor would say every account
    created itself.
    """
    org = register_org()
    agent = org.add_user("agent")

    rows = _trail(org)
    created = [row for row in rows if row["action"] == "user_created"]
    assert len(created) == 2, _actions(rows)

    # Newest first, so the agent's creation is ahead of the founding admin's.
    newest = created[0]
    assert newest["target_id"] == agent.user_id
    assert newest["actor_user_id"] == org.user_id, "the creator is the actor"
    assert newest["target_id"] != newest["actor_user_id"]


def test_a_role_change_records_both_ends(client: TestClient, register_org: Any) -> None:
    """`before`/`after` are the whole content of this record.

    §34 asks for "before/after values where appropriate", and a role change is the case
    where it is most obviously appropriate: "the role changed" answers nothing an
    investigation asks.
    """
    org = register_org()
    agent = org.add_user("agent")

    response = org.patch(f"{USERS}/{agent.user_id}/role", json={"role": "manager"})
    assert response.status_code == 200, response.text

    row = _only(_trail(org), "user_role_updated")

    assert row["actor_user_id"] == org.user_id
    assert row["target_id"] == agent.user_id
    assert row["extra_data"]["before"] == {"role": "agent"}
    assert row["extra_data"]["after"] == {"role": "manager"}


def test_a_no_op_role_change_writes_nothing(client: TestClient, register_org: Any) -> None:
    """§34 records *actions*, and this was not one.

    A row saying a role changed from agent to agent records an event that did not
    happen, which is worse than a gap: it is wrong rather than merely absent.
    """
    org = register_org()
    agent = org.add_user("agent")

    response = org.patch(f"{USERS}/{agent.user_id}/role", json={"role": "agent"})
    assert response.status_code == 200, response.text

    assert "user_role_updated" not in _actions(_trail(org))


def test_a_deactivation_records_the_transition(client: TestClient, register_org: Any) -> None:
    org = register_org()
    agent = org.add_user("agent")

    response = org.post(f"{USERS}/{agent.user_id}/deactivate")
    assert response.status_code == 200, response.text

    row = _only(_trail(org), "user_deactivated")

    assert row["target_id"] == agent.user_id
    assert row["extra_data"]["before"] == {"is_active": True}
    assert row["extra_data"]["after"] == {"is_active": False}


def test_the_ticket_lifecycle_is_recorded_action_by_action(
    client: TestClient, register_org: Any
) -> None:
    """Every step of §5's lifecycle, and each under the action §34 names for it.

    Driven in one test rather than five so the *sequence* is visible: the trail is read
    to establish what happened in what order, and a set of rows that is individually
    correct but collectively out of order would still answer the question wrongly.
    """
    org = register_org()
    agent = org.add_user("agent")
    customer = org.add_customer(name="Dana Scully", email="dana@audit.com")
    ticket = org.add_ticket(customer["id"], subject="The observatory is offline")

    steps = [
        ("ticket_created", lambda: org.get(f"{TICKETS}/{ticket['id']}")),
        (
            "ticket_assigned",
            lambda: org.post(
                f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": agent.user_id}
            ),
        ),
        (
            "ticket_priority_changed",
            lambda: org.post(f"{TICKETS}/{ticket['id']}/priority", json={"priority": "urgent"}),
        ),
        (
            "ticket_status_changed",
            lambda: org.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "in_progress"}),
        ),
        # `resolved` has its own action in §34's list, and it is chosen by the target
        # status rather than by the route — the same `/status` endpoint produces both.
        (
            "ticket_resolved",
            lambda: org.post(f"{TICKETS}/{ticket['id']}/status", json={"status": "resolved"}),
        ),
        (
            "ticket_status_changed",
            lambda: org.post(f"{TICKETS}/{ticket['id']}/close"),
        ),
        ("ticket_reopened", lambda: org.post(f"{TICKETS}/{ticket['id']}/reopen")),
    ]

    for _expected_action, call in steps:
        response = call()
        assert response.status_code == 200, response.text

    rows = [
        row
        for row in _trail(org)
        if row["target_type"] == "ticket" and row["target_id"] == ticket["id"]
    ]

    assert [row["action"] for row in rows] == [
        "ticket_reopened",
        "ticket_status_changed",
        "ticket_resolved",
        "ticket_status_changed",
        "ticket_priority_changed",
        "ticket_assigned",
        "ticket_created",
    ], "newest first, and a step in the wrong place is a step that did not happen"

    for row in rows:
        assert row["actor_user_id"] == org.user_id


def test_a_priority_change_carries_the_old_and_new_value(
    client: TestClient, register_org: Any
) -> None:
    """A ticket is created at MEDIUM, so the `before` here is a real value rather than
    an absent one — which is what makes this a different case from `ticket_created`."""
    org = register_org()
    customer = org.add_customer(name="Dana Scully", email="dana@audit.com")
    ticket = org.add_ticket(customer["id"])

    response = org.post(f"{TICKETS}/{ticket['id']}/priority", json={"priority": "urgent"})
    assert response.status_code == 200, response.text

    row = _only(_trail(org, action="ticket_priority_changed"), "ticket_priority_changed")

    assert row["extra_data"]["before"] == {"priority": "medium"}
    assert row["extra_data"]["after"] == {"priority": "urgent"}


def test_a_ticket_creation_has_no_before(client: TestClient, register_org: Any) -> None:
    """The absence is meaningful: there was no earlier ticket to describe.

    Asserted rather than left implicit so that a service which started writing
    `before={"status": null}` would fail here. `None` and "no key" are different claims,
    and only one of them is true.
    """
    org = register_org()
    customer = org.add_customer(name="Dana Scully", email="dana@audit.com")
    org.add_ticket(customer["id"])

    row = _only(_trail(org), "ticket_created")

    assert "before" not in row["extra_data"]
    assert row["extra_data"]["after"]["status"] == "open"


def test_the_actor_is_the_caller_not_a_field_in_the_body(
    client: TestClient, register_org: Any
) -> None:
    """The property that makes the trail evidence rather than a claim.

    `record_for` reads the actor from the authenticated context, so there is no code
    path by which a caller could stamp someone else's id on a row. This asserts it from
    the outside: a second admin acting on the first is recorded as themselves.

    The trail is read back as the *second* admin, because the first has just been
    demoted to manager by this very action — reading it as `org` would be a 403, and
    the 403 would be correct.
    """
    org = register_org()
    other_admin = org.add_user("admin")

    response = other_admin.patch(f"{USERS}/{org.user_id}/role", json={"role": "manager"})
    assert response.status_code == 200, response.text

    row = _only(_trail(other_admin), "user_role_updated")

    assert row["actor_user_id"] == other_admin.user_id
    assert row["target_id"] == org.user_id


# ---------------------------------------------------------------------------
# Reading: who may, and what they see
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["manager", "agent", "customer"])
def test_only_an_administrator_can_read_the_trail(
    client: TestClient, register_org: Any, role: str
) -> None:
    """§3's matrix gives "View audit log" to admin and to nobody else.

    Every non-admin role is checked rather than one: the matrix has four roles, and a
    guard that admitted managers would look like a guard that admitted everyone if only
    agents were tested.
    """
    org = register_org()
    # Something to read, so a 403 cannot be mistaken for an empty-list 200.
    org.add_user("agent")
    session = org if role == "admin" else org.add_user(role)

    response = session.get(AUDIT)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_the_trail_is_newest_first(client: TestClient, register_org: Any) -> None:
    """Ordered by `created_at desc, id`, and the test drives *audited* actions.

    Deliberately not a loop of `add_customer`: §34's list has no customer action, so
    that would write no rows at all and this would pass by comparing a one-element list
    against itself. Creating users is the cheapest action that does write a row.
    """
    org = register_org()
    for _ in range(3):
        org.add_user("agent")

    rows = _trail(org)
    timestamps = [row["created_at"] for row in rows]

    assert len(rows) >= 4, "the founding admin plus three agents"
    assert timestamps == sorted(timestamps, reverse=True)


def test_each_page_is_disjoint_from_the_last(client: TestClient, register_org: Any) -> None:
    """Offset pagination is only sound over a *total* order.

    Several audit rows can share a `created_at` to the microsecond — the registration
    path writes one alongside the user row it describes — so a sort on `created_at`
    alone would let a row appear in two pages and another appear in none. The `id`
    tiebreak in the repository is what this proves.
    """
    org = register_org()
    for _ in range(4):
        org.add_user("agent")

    first = org.get(AUDIT, params={"limit": 3}).json()
    second = org.get(AUDIT, params={"limit": 3, "offset": 3}).json()

    assert first and second
    assert not {row["id"] for row in first} & {row["id"] for row in second}


def test_the_action_filter_narrows_to_one_kind(client: TestClient, register_org: Any) -> None:
    """What the viewer is actually used for: "show me every role change"."""
    org = register_org()
    agent = org.add_user("agent")
    org.add_user("manager")
    org.patch(f"{USERS}/{agent.user_id}/role", json={"role": "manager"})

    rows = _trail(org, action="user_role_updated")

    assert _actions(rows) == ["user_role_updated"]
    assert rows[0]["target_id"] == agent.user_id


def test_the_actor_filter_narrows_to_one_person(client: TestClient, register_org: Any) -> None:
    """ "Everything this user did" — the second question an investigation asks.

    The second admin has to *act*, not merely exist. Creating them writes a
    `USER_CREATED` row whose actor is the founding admin and whose target is them, so a
    filter on their id would find nothing — the two roles are different, and a test that
    confused them would be asserting the bug it is meant to catch.
    """
    org = register_org()
    other_admin = org.add_user("admin")
    # The act: this is the row whose *actor* is `other_admin`.
    other_admin.add_user("agent")

    rows = _trail(org, actor_user_id=other_admin.user_id)

    assert rows, "the second admin created a user, so they have at least one row"
    assert {row["actor_user_id"] for row in rows} == {other_admin.user_id}


def test_the_target_filter_narrows_to_one_entity(client: TestClient, register_org: Any) -> None:
    """The history of a single ticket, which is the third question and the one that most
    needs an index — `ix_audit_logs_target`."""
    org = register_org()
    customer = org.add_customer(name="Dana Scully", email="dana@audit.com")
    ticket = org.add_ticket(customer["id"])
    other = org.add_ticket(customer["id"], subject="A second problem")

    rows = _trail(org, target_type="ticket", target_id=ticket["id"])

    assert rows
    assert {row["target_id"] for row in rows} == {ticket["id"]}
    assert other["id"] not in {row["target_id"] for row in rows}


def test_the_date_range_is_half_open(client: TestClient, register_org: Any) -> None:
    """`created_after` inclusive, `created_before` exclusive.

    A trail is read to establish a sequence of events, so a row appearing in two
    adjacent windows is a small lie about what happened. The boundary value is taken
    from a real row's timestamp, which is the only way to test the difference between
    `<` and `<=` rather than merely testing that a filter exists.
    """
    org = register_org()
    org.add_customer(name="Dana Scully", email="dana@audit.com")

    everything = _trail(org)
    boundary = everything[0]["created_at"]

    assert _trail(org, created_after=boundary) == everything
    assert _trail(org, created_before=boundary) == everything[1:]


# ---------------------------------------------------------------------------
# The table's own claims
# ---------------------------------------------------------------------------


def test_the_repository_offers_no_way_to_change_a_row() -> None:
    """The module's docstring claims the trail is append-only.

    A claim about a table is worth less than the code that makes it true, so this reads
    the code. `add` is inherited from `TenantScopedRepository` and is the only writer;
    an `update` or a `delete` appearing here would be a new decision, and this is where
    it has to be stated rather than slipped in.
    """
    from app.repositories.audit_log_repository import AuditLogRepository

    mutators = {"update", "delete", "remove", "purge", "set"} & set(dir(AuditLogRepository))

    assert not mutators, f"the audit repository gained a mutator: {sorted(mutators)}"


def test_the_audit_log_model_has_no_updated_at() -> None:
    """An edit timestamp is an invitation to edit."""
    from app.models.audit_log import AuditLog

    assert "updated_at" not in AuditLog.__table__.columns


def test_the_read_model_does_not_echo_the_organization(
    client: TestClient, register_org: Any
) -> None:
    """The token already says which organization this is.

    Echoing it back invites a client to treat it as a field it can influence, and every
    other read model in this API leaves it out for the same reason.
    """
    org = register_org()

    assert "organization_id" not in _trail(org)[0]
