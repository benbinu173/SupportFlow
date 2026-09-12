"""Secrets must not reach the logs.

Spec §4: no tokens or passwords in logs. This is the kind of requirement that is
satisfied today, by review, and quietly broken by the next `logger.info("login", ...)`
someone adds while debugging — which is why it is asserted mechanically rather than
described in a comment.

The approach is to **record what the application logs** during a complete
authentication lifecycle and assert that none of the secrets it handled appears in any
of it. That is stronger than grepping the source for logger calls, because it also
covers values that reach a log indirectly, through a field the caller passed in.

A positive control is essential: a recorder that captured nothing would satisfy every
assertion below while proving nothing. `test_the_recorder_actually_captured_the_session`
is what stops that.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.conftest import (
    AUTH,
    CUSTOMERS,
    PASSWORD,
    TICKETS,
    USERS,
    OrgSession,
    cookie_header,
    login,
)

pytestmark = pytest.mark.security

# Every module in the request path that holds a module-level logger. Listed explicitly
# rather than discovered by walking `sys.modules`, so that adding a module with a logger
# is a deliberate addition here rather than something silently left uncovered.
#
# The `api` routers are absent because they hold no logger — logging is the service
# layer's job, and a route that logged on its own would bypass the services listed here.
LOGGING_MODULES = (
    "app.api.auth",
    "app.api.deps",
    "app.main",
    "app.core.rate_limit",
    "app.services.auth_service",
    "app.services.customer_service",
    "app.services.message_service",
    "app.services.ticket_service",
    "app.services.user_service",
)

# The events this file relies on. Asserted present so that a rename shows up as a
# failure here rather than as a silently vacuous "no secrets found". A set rather than a
# tuple, because it is compared against `LogRecorder.events`.
EXPECTED_EVENTS = frozenset(
    {
        "organization_registered",
        "login_succeeded",
        "login_failed",
        "refresh_token_rotated",
        "refresh_token_reuse_detected",
        "logout",
        "permission_denied",
        # Phases I-K. Each one is a log line that carries an id, a status, or an actor —
        # never the subject, the description, or the body it is describing.
        "customer_created",
        "customer_updated",
        "ticket_created",
        "ticket_assigned",
        "ticket_priority_changed",
        "ticket_status_changed",
        "ticket_closed",
        "ticket_reopened",
        "message_posted",
    }
)

WRONG_PASSWORD = "a-wrong-password-that-is-still-a-secret"

# A password for the portal account, distinct from `PASSWORD`, so that "the password
# never reaches the logs" is a claim about this run rather than about a constant that
# happens to be shared by every other account in the suite.
PORTAL_PASSWORD = "a-portal-password-that-is-also-a-secret"


class LogRecorder:
    """A stand-in for a structlog logger that keeps every call.

    `structlog.get_logger()` returns an object whose level methods are resolved
    dynamically, so `__getattr__` is the right hook: anything the application calls is
    recorded under whatever name it used, and an unrecognised level is captured rather
    than raising.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str) -> Any:
        def record(event: object, **fields: Any) -> None:
            self.calls.append((level, str(event), fields))

        return record

    def __getattr__(self, level: str) -> Any:
        return self._record(level)

    @property
    def events(self) -> set[str]:
        return {event for _, event, _ in self.calls}

    def as_text(self) -> str:
        """Everything logged, rendered into one searchable string.

        Fields go through `repr` so a secret nested inside a container is still found,
        and so a value embedded in a longer string is caught by a substring search.
        """
        return "\n".join(f"{level} {event} {fields!r}" for level, event, fields in self.calls)


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """See `test_tenant_isolation.py` for why this is declared per-file."""


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> LogRecorder:
    """Replace every module logger in the request path with one recorder."""
    recorder = LogRecorder()
    for module in LOGGING_MODULES:
        monkeypatch.setattr(f"{module}.logger", recorder)
    return recorder


def _refresh(client: TestClient, token: str) -> Any:
    """Present a refresh token as an explicit header.

    The client's cookie jar is emptied first. It is session-scoped and shared, so a
    cookie written into it by an earlier response would otherwise be merged into this
    request alongside the header — making the request carry two tokens, one of them
    from a previous test.
    """
    client.cookies.clear()
    return client.post(f"{AUTH}/refresh", headers=cookie_header(token))


@pytest.fixture
def exercised(
    logs: LogRecorder, client: TestClient, register_org: Callable[..., OrgSession]
) -> dict[str, str]:
    """Drive a full session — success, failure, rotation, reuse, refusal, logout.

    Then a second pass through the customer, ticket, and message services, because those
    are where the phase's new log lines are and a log-hygiene test that never reached
    them would keep passing while covering none of them. Several of those calls carry a
    body of customer-written text, which is the value most likely to be interpolated into
    an event name by accident.

    Returns the secrets that were handled, so the tests search for exactly the values
    this run produced rather than for fixed strings.
    """
    org = register_org()

    # A successful login: the call most likely to log its own arguments by accident.
    session = login(client, org.email, org.password)

    # A failed one, whose password is wrong but still a secret.
    client.post(f"{AUTH}/login", json={"email": org.email, "password": WRONG_PASSWORD})

    # A capability refusal...
    agent = org.add_user("agent", email="agent@loghygiene.com")
    agent.post(
        USERS,
        json={
            "name": "Nope",
            "email": "nope@loghygiene.com",
            "password": PASSWORD,
            "role": "agent",
        },
    )
    # ...and a permitted read, for contrast.
    assert org.get(f"{USERS}/{agent.user_id}").status_code == 200

    # Rotation, then reuse of the superseded token — the loudest path in the service.
    rotated = _refresh(client, session.refresh_token or "")
    rotated_refresh = rotated.cookies.get(get_settings().REFRESH_COOKIE_NAME) or ""
    reused = _refresh(client, session.refresh_token or "")

    # Reuse detection revokes *every* live token for that user, so the sessions above
    # are dead. Logging out needs a live one, so start a fresh session first.
    fresh = login(client, org.email, org.password)
    logout = client.post(
        f"{AUTH}/logout",
        headers={**fresh.auth, **cookie_header(fresh.refresh_token or "")},
    )

    assert reused.status_code == 401, reused.text
    assert logout.status_code == 200, logout.text

    # --- Phases I-K, through the real services ---------------------------------
    # A portal account is created here rather than through `add_portal_user`, so that the
    # password is one this test chooses and can therefore search for.
    customer = org.add_customer(name="Dana Scully", email="dana@loghygiene.com")
    portal = org.post(
        USERS,
        json={
            "name": "Dana Scully",
            "email": "dana@loghygiene.com",
            "password": PORTAL_PASSWORD,
            "role": "customer",
            "customer_id": customer["id"],
        },
    )
    assert portal.status_code == 201, portal.text

    # The portal account logging in as itself, which is the path a `customer_id`-linked
    # session actually travels — and the only way to obtain a token for it, since
    # `POST /users` returns the created user rather than a session.
    portal_session = login(client, "dana@loghygiene.com", PORTAL_PASSWORD)
    assert portal_session.role == "customer", portal_session.role

    updated = org.patch(f"{CUSTOMERS}/{customer['id']}", json={"phone": "+1 555 0100"})
    assert updated.status_code == 200, updated.text

    ticket = org.add_ticket(
        customer["id"], subject="The observatory is offline", description="DEEP_THROAT"
    )
    ticket_id = ticket["id"]

    assigned = org.post(f"{TICKETS}/{ticket_id}/assign", json={"assigned_agent_id": agent.user_id})
    assert assigned.status_code == 200, assigned.text
    priority = org.post(f"{TICKETS}/{ticket_id}/priority", json={"priority": "high"})
    assert priority.status_code == 200, priority.text
    for status in ("in_progress", "resolved"):
        moved = org.post(f"{TICKETS}/{ticket_id}/status", json={"status": status})
        assert moved.status_code == 200, moved.text
    closed = org.post(f"{TICKETS}/{ticket_id}/close")
    assert closed.status_code == 200, closed.text
    reopened = org.post(f"{TICKETS}/{ticket_id}/reopen")
    assert reopened.status_code == 200, reopened.text

    # Reopening clears the assignment, so the agent cannot reach the ticket until it is
    # given back to them — and the messages below would 404 rather than being logged.
    reassigned = org.post(
        f"{TICKETS}/{ticket_id}/assign", json={"assigned_agent_id": agent.user_id}
    )
    assert reassigned.status_code == 200, reassigned.text

    # Two posts down the two routes: `message_posted` is the event name for both, so
    # only the internal one would be missed if this stopped after the first.
    reply = agent.post(f"{TICKETS}/{ticket_id}/messages", json={"body": "MULDER"})
    note = agent.post(f"{TICKETS}/{ticket_id}/notes", json={"body": "Skinner knows"})

    assert reply.status_code == 201, reply.text
    assert note.status_code == 201, note.text

    return {
        "password": org.password,
        "access_token": session.access_token,
        "rotated_access_token": rotated.json().get("access_token") or "",
        "refresh_token": session.refresh_token or "",
        "rotated_refresh_token": rotated_refresh,
        "portal_password": PORTAL_PASSWORD,
        "portal_access_token": portal_session.access_token,
    }


def test_the_recorder_actually_captured_the_session(
    logs: LogRecorder, exercised: dict[str, str]
) -> None:
    """The positive control, without which every assertion below is vacuous.

    A mis-patched logger — a module renamed, or a logger built per request rather than
    at import — would record nothing, and every "no secret found" assertion would pass
    while covering no code at all.
    """
    assert logs.calls, "nothing was logged: the recorder is not wired to the app"
    missing = EXPECTED_EVENTS - logs.events
    assert not missing, f"events never logged: {sorted(missing)}"


def test_no_password_reaches_the_logs(logs: LogRecorder, exercised: dict[str, str]) -> None:
    text = logs.as_text()

    assert exercised["password"] not in text
    assert WRONG_PASSWORD not in text
    # A prefix, in case something logs only part of it.
    assert exercised["password"][:8] not in text


def test_no_access_token_reaches_the_logs(logs: LogRecorder, exercised: dict[str, str]) -> None:
    text = logs.as_text()

    assert exercised["access_token"] not in text
    assert exercised["rotated_access_token"] not in text
    # Even the payload segment alone would be a disclosure.
    assert exercised["access_token"].split(".")[1] not in text


def test_no_refresh_token_reaches_the_logs(logs: LogRecorder, exercised: dict[str, str]) -> None:
    """The one that matters most: a refresh token is a long-lived session."""
    text = logs.as_text()

    assert exercised["refresh_token"] not in text
    assert exercised["rotated_refresh_token"] not in text
    assert exercised["refresh_token"][:16] not in text


def test_the_authorization_header_is_not_logged(
    logs: LogRecorder, exercised: dict[str, str]
) -> None:
    """A header logged wholesale would carry the bearer token with it."""
    text = logs.as_text()

    assert "Bearer " not in text
    assert "Authorization" not in text


def test_a_password_hash_is_never_logged(logs: LogRecorder, exercised: dict[str, str]) -> None:
    """An Argon2 hash is credential-equivalent for offline cracking, so it is not a
    log-safe value either."""
    assert "$argon2" not in logs.as_text()


def test_no_portal_password_or_token_reaches_the_logs(
    logs: LogRecorder, exercised: dict[str, str]
) -> None:
    """The same two claims for the portal path.

    Phases I-K introduced a second way a password enters the system — an admin creating
    a login for a customer — and a second way a session begins. Both go through
    `user_service` and `auth_service`, which are already in `LOGGING_MODULES`, but a
    service can log a field on one code path and not another, and the assertion is only
    as broad as the requests the fixture actually made.
    """
    text = logs.as_text()

    assert exercised["portal_password"] not in text
    assert exercised["portal_password"][:8] not in text
    assert exercised["portal_access_token"] not in text
    assert exercised["portal_access_token"].split(".")[1] not in text
