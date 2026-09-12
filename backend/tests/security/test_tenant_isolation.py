"""Multi-tenancy: one organization cannot reach another's data.

Spec §11 calls multi-tenancy one of the most important parts of the project and §51
calls this phase critical. These are the tests that hold that property up.

The rule being asserted throughout is **404, not 403** (ADR-009). A 403 would confirm
that the record exists and is merely out of reach, which turns the API into an
enumeration oracle: an attacker could map another tenant's user ids by watching which
ones answer "forbidden" and which answer "not found". A 404 is indistinguishable from
an id that never existed, and that is the only answer that discloses nothing.

These tests are marked `security`, so `make security` runs this suite on its own.
"""

import uuid
from collections.abc import Callable

import pytest
from httpx import Response

from app.core.security import create_access_token
from app.models.enums import UserRole
from tests.conftest import AUTH, PASSWORD, USERS, OrgSession, login

pytestmark = pytest.mark.security


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every isolation test.

    Declared here rather than in a package-level conftest, because the other file in
    this package inspects the routing table and issues no requests at all. An autouse
    fixture at package scope would make that file require a running Postgres to check
    something that has nothing to do with the database — and it did, until this moved.

    Nothing to do in the body: `truncate_tables` already yields, so *depending* on it is
    what places the truncation on the far side of the test.
    """


@pytest.fixture
def two_orgs(
    register_org: Callable[..., OrgSession],
) -> tuple[OrgSession, OrgSession]:
    """Two unrelated organizations, each with an authenticated admin.

    Registered through the real endpoint, which is also the only way to be sure the
    two tenants are genuinely independent — an isolation test against hand-inserted
    rows would prove the ORM works and nothing about the API.
    """
    first = register_org(organization_name="Northwind")
    second = register_org(organization_name="Southwind")
    return first, second


def assert_indistinguishable_from_a_missing_record(
    cross_tenant: Response, never_existed: Response
) -> None:
    """Compare a cross-tenant response with a genuinely absent record's.

    Status, error code, message, and the full body — not just the status code. A
    response that said "user not found" for a random id and "that user belongs to
    another organization" for a real one would leak exactly as much as a 403, while
    still passing a status-only assertion.
    """
    assert cross_tenant.status_code == never_existed.status_code
    assert cross_tenant.json() == never_existed.json()
    # A challenge header would mean the request failed authentication rather than
    # authorization, which is a different claim about the id.
    assert cross_tenant.headers.get("WWW-Authenticate") is None


# ---------------------------------------------------------------------------
# Reading another tenant's users
# ---------------------------------------------------------------------------


def test_an_organization_cannot_read_another_organization_s_user(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The core isolation guarantee, for the one tenant-owned resource that exists."""
    northwind, southwind = two_orgs

    response = southwind.get(f"{USERS}/{northwind.user_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_a_cross_tenant_404_is_identical_to_a_missing_record_404(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The indistinguishability property, asserted directly.

    Both responses are compared in full. This is the assertion that would fail if
    anyone ever "improved" the cross-tenant path with a more helpful message.
    """
    northwind, southwind = two_orgs

    cross_tenant = southwind.get(f"{USERS}/{northwind.user_id}")
    never_existed = southwind.get(f"{USERS}/{uuid.uuid4()}")

    assert_indistinguishable_from_a_missing_record(cross_tenant, never_existed)


def test_the_cross_tenant_response_discloses_nothing(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """No field of the target organization's record reaches the response.

    Checked against the raw body rather than the parsed JSON, so a field nested
    anywhere — or a stack trace in a debug build — would be caught.
    """
    northwind, southwind = two_orgs

    body = southwind.get(f"{USERS}/{northwind.user_id}").text

    assert northwind.email not in body
    assert northwind.user_id not in body
    assert "Northwind" not in body


def test_an_organization_cannot_enumerate_another_s_users(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """Blind guessing is no more useful than requesting a known id.

    A handful of ids are tried rather than a systematic sweep: sweeping the space is
    infeasible, and what matters is that no valid id is distinguishable from an
    invalid one.
    """
    northwind, southwind = two_orgs
    northwind.add_user("agent", email="agent@northwind.com")

    with_known_member = {southwind.get(f"{USERS}/{northwind.user_id}").status_code}
    for _ in range(5):
        with_known_member.add(southwind.get(f"{USERS}/{uuid.uuid4()}").status_code)

    assert with_known_member == {404}


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_a_listing_contains_only_the_caller_s_own_users(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The listing is filtered by tenant, not merely decorated with it.

    Both organizations are given members, so the assertion is about a non-empty
    intersection being absent rather than about an empty list.
    """
    northwind, southwind = two_orgs
    northwind.add_user("agent", email="agent@northwind.com")
    southwind.add_user("manager", email="manager@southwind.com")

    northwind_emails = {user["email"] for user in northwind.get(USERS).json()}
    southwind_emails = {user["email"] for user in southwind.get(USERS).json()}

    assert northwind_emails == {northwind.email, "agent@northwind.com"}
    assert southwind_emails == {southwind.email, "manager@southwind.com"}
    assert not northwind_emails & southwind_emails


def test_neither_listing_leaks_the_other_s_users_by_id(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The ids themselves are tenant data, and the check above compares only emails."""
    northwind, southwind = two_orgs
    intruder = northwind.add_user("agent", email="agent@northwind.com")

    listed = {user["id"] for user in southwind.get(USERS).json()}

    assert intruder.user_id not in listed


def test_a_user_absent_from_a_listing_cannot_be_fetched_by_id(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """Listing and fetching agree, so the id is not a bypass around the filter."""
    northwind, southwind = two_orgs
    hidden = northwind.add_user("agent", email="agent@northwind.com")

    assert hidden.user_id not in {user["id"] for user in southwind.get(USERS).json()}
    assert southwind.get(f"{USERS}/{hidden.user_id}").status_code == 404


# ---------------------------------------------------------------------------
# Writing across tenants
# ---------------------------------------------------------------------------


def test_an_organization_cannot_change_another_s_user_role(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    northwind, southwind = two_orgs
    victim = northwind.add_user("agent", email="agent@northwind.com")

    response = southwind.patch(f"{USERS}/{victim.user_id}/role", json={"role": "admin"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_the_target_of_a_refused_role_change_is_actually_unchanged(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The 404 must mean "nothing happened", not merely "the response said 404".

    Verified by asking the owning organization — and by asking the victim's own token,
    which is the strongest evidence that the row was untouched.
    """
    northwind, southwind = two_orgs
    victim = northwind.add_user("agent", email="agent@northwind.com")

    southwind.patch(f"{USERS}/{victim.user_id}/role", json={"role": "admin"})

    assert northwind.get(f"{USERS}/{victim.user_id}").json()["role"] == "agent"
    # Still an agent: the manager-only listing is still refused.
    assert victim.get(USERS).status_code == 403


def test_an_organization_cannot_deactivate_another_s_user(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    northwind, southwind = two_orgs
    victim = northwind.add_user("agent", email="agent@northwind.com")

    response = southwind.post(f"{USERS}/{victim.user_id}/deactivate")

    assert response.status_code == 404


def test_the_target_of_a_refused_deactivation_is_actually_still_active(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """Verified by the victim logging in, which is the consequence that matters.

    Asserting `is_active` on the row would pass even if the token path had been broken
    separately; a successful login is the user-visible truth.
    """
    northwind, southwind = two_orgs
    victim = northwind.add_user("agent", email="agent@northwind.com")

    southwind.post(f"{USERS}/{victim.user_id}/deactivate")

    assert victim.get(f"{AUTH}/me").status_code == 200
    relogin = victim.client.post(
        f"{AUTH}/login", json={"email": victim.email, "password": victim.password}
    )
    assert relogin.status_code == 200


def test_a_client_cannot_place_a_user_in_another_organization(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """There is no tenant field to set — and setting an undeclared one changes nothing.

    `organization_id` is not part of `UserCreate`, so it is discarded by validation
    rather than honoured. Asserted because "we do not accept that field" and "we
    accept it but ignore it" are both safe, while "we accept it" is a critical
    vulnerability, and only a test can tell the three apart.
    """
    northwind, southwind = two_orgs

    created = southwind.post(
        USERS,
        json={
            "name": "Planted",
            "email": "planted@southwind.com",
            "password": PASSWORD,
            "role": "agent",
            # Both spellings of an attempt to choose the tenant.
            "organization_id": northwind.user_id,
            "organizationId": northwind.user_id,
        },
    )

    assert created.status_code == 201
    planted_id = created.json()["id"]

    # It landed in the caller's organization, and is not visible to the other.
    assert planted_id in {user["id"] for user in southwind.get(USERS).json()}
    assert northwind.get(f"{USERS}/{planted_id}").status_code == 404


# ---------------------------------------------------------------------------
# Confused-deputy: a valid signature over a mismatched tenant
# ---------------------------------------------------------------------------


def test_a_token_naming_another_organization_is_refused(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The token is genuine — correctly signed, unexpired — and still refused.

    This is the case a signature check alone cannot catch: `org` in the claims names a
    different tenant from the one the user's row belongs to. The dependency compares
    the two and refuses, rather than trusting the claim (ADR-013). Without that
    comparison, anyone who could obtain a token for *any* organization could read every
    other one by editing a field they control.
    """
    northwind, southwind = two_orgs

    forged_tenant = create_access_token(
        uuid.UUID(northwind.user_id),
        uuid.UUID(southwind.user_id),  # a tenant this user does not belong to
        UserRole.ADMIN,  # and a role they would need
    )

    response = southwind.client.get(USERS, headers={"Authorization": f"Bearer {forged_tenant}"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_ACCESS_DENIED"


def test_a_token_for_another_tenant_cannot_reach_a_specific_record_either(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """Refused before the route body runs, so the id in the path is never looked up."""
    northwind, southwind = two_orgs

    forged_tenant = create_access_token(
        uuid.UUID(northwind.user_id), uuid.UUID(southwind.user_id), UserRole.ADMIN
    )

    response = southwind.client.get(
        f"{USERS}/{southwind.user_id}",
        headers={"Authorization": f"Bearer {forged_tenant}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_ACCESS_DENIED"


def test_a_token_naming_an_organization_that_does_not_exist_is_refused(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """Any mismatch is refused, not only a mismatch with another real tenant."""
    northwind, _ = two_orgs

    forged_tenant = create_access_token(uuid.UUID(northwind.user_id), uuid.uuid4(), UserRole.ADMIN)

    response = northwind.client.get(USERS, headers={"Authorization": f"Bearer {forged_tenant}"})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_ACCESS_DENIED"


def test_a_forged_tenant_claim_does_not_stop_the_user_s_own_token_working(
    two_orgs: tuple[OrgSession, OrgSession],
) -> None:
    """The refusal is about the token, and the legitimate session is unaffected.

    Without this, an implementation that revoked a user's sessions whenever a
    mismatched token appeared would pass every test above while handing an attacker a
    denial-of-service against any user whose id they could learn.
    """
    northwind, southwind = two_orgs

    forged = create_access_token(
        uuid.UUID(northwind.user_id), uuid.UUID(southwind.user_id), UserRole.ADMIN
    )
    northwind.client.get(USERS, headers={"Authorization": f"Bearer {forged}"})

    assert northwind.get(USERS).status_code == 200


# ---------------------------------------------------------------------------
# The same address in two tenants
# ---------------------------------------------------------------------------


def test_the_same_email_in_two_organizations_yields_two_independent_accounts(
    register_org: Callable[..., OrgSession],
) -> None:
    """Email is unique per tenant, not globally.

    A person may legitimately hold accounts with two support providers, and a global
    constraint would also disclose that the address exists somewhere else.
    """
    shared = "consultant@example.com"
    northwind = register_org(organization_name="Northwind", email=shared)
    southwind = register_org(organization_name="Southwind", email=shared)

    assert northwind.user_id != southwind.user_id
    assert northwind.get(f"{AUTH}/me").json()["id"] == northwind.user_id
    assert southwind.get(f"{AUTH}/me").json()["id"] == southwind.user_id


def test_shared_address_and_shared_password_would_be_ambiguous(
    register_org: Callable[..., OrgSession],
) -> None:
    """Why the test above uses distinct passwords, stated as a test.

    Login resolves an account from an email and a password alone — there is no tenant
    in the request to disambiguate with. Two accounts sharing both therefore cannot be
    told apart, and which one is returned depends on the order the candidates come back
    in. Asserted as "either, deterministically for a given credential" rather than
    pretending it is defined: the fix is to take the tenant from the request host (a
    subdomain per organization), which is beyond what a JSON API alone can express.

    This is the honest boundary of the current design, recorded where it can be found.
    """
    shared = "consultant@example.com"
    first = register_org(organization_name="Northwind", email=shared)
    second = register_org(organization_name="Southwind", email=shared)

    session = login(first.client, shared, PASSWORD)

    assert session.user_id in {first.user_id, second.user_id}
    # Repeated logins with the same credential resolve the same way.
    assert login(first.client, shared, PASSWORD).user_id == session.user_id


def test_each_of_two_accounts_sharing_an_address_sees_only_its_own_tenant(
    register_org: Callable[..., OrgSession],
) -> None:
    """Different passwords, so the credential selects the account unambiguously."""
    shared = "consultant@example.com"
    northwind = register_org(
        organization_name="Northwind", email=shared, password="northwind-password-1"
    )
    southwind = register_org(
        organization_name="Southwind", email=shared, password="southwind-password-2"
    )

    # Each credential resolves to its own account, and to no other.
    northwind_again = login(northwind.client, shared, "northwind-password-1")
    southwind_again = login(southwind.client, shared, "southwind-password-2")

    assert northwind_again.user_id == northwind.user_id
    assert southwind_again.user_id == southwind.user_id

    northwind.add_user("agent", email="agent@northwind.com")
    southwind.add_user("agent", email="agent@southwind.com")

    assert {user["email"] for user in northwind_again.get(USERS).json()} == {
        shared,
        "agent@northwind.com",
    }
    assert {user["email"] for user in southwind_again.get(USERS).json()} == {
        shared,
        "agent@southwind.com",
    }
