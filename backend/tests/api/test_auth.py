"""Authentication endpoints, end to end.

Every test here goes through HTTP: organizations are registered, users are created by
calling `POST /users`, and sessions are established by calling `POST /auth/login`. No
test inserts a row directly, so a passing suite is evidence that the API works rather
than evidence that the ORM does.

Nothing asserts on a status code alone where the §42 error code is the contract a
client actually reads.
"""

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.conftest import AUTH, PASSWORD, USERS, OrgSession, cookie_header

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_creates_an_organization_and_signs_the_admin_in(
    register_org: Callable[..., OrgSession],
) -> None:
    """Registration returns a usable session immediately — no second round trip."""
    org = register_org(organization_name="Globex")

    assert org.role == "admin"
    assert org.access_token
    # The refresh token is delivered as a cookie, never in the body.
    assert org.refresh_token

    me = org.get(f"{AUTH}/me")
    assert me.status_code == 200
    assert me.json()["email"] == org.email
    assert me.json()["role"] == "admin"
    assert me.json()["is_active"] is True


def test_registration_never_returns_the_password(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    """No response may echo a credential back."""
    response = client.post(
        f"{AUTH}/register",
        json={
            "organization_name": "Initech",
            "name": "Admin",
            "email": "admin@initech.com",
            "password": PASSWORD,
        },
    )
    assert PASSWORD not in response.text
    assert "password_hash" not in response.text


def test_validation_errors_do_not_echo_the_submitted_password(
    client: TestClient,
) -> None:
    """A rejected password must not come back in the error body.

    Pydantic's error dicts normally include the offending `input`, which on a
    registration payload is the plaintext password. The handler strips it — this test
    is what keeps it stripped, since the leak would otherwise be invisible.
    """
    short_password = "tooshort"
    response = client.post(
        f"{AUTH}/register",
        json={
            "organization_name": "Umbrella",
            "name": "Admin",
            "email": "admin@umbrella.com",
            "password": short_password,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert short_password not in response.text
    assert response.json()["details"]


def test_register_rejects_a_malformed_email(client: TestClient) -> None:
    response = client.post(
        f"{AUTH}/register",
        json={
            "organization_name": "Soylent",
            "name": "Admin",
            "email": "not-an-address",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_same_email_can_register_two_different_organizations(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    """Email is unique per tenant, not globally.

    The same person may legitimately hold accounts with two support providers, and a
    global unique constraint would also disclose that an account exists elsewhere.
    """
    shared = "consultant@example.com"
    first = register_org(organization_name="Provider One", email=shared)
    second = register_org(organization_name="Provider Two", email=shared)

    assert first.user_id != second.user_id

    # Both accounts remain independently usable.
    assert first.get(f"{AUTH}/me").status_code == 200
    assert second.get(f"{AUTH}/me").status_code == 200


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_succeeds_with_correct_credentials(register_org: Callable[..., OrgSession]) -> None:
    org = register_org()
    response = org.client.post(f"{AUTH}/login", json={"email": org.email, "password": PASSWORD})

    assert response.status_code == 200
    assert response.json()["access_token"]
    assert response.json()["expires_in"] > 0
    assert response.cookies.get(get_settings().REFRESH_COOKIE_NAME)


def test_login_is_case_insensitive_about_the_email(
    register_org: Callable[..., OrgSession],
) -> None:
    """Addresses are normalized on the way in, so capitalizing it still works."""
    org = register_org(email="mixed.case@example.com")
    response = org.client.post(
        f"{AUTH}/login", json={"email": "Mixed.Case@Example.COM", "password": PASSWORD}
    )
    assert response.status_code == 200


def test_a_wrong_password_and_an_unknown_email_are_indistinguishable(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    """The two failures must not be tellable apart.

    If they were, the login form would answer the question "does this address have an
    account here?" for anyone who asked, across every tenant.
    """
    org = register_org()

    wrong_password = client.post(
        f"{AUTH}/login", json={"email": org.email, "password": "not-the-password-at-all"}
    )
    unknown_email = client.post(
        f"{AUTH}/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_a_deactivated_user_cannot_log_in(register_org: Callable[..., OrgSession]) -> None:
    admin = register_org()
    agent = admin.add_user("agent")

    deactivated = admin.post(f"{USERS}/{agent.user_id}/deactivate")
    assert deactivated.status_code == 200

    response = agent.client.post(
        f"{AUTH}/login", json={"email": agent.email, "password": agent.password}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_login_is_rate_limited(
    register_org: Callable[..., OrgSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route is wired to the limiter and renders 429 in the §42 envelope.

    The limit is set to zero rather than to a small number so the assertion does not
    depend on how many logins earlier tests have already counted against this address
    in Redis. The counting logic itself is covered by `tests/unit/test_rate_limit.py`
    against a stand-in client; this test is about the wiring.
    """
    monkeypatch.setattr(get_settings(), "RATE_LIMIT_LOGIN_PER_MINUTE", 0, raising=False)

    org = register_org()
    response = org.client.post(f"{AUTH}/login", json={"email": org.email, "password": PASSWORD})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.headers.get("Retry-After")


# ---------------------------------------------------------------------------
# The access token
# ---------------------------------------------------------------------------


def test_me_requires_a_token(client: TestClient) -> None:
    response = client.get(f"{AUTH}/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_me_rejects_a_token_signed_with_another_secret(client: TestClient) -> None:
    """A forged signature must not be honoured.

    Hand-built rather than obtained from the app, so this is a genuine test of the
    signature check rather than of a code path that would produce the same token.
    """
    import jwt

    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "org": str(uuid.uuid4()),
            "role": "admin",
            "exp": 9999999999,
        },
        "a-different-secret-that-is-also-long-enough",
        algorithm="HS256",
    )

    response = client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_me_rejects_a_tampered_token(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    """Flipping a character in the payload invalidates the signature."""
    org = register_org()
    header, payload, signature = org.access_token.split(".")

    tampered = f"{header}.{payload[:-2]}XY.{signature}"
    response = client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {tampered}"})
    assert response.status_code == 401


def test_an_expired_token_is_rejected(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    """Expiry is enforced, not merely present in the claims."""
    from datetime import timedelta

    from app.core.security import create_access_token
    from app.models.enums import UserRole

    org = register_org()
    expired = create_access_token(
        uuid.UUID(org.user_id),
        uuid.UUID(int=0),
        UserRole.ADMIN,
        expires_delta=timedelta(seconds=-1),
    )

    response = client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


def test_an_admin_who_is_demoted_loses_access_immediately(
    register_org: Callable[..., OrgSession],
) -> None:
    """The database role wins over the role in the token (ADR-013).

    The agent's access token still claims admin — it was minted before the demotion
    and has not expired — but the request is served with the role stored in the
    database, so the admin-only listing is refused right away rather than fifteen
    minutes later.
    """
    admin = register_org()
    colleague = admin.add_user("admin", email="colleague@example.com")
    assert colleague.get(USERS).status_code == 200

    demoted = admin.patch(f"{USERS}/{colleague.user_id}/role", json={"role": "agent"})
    assert demoted.status_code == 200

    # Same token, same session — only the database row changed.
    assert colleague.get(USERS).status_code == 403


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def _refresh(client: TestClient, token: str) -> object:
    """Post a refresh token explicitly.

    Passed per-request rather than left in the client's cookie jar, so a test with two
    organizations cannot accidentally refresh as the wrong one.
    """
    return client.post(f"{AUTH}/refresh", headers=cookie_header(token))


def test_refresh_issues_a_usable_access_token(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    """The token returned by `/refresh` authenticates subsequent requests.

    Note what is deliberately *not* asserted: that the new token differs from the old
    one. A JWT is a deterministic encoding of its claims, and a refresh within the same
    second produces identical `iat` and `exp` for the same user — so the two strings
    are byte-identical, and asserting otherwise would be asserting a property the
    format does not have. It is also not a problem: refreshing rotates the *refresh*
    token, which is the long-lived credential; access tokens simply expire.
    """
    org = register_org()
    assert org.refresh_token

    response = _refresh(client, org.refresh_token)
    assert response.status_code == 200

    new_token = response.json()["access_token"]
    me = client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {new_token}"})
    assert me.status_code == 200
    assert me.json()["id"] == org.user_id


def test_refresh_rotates_the_refresh_token(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    """Each success invalidates the token presented and issues a replacement."""
    org = register_org()
    settings = get_settings()

    first = _refresh(client, org.refresh_token)
    replacement = first.cookies.get(settings.REFRESH_COOKIE_NAME)

    assert replacement
    assert replacement != org.refresh_token


def test_reusing_a_refresh_token_revokes_the_whole_family(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    """Replay detection, and the response to it.

    A token that has already been rotated out can only be presented by someone who
    captured it — the legitimate client no longer has it. The honest conclusion is
    "one of this user's sessions is compromised, we do not know which", so every live
    session is revoked. In particular the *replacement* token dies too, which is what
    this asserts: stopping the attacker while leaving them a working session would
    defeat the point.
    """
    org = register_org()
    settings = get_settings()

    rotated = _refresh(client, org.refresh_token)
    assert rotated.status_code == 200
    replacement = rotated.cookies.get(settings.REFRESH_COOKIE_NAME)
    assert replacement

    replay = _refresh(client, org.refresh_token)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    after_reuse = _refresh(client, replacement)
    assert after_reuse.status_code == 401


def test_an_unknown_refresh_token_is_rejected(client: TestClient) -> None:
    response = _refresh(client, "not-a-token-that-was-ever-issued")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_refresh_without_a_cookie_is_rejected(client: TestClient) -> None:
    response = client.post(f"{AUTH}/refresh")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_revokes_the_presented_token(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    org = register_org()
    assert org.refresh_token

    response = org.post(f"{AUTH}/logout", headers=cookie_header(org.refresh_token))
    assert response.status_code == 200

    # The token that was just revoked no longer works.
    assert _refresh(client, org.refresh_token).status_code == 401


def test_logout_requires_authentication(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    """Unlike refresh, logout needs a live access token.

    A client whose access token has expired calls `/refresh` first, which is a normal
    flow. Keeping the requirement means every non-public route is authenticated, which
    a test enforces mechanically.
    """
    org = register_org()
    response = client.post(f"{AUTH}/logout", headers=cookie_header(org.refresh_token or ""))
    assert response.status_code == 401


def test_logout_is_idempotent(register_org: Callable[..., OrgSession]) -> None:
    """Signing out twice is not an error, and does not disclose anything.

    Reporting "that token does not exist" would let anyone holding a stolen token ask
    whether it is still live.
    """
    org = register_org()
    assert org.refresh_token

    assert org.refresh_token
    first = org.post(f"{AUTH}/logout", headers=cookie_header(org.refresh_token))
    second = org.post(f"{AUTH}/logout", headers=cookie_header(org.refresh_token))

    assert first.status_code == second.status_code == 200
