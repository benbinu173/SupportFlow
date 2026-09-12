"""User administration, and the role permissions enforced against real endpoints.

`tests/unit/test_permissions.py` asserts the matrix as data. This file asserts that
the *routes are actually wired to it* — that `USER_CREATE` being admin-only in a
Python set translates into a 403 for a manager who calls the endpoint. Those are
different claims, and only the second one protects anything.

Every test drives the real API. An organization is registered, its users are created
by an admin calling `POST /users`, and each of them then authenticates for real.
"""

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.conftest import AUTH, PASSWORD, USERS, OrgSession, login

pytestmark = pytest.mark.integration

# A payload that is valid for every field, so a 403 in these tests can only be the
# authorization decision and never a validation failure.
NEW_USER = {
    "name": "New Hire",
    "email": "new.hire@staffed.com",
    "password": PASSWORD,
    "role": "agent",
}


@pytest.fixture
def roles(register_org: Callable[..., OrgSession]) -> dict[str, OrgSession]:
    """One organization staffed with a user in every role, each authenticated.

    A single tenant, because these tests are about *who within an organization* may do
    what. Cross-tenant behaviour is a separate concern and a separate suite.
    """
    admin = register_org(organization_name="Staffed Co")
    return {
        "admin": admin,
        "manager": admin.add_user("manager", email="manager@staffed.com"),
        "agent": admin.add_user("agent", email="agent@staffed.com"),
        "customer": admin.add_user("customer", email="customer@staffed.com"),
    }


# ---------------------------------------------------------------------------
# Listing users
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["manager", "agent", "customer"])
def test_only_staff_who_may_list_users_can_list_them(
    roles: dict[str, OrgSession], role: str
) -> None:
    """`List users` is ✓ for admin and manager, `—` for agent and customer."""
    expected = 200 if role == "manager" else 403

    response = roles[role].get(USERS)

    assert response.status_code == expected, response.text
    if expected == 403:
        assert response.json()["error"]["code"] == "FORBIDDEN"


def test_an_admin_sees_every_user_in_the_organization(
    roles: dict[str, OrgSession],
) -> None:
    response = roles["admin"].get(USERS)

    assert response.status_code == 200
    emails = {user["email"] for user in response.json()}
    assert emails == {session.email for session in roles.values()}


def test_the_listing_never_exposes_a_password_hash(roles: dict[str, OrgSession]) -> None:
    """The strongest form of the check: the field does not exist in the schema at all,
    so the assertion is about the *response*, not about one code path.

    The field set is written out rather than sampled, which is why this test had to be
    updated when `customer_id` was added in Phase I — that is the guard working. A new
    field on `UserRead` reaching a response without anyone deciding it should is exactly
    what this catches.
    """
    response = roles["admin"].get(USERS)

    assert "password_hash" not in response.text
    assert "password" not in response.text
    for user in response.json():
        assert set(user) == {
            "id",
            "name",
            "email",
            "role",
            "is_active",
            "customer_id",
            "created_at",
            "last_login_at",
        }


def test_the_listing_never_exposes_the_organization_id(
    roles: dict[str, OrgSession],
) -> None:
    """Echoing it back would suggest it is the client's to set.

    The organization is implied by the caller's token and is never a field anyone
    reads or writes through this API.
    """
    response = roles["admin"].get(USERS)

    assert "organization_id" not in response.text


def test_the_listing_is_paginated(roles: dict[str, OrgSession]) -> None:
    """`offset` and `limit` select a window, and the windows tile the whole set."""
    admin = roles["admin"]

    everything = admin.get(USERS).json()
    first_two = admin.get(USERS, params={"limit": 2}).json()
    next_two = admin.get(USERS, params={"limit": 2, "offset": 2}).json()

    assert len(first_two) == 2
    assert [*first_two, *next_two] == everything


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"limit": -1}, {"offset": -1}])
def test_an_out_of_range_page_size_is_rejected(
    roles: dict[str, OrgSession], params: dict[str, int]
) -> None:
    """The ceiling is enforced server-side: an unbounded page size is a way to make the
    server do arbitrary work per request."""
    response = roles["admin"].get(USERS, params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Creating users
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["manager", "agent", "customer"])
def test_only_an_admin_may_create_users(roles: dict[str, OrgSession], role: str) -> None:
    """`Create user` is admin-only — including for a manager, who may list users but
    not add them."""
    response = roles[role].post(USERS, json=NEW_USER)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_an_admin_creates_a_user_who_can_then_log_in(
    roles: dict[str, OrgSession],
) -> None:
    """The created account is real: it authenticates and resolves to the same identity.

    Asserted by logging in rather than by re-reading the row, because "the password was
    hashed correctly" is only true if the hash verifies at login.
    """
    created = roles["admin"].post(USERS, json=NEW_USER)

    assert created.status_code == 201, created.text
    assert created.json()["role"] == "agent"
    assert created.json()["is_active"] is True

    session = login(roles["admin"].client, NEW_USER["email"])

    assert session.user_id == created.json()["id"]
    assert session.role == "agent"


def test_a_created_user_joins_the_creator_s_organization(
    roles: dict[str, OrgSession],
) -> None:
    """The tenant comes from the caller's verified token.

    The request body has no organization field, so this is verified from the one place
    it is observable: the new account appears in the creator's listing.
    """
    roles["admin"].post(USERS, json=NEW_USER)

    listed = {user["email"] for user in roles["admin"].get(USERS).json()}

    assert NEW_USER["email"] in listed


def test_a_duplicate_email_within_the_organization_is_a_conflict(
    roles: dict[str, OrgSession],
) -> None:
    """Email is unique per tenant, so the second attempt is a 409 rather than a 500
    from the database's unique constraint."""
    response = roles["admin"].post(USERS, json={**NEW_USER, "email": roles["agent"].email})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "USER_ALREADY_EXISTS"


def test_a_role_is_required_when_creating_a_user(roles: dict[str, OrgSession]) -> None:
    """Not defaulted. A default would be a silent privilege decision."""
    payload = {key: value for key, value in NEW_USER.items() if key != "role"}

    response = roles["admin"].post(USERS, json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_an_unknown_role_is_rejected(roles: dict[str, OrgSession]) -> None:
    """The role vocabulary is closed — `owner` is not a role, and inventing one is not
    a way to get more access."""
    response = roles["admin"].post(USERS, json={**NEW_USER, "role": "owner"})

    assert response.status_code == 422


def test_a_short_password_is_rejected(roles: dict[str, OrgSession]) -> None:
    minimum = get_settings().PASSWORD_MIN_LENGTH
    response = roles["admin"].post(USERS, json={**NEW_USER, "password": "a" * (minimum - 1)})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Fetching one user
# ---------------------------------------------------------------------------


def test_fetching_a_user_returns_that_user(roles: dict[str, OrgSession]) -> None:
    response = roles["admin"].get(f"{USERS}/{roles['agent'].user_id}")

    assert response.status_code == 200
    assert response.json()["id"] == roles["agent"].user_id
    assert response.json()["email"] == roles["agent"].email


def test_a_manager_may_fetch_a_user(roles: dict[str, OrgSession]) -> None:
    """Reading one user needs `USER_LIST`, which a manager holds."""
    response = roles["manager"].get(f"{USERS}/{roles['agent'].user_id}")

    assert response.status_code == 200


def test_an_agent_and_a_customer_may_not_fetch_an_arbitrary_user(
    roles: dict[str, OrgSession],
) -> None:
    """`PROFILE_VIEW` is the capability to read *your own* profile, served by
    `/auth/me` — it is not a licence to read the directory."""
    for role in ("agent", "customer"):
        response = roles[role].get(f"{USERS}/{roles['admin'].user_id}")
        assert response.status_code == 403, f"{role} was allowed to read another user"


def test_an_unknown_user_id_is_a_404(roles: dict[str, OrgSession]) -> None:
    response = roles["admin"].get(f"{USERS}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_a_malformed_user_id_is_rejected_before_the_lookup(
    roles: dict[str, OrgSession],
) -> None:
    response = roles["admin"].get(f"{USERS}/not-a-uuid")

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Changing a role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["manager", "agent", "customer"])
def test_only_an_admin_may_change_a_role(roles: dict[str, OrgSession], role: str) -> None:
    response = roles[role].patch(
        f"{USERS}/{roles['customer'].user_id}/role", json={"role": "agent"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_an_admin_may_promote_a_user(roles: dict[str, OrgSession]) -> None:
    response = roles["admin"].patch(
        f"{USERS}/{roles['agent'].user_id}/role", json={"role": "manager"}
    )

    assert response.status_code == 200
    assert response.json()["role"] == "manager"


def test_a_promotion_takes_effect_on_the_promoted_user_s_next_request(
    roles: dict[str, OrgSession],
) -> None:
    """The database role is authoritative (ADR-013), so the agent's existing token —
    which still claims `agent` — is served with their new capabilities immediately."""
    agent = roles["agent"]
    assert agent.get(USERS).status_code == 403

    roles["admin"].patch(f"{USERS}/{agent.user_id}/role", json={"role": "manager"})

    # Same token, same session: only the database row changed.
    assert agent.get(USERS).status_code == 200


def test_an_admin_may_not_demote_the_only_administrator(
    register_org: Callable[..., OrgSession],
) -> None:
    """Nobody would remain who could grant the role back.

    A 422 rather than a 403: the caller is permitted to change roles, and the request
    is refused because of the state of the organization — a different kind of "no".
    """
    admin = register_org(organization_name="Solo Admin Co")

    response = admin.patch(f"{USERS}/{admin.user_id}/role", json={"role": "agent"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "administrator" in response.json()["error"]["message"]


def test_demoting_an_admin_is_allowed_once_another_admin_exists(
    roles: dict[str, OrgSession],
) -> None:
    """The rule is "at least one admin", not "never demote an admin".

    Without this test, an implementation that simply refused every demotion would pass
    the test above while breaking a legitimate operation.
    """
    admin, colleague = roles["admin"], roles["manager"]
    promoted = admin.patch(f"{USERS}/{colleague.user_id}/role", json={"role": "admin"})
    assert promoted.status_code == 200

    # Now there are two, so the original may step down.
    demoted = colleague.patch(f"{USERS}/{admin.user_id}/role", json={"role": "agent"})
    assert demoted.status_code == 200
    assert demoted.json()["role"] == "agent"


def test_setting_the_role_a_user_already_has_is_accepted(
    roles: dict[str, OrgSession],
) -> None:
    """Idempotent, and not treated as a demotion of the last admin."""
    response = roles["admin"].patch(
        f"{USERS}/{roles['admin'].user_id}/role", json={"role": "admin"}
    )

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_an_invalid_role_is_rejected(roles: dict[str, OrgSession]) -> None:
    response = roles["admin"].patch(
        f"{USERS}/{roles['agent'].user_id}/role", json={"role": "superuser"}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Deactivating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["manager", "agent", "customer"])
def test_only_an_admin_may_deactivate_a_user(roles: dict[str, OrgSession], role: str) -> None:
    response = roles[role].post(f"{USERS}/{roles['customer'].user_id}/deactivate")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_an_admin_may_deactivate_a_user(roles: dict[str, OrgSession]) -> None:
    response = roles["admin"].post(f"{USERS}/{roles['agent'].user_id}/deactivate")

    assert response.status_code == 200
    assert response.json()["is_active"] is False


def test_a_deactivated_user_is_locked_out_immediately(roles: dict[str, OrgSession]) -> None:
    """Their live access token stops working on the very next request, and they cannot
    obtain a new one — the `is_active` check runs on both paths."""
    agent = roles["agent"]
    roles["admin"].post(f"{USERS}/{agent.user_id}/deactivate")

    assert agent.get(f"{AUTH}/me").status_code == 403

    login = agent.client.post(
        f"{AUTH}/login", json={"email": agent.email, "password": agent.password}
    )
    assert login.status_code == 403


def test_a_deactivated_user_is_retained_rather_than_deleted(
    roles: dict[str, OrgSession],
) -> None:
    """Deactivation preserves authorship and audit history, so the row stays and only
    its flag changes."""
    roles["admin"].post(f"{USERS}/{roles['agent'].user_id}/deactivate")

    listed = {user["email"]: user for user in roles["admin"].get(USERS).json()}

    assert listed[roles["agent"].email]["is_active"] is False


def test_an_admin_may_not_deactivate_their_own_account(
    roles: dict[str, OrgSession],
) -> None:
    """It would end the caller's own session with no way to undo it."""
    response = roles["admin"].post(f"{USERS}/{roles['admin'].user_id}/deactivate")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "your own account" in response.json()["error"]["message"]


def test_deactivating_cannot_leave_the_organization_without_an_administrator(
    register_org: Callable[..., OrgSession],
) -> None:
    """The invariant holds, but note *which* check upholds it here.

    `deactivate_user` guards two ways: it refuses self-deactivation, and it refuses to
    remove an admin when no other admin remains. Through this endpoint only the first
    is reachable — the capability is admin-only, so any caller acting on an admin is
    either that admin (caught by the self-check) or a second admin, whose own existence
    satisfies the count. The count is retained as a defensive check on the service
    function, which is also called from places that are not this route.

    So this test asserts the *outcome* rather than claiming to cover the count: after
    every operation available here, an administrator still exists who can undo it.
    """
    admin = register_org(organization_name="Handover Co")
    successor = admin.add_user("admin", email="successor@handover.com")

    # Two admins: the successor may deactivate the original.
    assert successor.post(f"{USERS}/{admin.user_id}/deactivate").status_code == 200

    # One admin left, and that admin cannot remove themselves.
    assert successor.post(f"{USERS}/{successor.user_id}/deactivate").status_code == 422

    # A live administrator remains, and the organization is still administrable.
    still_admin = successor.get(f"{USERS}/{successor.user_id}").json()
    assert still_admin["role"] == "admin"
    assert still_admin["is_active"] is True


def test_a_deactivated_user_may_not_change_their_own_role(
    roles: dict[str, OrgSession],
) -> None:
    """Authorization is checked before the row is loaded, so a deactivated account is
    refused at the token stage rather than at the operation."""
    roles["admin"].post(f"{USERS}/{roles['manager'].user_id}/deactivate")

    response = roles["manager"].patch(
        f"{USERS}/{roles['manager'].user_id}/role", json={"role": "admin"}
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", USERS),
        ("post", USERS),
        ("get", f"{USERS}/00000000-0000-0000-0000-000000000000"),
        ("patch", f"{USERS}/00000000-0000-0000-0000-000000000000/role"),
        ("post", f"{USERS}/00000000-0000-0000-0000-000000000000/deactivate"),
    ],
)
def test_every_users_route_requires_a_token(client: TestClient, method: str, path: str) -> None:
    """No route here is reachable without a credential, whatever its capability."""
    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
