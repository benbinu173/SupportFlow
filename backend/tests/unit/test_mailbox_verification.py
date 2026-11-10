"""Local checkers keep their caller's transport and confirm only the intended mailbox link."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from scripts.mailbox_verification import finish_registration, verify_local_account

from app.core.security import create_email_verification_token, email_verification_fingerprint

EMAIL = "owner@example.com"


def token(user_id: uuid.UUID) -> str:
    return create_email_verification_token(
        user_id,
        uuid.uuid4(),
        email_verification_fingerprint(EMAIL),
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )


def test_registration_uses_the_existing_https_client_and_verified_workspace() -> None:
    credential = token(uuid.uuid4())
    login_payloads = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            assert str(request.url).startswith("http://127.0.0.1:8026/")
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "ID": "mail",
                            "Subject": "Verify your email — Test workspace",
                            "To": [{"Address": EMAIL}],
                        }
                    ]
                },
            )
        if request.url.path == "/api/v1/message/mail":
            return httpx.Response(
                200, json={"Text": f"https://localhost/verify-email#token={credential}"}
            )
        if request.url.path == "/api/v1/auth/verify-email":
            assert json.loads(request.content) == {"token": credential}
            return httpx.Response(
                200, json={"email": EMAIL, "organization_slug": "correct-workspace"}
            )
        assert request.url.path == "/api/v1/auth/login"
        login_payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"access_token": "test-session"})

    creation = httpx.Response(
        201,
        json={"email": EMAIL},
        request=httpx.Request(
            "POST",
            "https://localhost/api/v1/auth/register",
            json={
                "organization_name": "Test workspace",
                "email": EMAIL,
                "password": "test-password",
            },
        ),
    )
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = finish_registration(creation, client=client)
        assert result.status_code == 200
        assert not client.is_closed
    assert login_payloads == [
        {"email": EMAIL, "password": "test-password", "organization_slug": "correct-workspace"}
    ]


def test_mailbox_selection_skips_another_account_and_an_old_used_link() -> None:
    user_id = uuid.uuid4()
    other, used, current = token(uuid.uuid4()), token(user_id), token(user_id)
    submitted = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "ID": name,
                            "Subject": "Verify your email — Workspace",
                            "To": [{"Address": EMAIL}],
                        }
                        for name in ("other", "used", "current")
                    ]
                },
            )
        if request.url.path.startswith("/api/v1/message/"):
            credential = {"other": other, "used": used, "current": current}[
                request.url.path.rsplit("/", 1)[1]
            ]
            return httpx.Response(
                200, json={"Text": f"http://localhost:5173/verify-email#token={credential}"}
            )
        value = json.loads(request.content)["token"]
        submitted.append(value)
        return httpx.Response(
            400 if value == used else 200, json={"email": EMAIL, "organization_slug": "workspace"}
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = verify_local_account(
            EMAIL,
            api_base="http://localhost:8000/api/v1",
            client=client,
            user_id=str(user_id),
            resend=False,
        )
    assert result["organization_slug"] == "workspace"
    assert submitted == [used, current]
