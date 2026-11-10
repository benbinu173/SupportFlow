"""Account recovery through real HTTP, PostgreSQL, and the email task's database read."""

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.core.security import (
    create_password_reset_token,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
    refresh_token_expiry,
)
from app.models.audit_log import AuditLog
from app.models.refresh_token import RefreshToken
from app.workers import email_tasks
from tests.conftest import AUTH, PASSWORD, USERS, OrgSession, cookie_header

pytestmark = pytest.mark.integration
NEW_PASSWORD = "new-password-for-this-workspace"


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[list[Any]]:
    entries: list[list[Any]] = []

    def enqueue(*, args: list[Any], **kwargs: Any) -> None:
        entries.append(args)

    monkeypatch.setattr(email_tasks.send_password_reset_email, "apply_async", enqueue)
    return entries


@pytest.fixture
def emails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    delivered: list[dict[str, str]] = []
    monkeypatch.setattr(email_tasks, "send_email", lambda **kwargs: delivered.append(kwargs))
    return delivered


def request_link(client: TestClient, email: str) -> Any:
    return client.post(f"{AUTH}/forgot-password", json={"email": email})


def token_for(org: OrgSession, *, version: int = 0, expired: bool = False) -> str:
    claims = decode_access_token(org.access_token)
    return create_password_reset_token(
        claims.user_id,
        claims.organization_id,
        version,
        expires_at=datetime.now(UTC) + timedelta(minutes=-1 if expired else 30),
    )


def reset(client: TestClient, token: str, password: str = NEW_PASSWORD) -> Any:
    return client.post(f"{AUTH}/reset-password", json={"token": token, "password": password})


def test_known_unknown_and_disabled_accounts_receive_the_same_public_response(
    client: TestClient, register_org: Callable[..., OrgSession], queued: list[list[Any]]
) -> None:
    owner = register_org()
    disabled = owner.add_user("agent")
    assert owner.post(f"{USERS}/{disabled.user_id}/deactivate").status_code == 200

    responses = [
        request_link(client, email)
        for email in (owner.email, disabled.email, "unknown@example.com")
    ]
    assert all(reply.status_code == 202 for reply in responses)
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert [args[0] for args in queued] == [owner.user_id]
    assert owner.get(f"{AUTH}/me").status_code == 200


def test_queue_failure_does_not_disclose_account_existence(
    client: TestClient,
    register_org: Callable[..., OrgSession],
    monkeypatch: pytest.MonkeyPatch,
    queued: list[list[Any]],
) -> None:
    owner = register_org()

    def unavailable(**kwargs: Any) -> None:
        raise OSError("broker unavailable")

    monkeypatch.setattr(email_tasks.send_password_reset_email, "apply_async", unavailable)
    assert (
        request_link(client, owner.email).json()
        == request_link(client, "unknown@example.com").json()
    )


def test_worker_sends_a_workspace_specific_fragment_link_without_a_broker_credential(
    client: TestClient,
    register_org: Callable[..., OrgSession],
    queued: list[list[Any]],
    emails: list[dict[str, str]],
) -> None:
    first = register_org(organization_name="First workspace", email="shared@example.com")
    second = register_org(organization_name="Second workspace", email="shared@example.com")
    assert request_link(client, first.email).status_code == 202
    assert {args[0] for args in queued} == {first.user_id, second.user_id}
    for args in queued:
        assert len(args) == 3 and isinstance(args[1], int) and isinstance(args[2], float)
        assert (
            email_tasks.send_password_reset_email.apply(args=args, throw=True).result == "delivered"
        )

    assert len(emails) == 2
    assert {mail["to"] for mail in emails} == {"shared@example.com"}
    assert {mail["subject"].split(" — ")[1] for mail in emails} == {
        "First workspace",
        "Second workspace",
    }
    link = next(line for line in emails[0]["body"].splitlines() if "/reset-password#" in line)
    url = urlsplit(link)
    token = parse_qs(url.fragment)["token"][0]
    assert url.query == ""
    assert token not in str(queued)
    assert reset(client, token).status_code == 200


def test_success_rejects_old_password_access_refresh_and_all_outstanding_links(
    client: TestClient,
    register_org: Callable[..., OrgSession],
    sync_engine: Engine,
) -> None:
    owner = register_org()
    token, sibling = token_for(owner), token_for(owner)
    result = reset(client, token)
    assert result.status_code == 200
    assert NEW_PASSWORD not in result.text and token not in result.text
    assert "Max-Age=0" in result.headers["set-cookie"]
    assert owner.get(f"{AUTH}/me").status_code == 401
    assert reset(client, token).status_code == reset(client, sibling).status_code == 400
    assert (
        client.post(f"{AUTH}/login", json={"email": owner.email, "password": PASSWORD}).status_code
        == 401
    )
    assert (
        client.post(f"{AUTH}/refresh", headers=cookie_header(owner.refresh_token or "")).status_code
        == 401
    )
    logged_in = client.post(f"{AUTH}/login", json={"email": owner.email, "password": NEW_PASSWORD})
    assert logged_in.status_code == 200
    assert decode_access_token(logged_in.json()["access_token"]).token_version == 1

    with Session(sync_engine) as session:
        rows = session.scalars(
            select(AuditLog).where(AuditLog.target_id == uuid.UUID(owner.user_id))
        ).all()
        reset_rows = [row for row in rows if row.action.value == "password_reset"]
        assert len(reset_rows) == 1
        assert token not in str(reset_rows[0].extra_data) and NEW_PASSWORD not in str(
            reset_rows[0].extra_data
        )


def test_a_same_email_account_in_another_tenant_keeps_its_password_and_sessions(
    client: TestClient,
    register_org: Callable[..., OrgSession],
) -> None:
    first = register_org(email="shared@example.com", password="first-workspace-password")
    second = register_org(email="shared@example.com", password="second-workspace-password")
    assert reset(client, token_for(first)).status_code == 200
    assert second.get(f"{AUTH}/me").status_code == 200
    assert (
        client.post(
            f"{AUTH}/login", json={"email": second.email, "password": second.password}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{AUTH}/refresh", headers=cookie_header(second.refresh_token or "")
        ).status_code
        == 200
    )


def test_a_revoked_pre_reset_cookie_cannot_revoke_a_new_password_session(
    client: TestClient,
    register_org: Callable[..., OrgSession],
) -> None:
    owner = register_org()
    assert reset(client, token_for(owner)).status_code == 200
    logged_in = client.post(f"{AUTH}/login", json={"email": owner.email, "password": NEW_PASSWORD})
    assert logged_in.status_code == 200
    new_cookie = logged_in.cookies[get_settings().REFRESH_COOKIE_NAME]

    assert (
        client.post(f"{AUTH}/refresh", headers=cookie_header(owner.refresh_token or "")).status_code
        == 401
    )
    assert client.post(f"{AUTH}/refresh", headers=cookie_header(new_cookie)).status_code == 200


@pytest.mark.parametrize("kind", ["expired", "tampered", "unknown-user", "wrong-tenant"])
def test_invalid_links_cannot_change_the_account_password(
    client: TestClient,
    register_org: Callable[..., OrgSession],
    kind: str,
) -> None:
    owner = register_org()
    token = token_for(owner, expired=kind == "expired")
    if kind in {"unknown-user", "wrong-tenant"}:
        claims = decode_access_token(owner.access_token)
        token = create_password_reset_token(
            uuid.uuid4() if kind == "unknown-user" else claims.user_id,
            uuid.uuid4() if kind == "wrong-tenant" else claims.organization_id,
            0,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    if kind == "tampered":
        token = token[:-5] + "xxxxx"
    reply = reset(client, token)
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "INVALID_RESET_TOKEN"
    assert owner.get(f"{AUTH}/me").status_code == 200


def test_password_validation_does_not_consume_the_link(
    client: TestClient,
    register_org: Callable[..., OrgSession],
) -> None:
    token = token_for(register_org())
    reply = reset(client, token, "short")
    assert reply.status_code == 422
    assert token not in reply.text and "short" not in reply.text
    assert reset(client, token).status_code == 200


def test_concurrent_consumers_cannot_both_change_the_password(
    client: TestClient,
    register_org: Callable[..., OrgSession],
) -> None:
    token = token_for(register_org())
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(
                lambda password: reset(client, token, password),
                [NEW_PASSWORD, "another-long-new-password"],
            )
        )
    assert sorted(reply.status_code for reply in replies) == [200, 400]


def test_reset_closes_the_accounts_existing_local_socket(
    client: TestClient, register_org: Callable[..., OrgSession]
) -> None:
    owner = register_org()
    with client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "auth", "token": owner.access_token})
        assert socket.receive_json() == {"type": "authenticated"}
        assert reset(client, token_for(owner)).status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401


def test_socket_rechecks_a_reset_without_an_in_process_revocation_signal(
    client: TestClient, register_org: Callable[..., OrgSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.websocket.manager import manager

    owner = register_org()
    monkeypatch.setattr(get_settings(), "WS_SESSION_CHECK_INTERVAL_SECONDS", 1)
    monkeypatch.setattr(manager, "revoke_user", lambda *args: None)
    with client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "auth", "token": owner.access_token})
        assert socket.receive_json() == {"type": "authenticated"}
        assert reset(client, token_for(owner)).status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401


def test_a_late_old_refresh_row_still_cannot_restore_a_reset_session(
    client: TestClient,
    register_org: Callable[..., OrgSession],
    sync_engine: Engine,
) -> None:
    owner = register_org()
    claims = decode_access_token(owner.access_token)
    assert reset(client, token_for(owner)).status_code == 200
    logged_in = client.post(f"{AUTH}/login", json={"email": owner.email, "password": NEW_PASSWORD})
    assert logged_in.status_code == 200
    new_cookie = logged_in.cookies[get_settings().REFRESH_COOKIE_NAME]
    # Model the row a rotation racing the reset could commit after the revocation UPDATE.
    raw = generate_refresh_token()
    with Session(sync_engine) as session:
        session.add(
            RefreshToken(
                user_id=claims.user_id,
                organization_id=claims.organization_id,
                token_hash=hash_refresh_token(raw),
                token_version=0,
                expires_at=refresh_token_expiry(),
            )
        )
        session.commit()
    assert client.post(f"{AUTH}/refresh", headers=cookie_header(raw)).status_code == 401
    assert client.post(f"{AUTH}/refresh", headers=cookie_header(new_cookie)).status_code == 200


def test_expired_or_superseded_queued_emails_are_not_sent(
    client: TestClient,
    register_org: Callable[..., OrgSession],
    queued: list[list[Any]],
    emails: list[dict[str, str]],
) -> None:
    owner = register_org()
    request_link(client, owner.email)
    assert reset(client, token_for(owner)).status_code == 200
    assert (
        email_tasks.send_password_reset_email.apply(args=queued[0], throw=True).result
        == "no-recipient"
    )
    expired = [owner.user_id, 1, (datetime.now(UTC) - timedelta(seconds=1)).timestamp()]
    assert email_tasks.send_password_reset_email.apply(args=expired, throw=True).result == "expired"
    assert emails == []


def test_recovery_is_rate_limited_before_an_email_is_queued(
    client: TestClient,
    register_org: Callable[..., OrgSession],
    queued: list[list[Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = register_org(email=f"rate-limit-{uuid.uuid4().hex}@example.com")
    monkeypatch.setattr(get_settings(), "RATE_LIMIT_PASSWORD_RESET_EMAIL_PER_HOUR", 1)
    assert request_link(client, owner.email).status_code == 202
    blocked = request_link(client, owner.email)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert len(queued) == 1
