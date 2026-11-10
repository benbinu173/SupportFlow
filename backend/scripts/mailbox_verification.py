"""Complete generated local test accounts through Mailpit and the real verification API.

No credential is printed or written. Existing caller clients retain their HTTPS policy.
The deployment smoke checker needs the Mailpit compose override when provisioning a tenant.
"""

import json
import re
import time
from contextlib import nullcontext
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt


def verify_local_account(
    email: str,
    *,
    api_base: str,
    client: httpx.Client | None = None,
    mailpit_url: str | None = None,
    workspace: str | None = None,
    user_id: str | None = None,
    resend: bool = True,
) -> dict[str, Any]:
    origin = urlsplit(api_base)
    if origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("Verify the generated account's email, then rerun this checker")
    mailpit_url = mailpit_url or (
        "http://127.0.0.1:8026" if origin.scheme == "https" else "http://127.0.0.1:8025"
    )
    manager = nullcontext(client) if client is not None else httpx.Client(timeout=5)
    with manager as transport:
        if resend:
            requested = transport.post(
                f"{api_base}/auth/resend-verification", json={"email": email}
            )
            if requested.status_code != 202:
                raise RuntimeError(f"Verification request: HTTP {requested.status_code}")
        deadline = time.monotonic() + 30
        seen: set[str] = set()
        while time.monotonic() < deadline:
            try:
                inbox = transport.get(
                    f"{mailpit_url}/api/v1/search", params={"query": f"to:{email}"}
                )
                inbox.raise_for_status()
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    "Start the local Mailpit service to verify generated accounts"
                ) from exc
            for mail in inbox.json().get("messages", []):
                mail_id = str(mail["ID"])
                if mail_id in seen or "Verify your email" not in mail["Subject"]:
                    continue
                if workspace is not None and not mail["Subject"].endswith(
                    f"— {' '.join(workspace.split())}"
                ):
                    continue
                if not any(recipient["Address"] == email for recipient in mail["To"]):
                    continue
                seen.add(mail_id)
                body = transport.get(f"{mailpit_url}/api/v1/message/{mail_id}")
                body.raise_for_status()
                match = re.search(r"https?://[^\s]+/verify-email#token=[^\s]+", body.json()["Text"])
                if match is None:
                    continue
                token = parse_qs(urlsplit(match.group()).fragment).get("token", [""])[0]
                if user_id is not None:
                    # Mail selection only. The API always verifies the signature and tenant.
                    try:
                        subject = jwt.decode(token, options={"verify_signature": False}).get("sub")
                    except jwt.InvalidTokenError:
                        continue
                    if subject != user_id:
                        continue
                verified = transport.post(f"{api_base}/auth/verify-email", json={"token": token})
                if verified.status_code == 400:
                    # An older expired/used email must not defeat the newly requested one.
                    continue
                if verified.status_code != 200:
                    raise RuntimeError(f"Email confirmation: HTTP {verified.status_code}")
                return cast("dict[str, Any]", verified.json())
            time.sleep(0.25)
    raise RuntimeError("Verification email was not delivered; start the notifications worker")


def finish_registration(
    response: httpx.Response, *, client: httpx.Client | None = None, mailpit_url: str | None = None
) -> httpx.Response:
    """Turn the successful creation response into a session for the generated account."""
    if response.status_code != 201:
        raise RuntimeError(f"Account creation: HTTP {response.status_code}")
    payload = json.loads(response.request.content)
    api_base = str(response.request.url).removesuffix("/auth/register")
    verified = verify_local_account(
        response.json()["email"],
        api_base=api_base,
        client=client,
        mailpit_url=mailpit_url,
        workspace=payload["organization_name"],
        resend=False,
    )
    manager = nullcontext(client) if client is not None else httpx.Client(timeout=30)
    with manager as transport:
        logged_in = transport.post(
            f"{api_base}/auth/login",
            json={
                "email": verified["email"],
                "password": payload["password"],
                "organization_slug": verified["organization_slug"],
            },
        )
        if logged_in.status_code != 200:
            raise RuntimeError(f"Verified account sign-in: HTTP {logged_in.status_code}")
        return logged_in


def verify_created_user(response: httpx.Response) -> None:
    user = response.json()
    if user["email_verified_at"] is None:
        verify_local_account(
            user["email"],
            api_base=str(response.request.url).removesuffix("/users"),
            user_id=user["id"],
            resend=False,
        )
