"""`/sla/policies` over HTTP: the four seeded rows, who may read them, and who may change them.

Phase Q's admin-facing surface, and three of its claims can only be made here.

* **Every organization starts with §27's four policies.** The seeding happens inside
  `auth_service.register`, in the same transaction as the organization and its founding
  admin, and the only way to see that it landed is to register a tenant and read the
  endpoint back. A tenant whose SLA is inert until somebody visits a settings screen is a
  product decision the specification does not make.
* **Reading and writing are different capabilities.** `SLA_VIEW` is held by admin,
  manager, and agent; `SLA_CONFIGURE` by admin alone. `tests/security/test_route_protection.py`
  proves both routes *declare* a capability — this file proves they declare the *right*
  one, which a route-protection sweep cannot know.
* **A refused edit changes nothing.** The 422 for `resolution < response` is validated
  against the merged row, and the assertion that matters is not the status code but that
  the stored policy is untouched afterwards. A validation that ran after the mutation would
  answer 422 and leave the row wrong.

The audit rows are read through the API rather than off the table, because §34's claim is
that an administrator can see the change — a row nobody can read is half a feature.
"""

from collections.abc import Callable
from typing import Any

import pytest

from tests.conftest import API, SLA, OrgSession

pytestmark = pytest.mark.integration

AUDIT = f"{API}/audit-logs"

# §27's table, in the order the endpoint returns it: the priority enum's declaration order.
# Written out rather than imported from `sla_service.DEFAULT_POLICIES`, because a test that
# derives its expectation from the code under test proves only that the code is
# self-consistent — and these four rows are a transcription of a specification table.
EXPECTED = {
    "low": (24 * 60, 72 * 60),
    "medium": (8 * 60, 24 * 60),
    "high": (2 * 60, 8 * 60),
    "urgent": (30, 4 * 60),
}


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    return register_org(organization_name="SLA Co")


@pytest.fixture
def staff(org: OrgSession) -> dict[str, OrgSession]:
    """One authenticated user per staff role, all in the same organization."""
    return {
        "admin": org,
        "manager": org.add_user("manager", email="manager@slaco.com"),
        "agent": org.add_user("agent", email="agent@slaco.com"),
        "customer": org.add_user("customer", email="customer@slaco.com"),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def policies(session: OrgSession) -> list[dict[str, Any]]:
    response = session.get(f"{SLA}/policies")
    assert response.status_code == 200, response.text
    return list(response.json())


def policy_for(session: OrgSession, priority: str) -> dict[str, Any]:
    matching = [row for row in policies(session) if row["priority"] == priority]
    assert len(matching) == 1, f"expected one {priority} policy, found {len(matching)}"
    return matching[0]


def edit(session: OrgSession, priority: str, **body: Any) -> Any:
    return session.patch(f"{SLA}/policies/{priority}", json=body)


def audit_rows(session: OrgSession, action: str) -> list[dict[str, Any]]:
    response = session.get(AUDIT, params={"action": action, "limit": 100})
    assert response.status_code == 200, response.text
    return list(response.json())


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_registration_seeds_all_four_policies(org: OrgSession) -> None:
    """§27's four rows exist the moment the organization does.

    Order is asserted, not just membership: the endpoint promises the priority enum's
    declaration order, and an admin settings screen renders them in the order it receives
    them. A client sorting priorities itself would be a client that has to know the order.
    """
    rows = policies(org)

    assert [row["priority"] for row in rows] == ["low", "medium", "high", "urgent"]


@pytest.mark.parametrize(("priority", "targets"), sorted(EXPECTED.items()))
def test_the_seeded_targets_are_the_specification_table(
    org: OrgSession, priority: str, targets: tuple[int, int]
) -> None:
    """The numbers, per priority, against §27's table written out above.

    Parametrized so a wrong value names the priority it belongs to. The threshold is 80
    for all four — the column's default, which §27 does not state but which is what makes
    a warning fire while there is still time to act rather than at the breach.
    """
    response_minutes, resolution_minutes = targets

    row = policy_for(org, priority)

    assert row["response_time_minutes"] == response_minutes
    assert row["resolution_time_minutes"] == resolution_minutes
    assert row["warning_threshold_percent"] == 80
    assert row["is_active"] is True


def test_a_seeded_policy_reports_its_own_id(org: OrgSession) -> None:
    """Each row is addressable, which is what the audit trail's `target_id` needs.

    A settings screen that could only edit by priority and a trail that recorded the
    priority would leave "which policy, before or after a rename" unanswerable.
    """
    row = policy_for(org, "urgent")

    assert row["id"]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "manager", "agent"])
def test_every_staff_role_can_read_the_targets(staff: dict[str, OrgSession], role: str) -> None:
    """§3 gives `SLA_VIEW` to admin, manager, and agent — the whole desk, not the office.

    An agent working to a deadline needs to know what the deadline is, which is why this is
    one of the capabilities the customer role is the only one missing.
    """
    assert len(policies(staff[role])) == 4


def test_a_customer_cannot_read_the_targets(staff: dict[str, OrgSession]) -> None:
    """§3 withholds `SLA_VIEW` from the portal, and this is the 403 that enforces it.

    The reason is a business one rather than a security one: the targets are the provider's
    commitments, and a customer who could read the table would learn which priorities get
    a faster promise — which is the same fact `TicketRead.sla` is null to protect.
    """
    response = staff["customer"].get(f"{SLA}/policies")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def test_an_admin_can_raise_one_target(staff: dict[str, OrgSession]) -> None:
    """The realistic edit: one field, everything else untouched.

    Which is the reason the endpoint is a `PATCH`. The response reports the stored row, so
    the three fields the request did not mention come back with their previous values rather
    than as nulls.
    """
    response = edit(staff["admin"], "high", response_time_minutes=180)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["response_time_minutes"] == 180
    assert body["resolution_time_minutes"] == 8 * 60
    assert body["warning_threshold_percent"] == 80


def test_an_admin_can_deactivate_a_priority(staff: dict[str, OrgSession]) -> None:
    """Switching a priority off keeps its targets, so it can be switched back on.

    A settings screen has to show the row either way; `is_active` is a one-way door only if
    the read endpoint hides inactive rows, which is why `GET` includes them.
    """
    edit(staff["admin"], "low", is_active=False)

    assert policy_for(staff["admin"], "low")["is_active"] is False
    assert len(policies(staff["admin"])) == 4


def test_an_edit_lands_in_the_audit_trail(staff: dict[str, OrgSession]) -> None:
    """§34 names `SLA_POLICY_UPDATED`, and the trail records the before and the after.

    Both values, because a trail that recorded only the new target could not answer "what
    was the policy when this ticket breached" — which is the question an incident review
    actually asks. They arrive nested under `extra_data` rather than as top-level keys,
    which is what makes the row extensible: an action with no old value to compare against
    is *stored* the same shape and simply carries one side.
    """
    policy_id = policy_for(staff["admin"], "high")["id"]

    edit(staff["admin"], "high", response_time_minutes=180, warning_threshold_percent=50)

    (row,) = audit_rows(staff["admin"], "sla_policy_updated")
    assert row["target_type"] == "sla_policy"
    assert row["target_id"] == policy_id
    assert row["actor_user_id"] == staff["admin"].user_id
    before, after = row["extra_data"]["before"], row["extra_data"]["after"]
    assert before["response_time_minutes"] == 2 * 60
    assert after["response_time_minutes"] == 180
    assert before["warning_threshold_percent"] == 80
    assert after["warning_threshold_percent"] == 50
    # The two fields the request did not mention, present on both sides and equal. A
    # snapshot that recorded only what changed would make "the resolution target at the
    # time" unanswerable from the row that exists to answer it.
    assert before["resolution_time_minutes"] == after["resolution_time_minutes"] == 8 * 60


def test_a_no_op_edit_is_still_recorded(staff: dict[str, OrgSession]) -> None:
    """Editing a field to the value it already holds writes a row with identical sides.

    Asserted because the alternative — skipping the audit row when nothing changed — is the
    natural optimization and it destroys the trail's ability to answer "did anyone touch the
    SLA configuration before the incident". A trail with gaps for the edits that happened to
    be no-ops is a trail nobody can reason about.
    """
    edit(staff["admin"], "high", response_time_minutes=2 * 60)

    (row,) = audit_rows(staff["admin"], "sla_policy_updated")
    assert row["extra_data"]["before"] == row["extra_data"]["after"]


@pytest.mark.parametrize("role", ["manager", "agent"])
def test_nobody_below_admin_can_change_a_target(staff: dict[str, OrgSession], role: str) -> None:
    """§3 gives `SLA_CONFIGURE` to admin alone. The targets are a business commitment.

    A manager runs the queue but does not renegotiate the contract, so a manager who wants
    the operational ceiling raised asks an admin rather than granting it to themselves.
    """
    response = edit(staff[role], "high", response_time_minutes=1)

    assert response.status_code == 403
    assert policy_for(staff["admin"], "high")["response_time_minutes"] == 2 * 60


def test_a_customer_cannot_change_a_target(staff: dict[str, OrgSession]) -> None:
    """The portal is refused by the same guard, and the row is checked afterwards."""
    response = edit(staff["customer"], "urgent", resolution_time_minutes=1)

    assert response.status_code == 403
    assert policy_for(staff["admin"], "urgent")["resolution_time_minutes"] == 4 * 60


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_empty_edit_is_refused(staff: dict[str, OrgSession]) -> None:
    """A request that names no field has asked for nothing, and `200` would claim otherwise.

    Pydantic is happy with `{}` — every field is optional — so this is the validator that
    stops the route from committing nothing, writing an audit row saying a policy was
    updated, and reporting success.
    """
    response = edit(staff["admin"], "high")

    assert response.status_code == 422
    assert "at least one field" in response.text.lower()


def test_a_resolution_target_shorter_than_the_response_target_is_refused(
    staff: dict[str, OrgSession],
) -> None:
    """The cross-field rule, and the assertion that matters is that the row did not move.

    Only the response target is sent here, and the resolution target it must be checked
    against is the stored one — so a validator that looked at the payload alone would see
    one field, find nothing wrong, and write an unsatisfiable policy that PostgreSQL would
    reject at commit as a `CheckViolationError`, surfacing as a 500.
    """
    response = edit(staff["admin"], "low", response_time_minutes=72 * 60 + 1)

    assert response.status_code == 422
    assert "resolution" in response.text.lower()
    assert policy_for(staff["admin"], "low")["response_time_minutes"] == 24 * 60


def test_raising_the_resolution_target_alone_is_allowed(staff: dict[str, OrgSession]) -> None:
    """The same rule from the permitted side, so the test above is not passing by refusing.

    Without this, a validator that rejected every partial edit touching one of the two
    targets would satisfy the previous test and break the endpoint's only real use.
    """
    response = edit(staff["admin"], "low", resolution_time_minutes=96 * 60)

    assert response.status_code == 200, response.text
    assert response.json()["resolution_time_minutes"] == 96 * 60


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("response_time_minutes", 0),
        ("resolution_time_minutes", 0),
        ("warning_threshold_percent", 0),
        ("warning_threshold_percent", 100),
    ],
)
def test_a_value_outside_the_tables_constraints_is_refused(
    staff: dict[str, OrgSession], field: str, value: int
) -> None:
    """The four CheckConstraints, refused as a 422 with a field name rather than at commit.

    `warning_threshold_percent` has a bound at each end and both are here: 0 would warn the
    instant a ticket was created and 100 would warn at the breach, which is the same moment
    as the breach alert.
    """
    response = edit(staff["admin"], "medium", **{field: value})

    assert response.status_code == 422
    assert policy_for(staff["admin"], "medium")[field] != value


def test_an_unknown_priority_is_refused(staff: dict[str, OrgSession]) -> None:
    """The priority is a path segment typed as the enum, so a typo is a 422 and not a 404.

    Worth asserting because the two are easy to conflate: no policy exists at
    `/sla/policies/critical`, and the answer is "that is not a priority" rather than "that
    policy is missing".
    """
    response = edit(staff["admin"], "critical", response_time_minutes=60)

    assert response.status_code == 422
