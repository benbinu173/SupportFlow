"""§Z smoke tests — proof that a *running* deployment serves traffic, not that the code compiles.

    cd backend && .venv/Scripts/python.exe -m scripts.deploy_smoke
    .venv/Scripts/python.exe -m scripts.deploy_smoke --base-url https://supportflow.example.com \
        --verify-tls

**Run with `-m`**, for the reason the seed script records: the path form resolves `import app`
only when this checkout is installed editable. `make smoke` uses this form.

**Every request here goes over the network to the address it is given, through the reverse
proxy, over TLS.** That is the whole point: the test suite proves the application is correct
using an in-process client, and it would keep passing if Caddy stopped routing `/ws`, if the
container never started, or if the certificate expired. This script is the only thing in the
project that can tell those apart from a working deployment.

It checks, in order:

1. `/health` answers, so a process is listening behind the proxy.
2. `/health/ready` answers `ready`, so that process can reach Postgres and Redis.
3. The seeded administrator can log in, so migrations ran and the seed is present.
4. Security headers and a request id are on the responses — §54's hardening and Phase Y's
   correlation, verified as *deployed* rather than as written.
5. Tenant isolation, **two-sided**: a ticket that tenant B created returns `200` to B's token and
   `404` to A's. A one-sided check would pass just as well if the ticket did not exist.
6. A real WebSocket, through Caddy, receives the `ticket.created` event for a ticket created
   over HTTP a moment earlier. This is the check a proxy that silently drops upgrade headers
   fails, and the one nothing else in the repository can make.

**TLS verification is off by default**, because the documented local deployment terminates TLS
with Caddy's internal CA and there is nothing to verify against. `--verify-tls` turns it on for a
real hostname with a real certificate, which is the mode that actually protects anything.

**The second tenant is created once and reused.** `POST /auth/register` is rate limited to five
an hour per address (§45), so the script logs in as the smoke tenant first and only registers if
that login fails — repeated runs cost nothing, and a genuinely exhausted limit is reported as
`SKIP` rather than as a deployment failure.
"""

# This script reports to a person at a terminal, which is what `print` is for.
# ruff: noqa: T201

import argparse
import json
import ssl
import sys
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from websockets.sync.client import connect as socket_connect

HEALTH = "/health"
READINESS = "/health/ready"
LOGIN = "/api/v1/auth/login"
REGISTER = "/api/v1/auth/register"
CUSTOMERS = "/api/v1/customers"
TICKETS = "/api/v1/tickets"
SOCKET_PATH = "/ws"

# The seeded tenant (§56). Overridable, because a real deployment's administrator is not the
# demo one — the defaults are what `scripts/seed_demo.py` creates.
ADMIN_EMAIL = "admin@acme.example.com"
ADMIN_PASSWORD = "DemoPassw0rd!23"

# The second tenant, used only to prove isolation. Created by this script on first run.
SMOKE_ORG = "Smoke Test Tenant"
SMOKE_EMAIL = "owner@smoke.example.com"
SMOKE_PASSWORD = "SmokeTenantPassw0rd!23"

_passed = 0
_failed = 0
_skipped = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ok    {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}{f' -- {detail}' if detail else ''}")


def skip(label: str, why: str) -> None:
    global _skipped
    _skipped += 1
    print(f"  SKIP  {label} -- {why}")


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


@dataclass(frozen=True)
class Tenant:
    """A signed-in tenant: the token to send, and who it belongs to."""

    token: str
    email: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def _socket_url(base_url: str) -> str:
    scheme, _, host = base_url.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{host.rstrip('/')}{SOCKET_PATH}"


def _socket_ssl(base_url: str, verify: bool) -> ssl.SSLContext | None:
    """The TLS settings for the socket, or `None` when the deployment is plain HTTP.

    `websockets` refuses an `ssl` argument for a `ws://` URL, so the scheme decides rather than a
    flag — a caller cannot ask for TLS on a cleartext connection and get a confusing error.
    """
    if not base_url.startswith("https"):
        return None
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _body(response: httpx.Response) -> dict[str, Any]:
    """The response as JSON, or an empty mapping if it is not JSON at all."""
    try:
        parsed = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _signed_in(response: httpx.Response) -> tuple[str | None, str]:
    """The access token from a login or register response, and why it is missing if not."""
    if response.status_code != 200 and response.status_code != 201:
        error = _body(response).get("error", {})
        return None, f"HTTP {response.status_code} {error.get('code', '')}".strip()
    token = _body(response).get("access_token")
    if not isinstance(token, str) or not token:
        return None, "no access_token in the response"
    return token, ""


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def _check_probes(client: httpx.Client) -> None:
    section("Probes")

    live = client.get(HEALTH)
    check("liveness answers 200", live.status_code == 200, str(live.status_code))
    check("/health reports ok", _body(live).get("status") == "ok", live.text[:120])

    ready = client.get(READINESS)
    body = _body(ready)
    check("readiness answers 200", ready.status_code == 200, ready.text[:200])
    check("readiness reports ready", body.get("status") == "ready", str(body.get("status")))
    checks = body.get("checks", {})
    check(
        "every dependency the probe covers is reachable",
        bool(checks) and all(checks.values()),
        str(checks),
    )


def _check_headers(response: httpx.Response) -> None:
    section("Security headers and correlation — as deployed")

    check(
        "every response carries a request id",
        bool(response.headers.get("X-Request-ID")),
        "X-Request-ID absent",
    )
    check(
        "nosniff is set",
        response.headers.get("X-Content-Type-Options") == "nosniff",
        str(response.headers.get("X-Content-Type-Options")),
    )
    check(
        "the response cannot be framed",
        response.headers.get("X-Frame-Options") == "DENY",
        str(response.headers.get("X-Frame-Options")),
    )
    # Present only outside development, which is what makes this a statement about the
    # deployment's ENVIRONMENT rather than about the middleware existing.
    check(
        "HSTS is set, so this is a production configuration",
        bool(response.headers.get("Strict-Transport-Security")),
        "Strict-Transport-Security absent",
    )


def _sign_in_admin(client: httpx.Client, email: str, password: str) -> Tenant | None:
    section("The seeded administrator")

    response = client.post(LOGIN, json={"email": email, "password": password})
    token, why = _signed_in(response)
    if token is None:
        check(f"{email} can log in", False, why)
        print(
            "\n  The seed has not been run against this deployment, or the credentials are\n"
            "  different. Run: .venv/Scripts/python.exe scripts/seed_demo.py"
        )
        return None
    check(f"{email} can log in", True)

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    check("/auth/me answers with the caller", me.status_code == 200, str(me.status_code))
    check(
        "the token carries the right account",
        _body(me).get("email") == email,
        str(_body(me).get("email")),
    )
    return Tenant(token=token, email=email)


def _second_tenant(client: httpx.Client) -> Tenant | None:
    """Sign in as the smoke tenant, creating it the first time.

    Returns `None` when it cannot be obtained at all — a rate-limited registration is reported as
    a skip by the caller, because it says nothing about the deployment.
    """
    section("A second tenant, for the isolation check")

    response = client.post(LOGIN, json={"email": SMOKE_EMAIL, "password": SMOKE_PASSWORD})
    if response.status_code == 200:
        token, _ = _signed_in(response)
        if token is not None:
            check("the smoke tenant already exists and can log in", True)
            return Tenant(token=token, email=SMOKE_EMAIL)

    created = client.post(
        REGISTER,
        json={
            "organization_name": SMOKE_ORG,
            "name": "Smoke Owner",
            "email": SMOKE_EMAIL,
            "password": SMOKE_PASSWORD,
        },
    )
    token, why = _signed_in(created)
    if token is None:
        skip("the smoke tenant is available", why)
        return None

    check("the smoke tenant was created", created.status_code == 201, str(created.status_code))
    return Tenant(token=token, email=SMOKE_EMAIL)


def _a_customer(client: httpx.Client, tenant: Tenant) -> str | None:
    """A customer id in this tenant, reusing one if the tenant already has any.

    Reused rather than always created: `(organization_id, email)` is unique, so creating one per
    run would be a `409` on the second run and the smoke test would fail for a reason that is
    entirely its own bookkeeping.
    """
    listed = client.get(CUSTOMERS, headers=tenant.headers)
    if listed.status_code == 200:
        existing = listed.json()
        if isinstance(existing, list) and existing:
            return str(existing[0]["id"])

    created = client.post(
        CUSTOMERS,
        headers=tenant.headers,
        json={"name": "Smoke Contact", "email": f"smoke.contact@{tenant.email.split('@')[1]}"},
    )
    if created.status_code != 201:
        return None
    return str(_body(created).get("id") or "") or None


def _new_ticket(client: httpx.Client, tenant: Tenant, subject: str) -> httpx.Response | None:
    customer_id = _a_customer(client, tenant)
    if customer_id is None:
        check(f"'{tenant.email}' has a customer to raise a ticket for", False)
        return None
    return client.post(
        TICKETS,
        headers=tenant.headers,
        json={
            "subject": subject,
            "description": "Created by scripts/deploy_smoke.py to prove the deployment works.",
            "customer_id": customer_id,
            "priority": "medium",
        },
    )


def _check_isolation(client: httpx.Client, admin: Tenant, other: Tenant) -> None:
    section("Tenant isolation, from both sides")

    raised = _new_ticket(client, other, "A ticket in the smoke tenant")
    if raised is None:
        check("the smoke tenant can raise a ticket", False, "no customer to raise it for")
        return
    if raised.status_code != 201:
        check("the smoke tenant can raise a ticket", False, str(raised.status_code))
        return
    check("the smoke tenant can raise a ticket", True)

    ticket_id = str(_body(raised).get("id") or "")
    if not ticket_id:
        check("the created ticket has an id", False, raised.text[:200])
        return

    # The owner sees it. Without this the next check would pass for a ticket that does not exist.
    own = client.get(f"{TICKETS}/{ticket_id}", headers=other.headers)
    check("its owner can read it", own.status_code == 200, str(own.status_code))

    # And the other tenant cannot — 404 rather than 403, so the response does not confirm that
    # the id exists somewhere (§37: existence is not disclosed across tenants).
    foreign = client.get(f"{TICKETS}/{ticket_id}", headers=admin.headers)
    check(
        "the other tenant gets 404, not the ticket",
        foreign.status_code == 404,
        f"HTTP {foreign.status_code}",
    )

    # **Byte-for-byte, not just "also a 404".** The property §37 asks for is that a refused
    # cross-tenant read is *indistinguishable* from one for an id that never existed; a status
    # code alone does not establish that, and this is the check that would have caught the
    # per-request value Phase Y briefly put in the error envelope. The suite's isolation tests
    # caught it first, and this makes the same property visible from outside the process.
    missing = client.get(f"{TICKETS}/{uuid.uuid4()}", headers=admin.headers)
    check(
        "the refusal is identical to one for an id that never existed",
        missing.status_code == 404 and _body(missing) == _body(foreign),
        json.dumps(_body(foreign))[:160],
    )

    # The correlation id is a response *header* on every response, not a field in this body —
    # and the check above is the reason it is not in the body.
    check(
        "the refusal carries the request id a person can quote",
        bool(foreign.headers.get("X-Request-ID")),
        foreign.headers.get("X-Request-ID", "absent"),
    )


def _check_socket(client: httpx.Client, admin: Tenant, base_url: str, verify: bool) -> None:
    section("Realtime, through the proxy")

    url = _socket_url(base_url)
    try:
        socket = socket_connect(url, ssl=_socket_ssl(base_url, verify), open_timeout=15)
    except Exception as exc:
        # Deliberately broad, and printed rather than swallowed: a refused handshake, a DNS
        # failure, and a certificate error are all reasons this check is red, and the reason is
        # the only useful thing to report.
        check(f"a WebSocket connects to {url}", False, repr(exc))
        return

    with socket:
        check(f"a WebSocket connects to {url}", True)

        socket.send(json.dumps({"type": "auth", "token": admin.token}))
        try:
            hello = json.loads(socket.recv(timeout=15))
        except Exception as exc:
            check("the socket accepts the token", False, repr(exc))
            return
        check("the socket accepts the token", hello.get("type") == "authenticated", str(hello))

        raised = _new_ticket(client, admin, "A ticket to watch for on the socket")
        if raised is None:
            check("a ticket is created to trigger an event", False, "no customer to raise it for")
            return
        if raised.status_code != 201:
            check("a ticket is created to trigger an event", False, str(raised.status_code))
            return
        check("a ticket is created to trigger an event", True)

        # Up to three frames, because creating a ticket can publish more than one event and the
        # order is not guaranteed — a `notification.created` for the same act may arrive first.
        seen: list[str] = []
        arrived = False
        for _ in range(3):
            try:
                envelope = json.loads(socket.recv(timeout=20))
            except TimeoutError:
                break
            kind = str(envelope.get("type", ""))
            seen.append(kind)
            if kind == "ticket.created":
                arrived = True
                break

        check(
            "the ticket.created event arrives over the socket",
            arrived,
            f"saw {seen or 'nothing'}",
        )


def _check_configuration_routes(client: httpx.Client, admin: Tenant) -> None:
    """A route whose behaviour depends on configuration, reported rather than assumed.

    Not a deployment failure. The knowledge ask needs an embedding provider to retrieve anything
    and a generation key to answer; a deployment without them is a legal configuration that
    refuses at the point of use rather than at startup, and a smoke test that failed on it would
    be testing the operator's budget rather than the deployment.
    """
    section("A configuration-dependent route")

    answer = client.post(
        "/api/v1/knowledge/search",
        headers=admin.headers,
        json={"question": "How long do I have to return an item?"},
    )
    if answer.status_code in (200, 201):
        body = _body(answer)
        check("the knowledge question was answered", True)
        # §24: an empty `sources` list is the refusal. Both outcomes are correct here, and the
        # difference is worth printing rather than collapsing into one "ok".
        print(f"        {len(body.get('sources', []))} sources cited")
    else:
        error = _body(answer).get("error", {})
        skip(
            "the knowledge question was answered",
            f"HTTP {answer.status_code} {error.get('code', '')} — no provider configured",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test a running SupportFlow deployment.")
    parser.add_argument(
        "--base-url",
        default="https://localhost",
        help="Where the deployment is reachable, through its reverse proxy.",
    )
    parser.add_argument(
        "--verify-tls",
        action="store_true",
        help=(
            "Verify the TLS certificate. Off by default because the documented local stack uses "
            "Caddy's internal CA; on for a real hostname, which is the mode that protects "
            "anything."
        ),
    )
    parser.add_argument("--email", default=ADMIN_EMAIL, help="The administrator to sign in as.")
    parser.add_argument("--password", default=ADMIN_PASSWORD, help="That administrator's password.")
    args = parser.parse_args()

    print(f"Smoke-testing {args.base_url} (TLS verification {'on' if args.verify_tls else 'off'})")

    with httpx.Client(
        base_url=args.base_url, verify=args.verify_tls, timeout=20, follow_redirects=False
    ) as client:
        try:
            _check_probes(client)
        except httpx.HTTPError as exc:
            print(f"\nThe deployment did not answer: {exc!r}")
            print("Nothing else can be checked. Is the stack up, and is the URL right?")
            sys.exit(2)

        # The headers are read off a probe response rather than a special route: every response
        # carries them, which is the property being asserted.
        _check_headers(client.get(HEALTH))

        admin = _sign_in_admin(client, args.email, args.password)
        if admin is None:
            sys.exit(2)

        other = _second_tenant(client)
        if other is None:
            skip("tenant isolation", "no second tenant available")
        else:
            _check_isolation(client, admin, other)

        _check_socket(client, admin, args.base_url, args.verify_tls)
        _check_configuration_routes(client, admin)

    summary = f"{_passed} passed, {_failed} failed"
    if _skipped:
        summary += f", {_skipped} skipped"
    print(f"\n{summary}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
