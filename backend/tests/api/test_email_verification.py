"""Real database, HTTP, worker delivery, and mailbox-link consumption."""

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
)
from app.models.audit_log import AuditLog
from app.models.enums import AuditAction, OrganizationStatus, UserRole
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.workers import email_tasks
from tests.conftest import AUTH, PASSWORD, USERS, OrgSession

pytestmark = pytest.mark.integration


def create_account(
    client: TestClient,
    queued: list[list[Any]],
    *,
    email: str = "pending@example.com",
    workspace: str = "Pending workspace",
) -> list[Any]:
    response = client.post(
        f"{AUTH}/register",
        json={
            "organization_name": workspace,
            "name": "Owner",
            "email": email,
            "password": PASSWORD,
        },
    )
    assert response.status_code == 201
    assert "access_token" not in response.json()
    assert not response.cookies
    assert response.json()["email"] == email
    return queued[-1]


def link(
    args: list[Any], *, expired: bool = False, organization_id: uuid.UUID | None = None
) -> str:
    return create_email_verification_token(
        uuid.UUID(args[0]),
        organization_id or uuid.UUID(args[1]),
        args[2],
        expires_at=datetime.now(UTC) + timedelta(minutes=-1 if expired else 60),
    )


def confirm(client: TestClient, token: str) -> Any:
    return client.post(f"{AUTH}/verify-email", json={"token": token})


def test_registration_commits_account_without_issuing_any_session(
    client: TestClient, queued_verifications: list[list[Any]], sync_engine: Engine
) -> None:
    args = create_account(client, queued_verifications)
    with Session(sync_engine) as session:
        user = session.get(User, uuid.UUID(args[0]))
        assert user is not None and user.email_verified_at is None and user.last_login_at is None
        assert session.scalar(select(func.count()).select_from(RefreshToken)) == 0
    login = client.post(
        f"{AUTH}/login", json={"email": "pending@example.com", "password": PASSWORD}
    )
    assert login.status_code == 403 and login.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"
    wrong = client.post(f"{AUTH}/login", json={"email": "pending@example.com", "password": "wrong"})
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_unverified_access_and_websocket_credentials_are_refused(
    client: TestClient, queued_verifications: list[list[Any]]
) -> None:
    args = create_account(client, queued_verifications)
    access = create_access_token(uuid.UUID(args[0]), uuid.UUID(args[1]), UserRole.ADMIN)
    for path in (f"{AUTH}/me", "/api/v1/tickets"):
        response = client.get(path, headers={"Authorization": f"Bearer {access}"})
        assert (
            response.status_code == 403 and response.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"
        )
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": access})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_success_persists_one_audit_entry_and_makes_all_sibling_links_unusable(
    client: TestClient, queued_verifications: list[list[Any]], sync_engine: Engine
) -> None:
    args = create_account(client, queued_verifications)
    first, sibling = link(args), link(args)
    response = confirm(client, first)
    assert response.status_code == 200 and first not in response.text
    assert response.json()["organization_slug"] == "pending-workspace"
    for value in (first, sibling):
        refused = confirm(client, value)
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "INVALID_VERIFICATION_TOKEN"
    login = client.post(
        f"{AUTH}/login", json={"email": "pending@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200
    with Session(sync_engine) as session:
        user = session.get(User, uuid.UUID(args[0]))
        assert user is not None and user.email_verified_at is not None and user.token_version == 0
        rows = session.scalars(
            select(AuditLog).where(AuditLog.action == AuditAction.EMAIL_VERIFIED)
        ).all()
        assert len(rows) == 1 and rows[0].target_id == user.id
        assert first not in str(rows[0].extra_data)


def test_simultaneous_consumption_succeeds_only_once(
    client: TestClient, queued_verifications: list[list[Any]]
) -> None:
    token = link(create_account(client, queued_verifications))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(client, token).status_code, range(2)))
    assert sorted(results) == [200, 400]


@pytest.mark.parametrize(
    "state", ["expired", "wrong-tenant", "changed-email", "disabled", "suspended", "deleted"]
)
def test_unusable_links_share_one_response(
    client: TestClient, queued_verifications: list[list[Any]], sync_engine: Engine, state: str
) -> None:
    args = create_account(client, queued_verifications)
    token = link(
        args,
        expired=state == "expired",
        organization_id=uuid.uuid4() if state == "wrong-tenant" else None,
    )
    with Session(sync_engine) as session:
        user = session.get(User, uuid.UUID(args[0]))
        assert user is not None
        if state == "changed-email":
            user.email = "changed@example.com"
        elif state == "disabled":
            user.is_active = False
        elif state == "suspended":
            user.organization.status = OrganizationStatus.SUSPENDED
        elif state == "deleted":
            session.delete(user)
        session.commit()
    response = confirm(client, token)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_VERIFICATION_TOKEN"
    assert token not in response.text


def test_resend_is_indistinguishable_for_unknown_verified_and_disabled_accounts(
    client: TestClient,
    register_org: Callable[..., OrgSession],
    queued_verifications: list[list[Any]],
    sync_engine: Engine,
) -> None:
    verified = register_org()
    disabled = create_account(client, queued_verifications, email="disabled@example.com")
    with Session(sync_engine) as session:
        user = session.get(User, uuid.UUID(disabled[0]))
        assert user is not None
        user.is_active = False
        session.commit()
    create_account(client, queued_verifications)
    queued_verifications.clear()
    results = [
        client.post(f"{AUTH}/resend-verification", json={"email": email})
        for email in (
            "pending@example.com",
            verified.email,
            "disabled@example.com",
            "unknown@example.com",
        )
    ]
    assert all(response.status_code == 202 for response in results)
    assert all(response.json() == results[0].json() for response in results)
    assert len(queued_verifications) == 1


def test_broker_failure_retains_the_account_and_generic_resend_response(
    client: TestClient,
    queued_verifications: list[list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    sync_engine: Engine,
) -> None:
    def unavailable(**kwargs: Any) -> None:
        raise OSError("broker unavailable")

    monkeypatch.setattr(email_tasks.send_verification_email, "apply_async", unavailable)
    response = client.post(
        f"{AUTH}/register",
        json={
            "organization_name": "Queue outage",
            "name": "Owner",
            "email": "pending@example.com",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 201
    with Session(sync_engine) as session:
        assert session.scalar(select(User).where(User.email == "pending@example.com")) is not None
    known = client.post(f"{AUTH}/resend-verification", json={"email": "pending@example.com"})
    unknown = client.post(f"{AUTH}/resend-verification", json={"email": "unknown@example.com"})
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()


def test_worker_sends_separate_workspace_links_and_skips_stale_or_wrong_tenant_tasks(
    client: TestClient, queued_verifications: list[list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = create_account(
        client, queued_verifications, email="shared@example.com", workspace="First workspace"
    )
    second = create_account(
        client, queued_verifications, email="shared@example.com", workspace="Second workspace"
    )
    emails: list[dict[str, str]] = []
    monkeypatch.setattr(email_tasks, "send_email", lambda **kwargs: emails.append(kwargs))
    for args in (first, second):
        assert len(args) == 4 and "shared@example.com" not in str(args)
        assert (
            email_tasks.send_verification_email.apply(args=args, throw=True).result == "delivered"
        )
    assert {mail["subject"].split(" — ")[1] for mail in emails} == {
        "First workspace",
        "Second workspace",
    }
    for mail, args in zip(emails, (first, second), strict=True):
        url = urlsplit(next(line for line in mail["body"].splitlines() if "/verify-email#" in line))
        token = parse_qs(url.fragment)["token"][0]
        assert not url.query and token not in str(queued_verifications)
        verified = confirm(client, token)
        assert verified.status_code == 200
        login = client.post(
            f"{AUTH}/login",
            json={
                "email": "shared@example.com",
                "password": PASSWORD,
                "organization_slug": verified.json()["organization_slug"],
            },
        )
        assert login.status_code == 200
        from app.core.security import decode_access_token

        assert str(decode_access_token(login.json()["access_token"]).user_id) == args[0]
        assert (
            email_tasks.send_verification_email.apply(args=args, throw=True).result
            == "no-recipient"
        )
    wrong = [first[0], str(uuid.uuid4()), first[2], first[3]]
    assert (
        email_tasks.send_verification_email.apply(args=wrong, throw=True).result == "no-recipient"
    )
    expired = [first[0], first[1], first[2], (datetime.now(UTC) - timedelta(minutes=1)).timestamp()]
    assert email_tasks.send_verification_email.apply(args=expired, throw=True).result == "expired"


def test_admin_created_accounts_also_require_verification(
    client: TestClient,
    register_org: Callable[..., OrgSession],
    queued_verifications: list[list[Any]],
) -> None:
    owner = register_org()
    response = owner.post(
        USERS,
        json={
            "name": "New agent",
            "email": "agent@example.com",
            "password": PASSWORD,
            "role": "agent",
        },
    )
    assert response.status_code == 201 and response.json()["email_verified_at"] is None
    assert queued_verifications[-1][0] == response.json()["id"]
    assert (
        client.post(
            f"{AUTH}/login", json={"email": "agent@example.com", "password": PASSWORD}
        ).status_code
        == 403
    )
    assert confirm(client, link(queued_verifications[-1])).status_code == 200
    assert (
        client.post(
            f"{AUTH}/login", json={"email": "agent@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )


def test_password_recovery_does_not_silently_verify_email(
    client: TestClient, queued_verifications: list[list[Any]]
) -> None:
    args = create_account(client, queued_verifications)
    reset = create_password_reset_token(
        uuid.UUID(args[0]),
        uuid.UUID(args[1]),
        0,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    assert (
        client.post(
            f"{AUTH}/reset-password", json={"token": reset, "password": "replacement-password"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{AUTH}/login",
            json={"email": "pending@example.com", "password": "replacement-password"},
        ).status_code
        == 403
    )
    assert confirm(client, link(args)).status_code == 200


@pytest.mark.parametrize("endpoint", ["resend-verification", "verify-email"])
def test_verification_endpoints_are_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    setting = (
        "RATE_LIMIT_EMAIL_VERIFICATION_PER_HOUR"
        if endpoint == "resend-verification"
        else "RATE_LIMIT_EMAIL_VERIFICATION_CONFIRM_PER_MINUTE"
    )
    monkeypatch.setattr(get_settings(), setting, 0)
    payload = (
        {"email": "unknown@example.com"}
        if endpoint == "resend-verification"
        else {"token": "not-a-token"}
    )
    response = client.post(f"{AUTH}/{endpoint}", json=payload)
    assert response.status_code == 429 and response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.headers.get("Retry-After")


def test_email_bucket_is_shared_across_request_addresses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "RATE_LIMIT_EMAIL_VERIFICATION_EMAIL_PER_HOUR", 1)
    addresses = iter(["192.0.2.1", "192.0.2.2"])
    monkeypatch.setattr("app.api.rate_limits.client_ip", lambda request: next(addresses))
    email = f"rate-{uuid.uuid4().hex}@example.com"
    assert client.post(f"{AUTH}/resend-verification", json={"email": email}).status_code == 202
    response = client.post(f"{AUTH}/resend-verification", json={"email": email.upper()})
    assert response.status_code == 429
