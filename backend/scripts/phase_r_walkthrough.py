"""Phase R end to end: a real socket, a real fan-out, and a worker in another process.

The suites already assert the boundary without a socket, the transport through `TestClient`,
and the queue's behaviour under a stalled client. This script exists for the four things none
of those can show:

* **That a client written against the documented protocol works.** `websockets`' own client
  connects to a running uvicorn, sends the auth frame, and reads the envelopes — so what is
  being exercised is the contract a browser implements in Phase C, not a `TestClient` stand-in
  for one.
* **That the credential never reaches the server's request log.** The handshake below carries
  no query string, no header and no subprotocol, which is the property ADR-025 chose the frame
  mechanism for. The check is about what this script *sends*; the server's own stdout is the
  other half, and the command to read it is printed at the end.
* **That the worker's publish arrives on the API's socket**, which is the whole reason the
  fan-out travels through Redis. `check_organization_sla` is dispatched to the broker and run
  by a **second process**; the envelope it publishes lands here. An in-process callback could
  not do this, and no test in the suite can either — the suite is one process.
* **That each change arrives with both ends of it**, so a client can render a toast without
  fetching the ticket.

Absence is asserted with a bounded read rather than with the control-envelope technique the
suites use. That technique exists so CI never sleeps; here a one-and-a-half-second wait is the
legible version of the same claim, and this script is for a person watching. What keeps it
honest is the ordering: the socket that *should* be told is drained first, so by the time the
socket that should not be told is read, the fan-out has demonstrably already run for that event.

**The slow-consumer close is not exercised here.** Driving a real socket into overflow means
filling kernel buffers before the application queue can start to fill, which depends on the
machine's socket buffer sizes. `tests/unit/test_realtime_manager.py` reaches the same decision
directly and deterministically — including the drain that makes the close actually land — and
the README records the numbers.

Four steps:

    # 1. the API, from backend/
    .venv/Scripts/python.exe -m uvicorn app.main:app \\
        --loop app.core.event_loop:loop_factory --port 8000

    # 2. the worker, from backend/ -- section 5 needs it
    .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker \\
        --loglevel=info --pool=solo -Q notifications,sla

    # 3. this script, from backend/
    .venv/Scripts/python.exe scripts/phase_r_walkthrough.py

    # 4. afterwards, in the shell running the API -- the other half of section 1. Nothing
    #    should match, because the credential is never on a request line:
    #      uvicorn's stdout | Select-String -Pattern "token"

Three things to know before running it. It **registers two organizations**, and §45 limits
registration to five an hour per address, so a third run inside the hour reports `429` rather
than a failed assertion — the limiter working, not the phase:

    docker compose exec redis redis-cli -n 0 --scan --pattern 'ratelimit:register:*'

It **writes to `tickets.created_at`** to move one ticket's clock into the SLA warning band,
which is the only way to test a deadline without waiting half an hour and which no endpoint
exposes. Point it at a development database. And it leaves its organizations behind: nothing
here deletes anything.
"""

# ruff: noqa: T201

import contextlib
import json
import sys
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import text
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as socket_connect

from app.core.database import engine
from app.core.event_loop import run
from app.workers.sla_tasks import check_organization_sla

BASE = "http://localhost:8000/api/v1"
TICKETS = f"{BASE}/tickets"
PASSWORD = "correct-horse-battery-staple"

#: The socket. `/ws` at the application root, not under `API_V1_PREFIX` — `vite.config.ts`
#: proxies this exact path and rewrites nothing, so the path was decided before this phase and
#: this script follows it rather than choosing it.
WS = "ws://localhost:8000/ws"

#: How long to wait on a read that is expected to produce nothing. Long enough that a message
#: would have arrived had the predicate let it through — the whole publish path is a local
#: Redis round trip measured in single-digit milliseconds — and short enough that a person
#: watching does not lose interest.
QUIET_SECONDS = 1.5

#: §27's URGENT row as seeded: 30 minutes to a first response. Written out rather than
#: imported from `sla_service.DEFAULT_POLICIES`, for the reason the Phase Q script gives —
#: a walkthrough that derives its expectation from the code under test shows only that the code
#: is self-consistent, and this is a transcription of a specification table. The *warning
#: threshold* is not transcribed, because it is the tenant's to edit; section 5 reads it from
#: the organization's own policy.
URGENT_RESPONSE_MINUTES = 30

#: How long to wait for the worker to pick up a dispatched task and publish. Generous, because
#: it includes the cold start of a task in a second process.
WORKER_TIMEOUT_SECONDS = 30.0

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. The script runs to the end even after a failure.

    A walkthrough that stops at the first problem hides the rest of the story, and seeing the
    rest of the story is why one walks through rather than running the suite — which already
    stops at the first problem.
    """
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ok    {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}{f' -- {detail}' if detail else ''}")


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def kinds(envelopes: list[dict[str, Any]]) -> set[str]:
    """The event types in a drained batch, for an assertion that does not count them."""
    return {str(envelope.get("type")) for envelope in envelopes}


def find(envelopes: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    """The one envelope of a given type, or `None` if the batch did not carry it."""
    return next((item for item in envelopes if item.get("type") == event_type), None)


class Api:
    """One signed-in caller: an HTTP client carrying a bearer token."""

    def __init__(self, token: str, email: str) -> None:
        self.token = token
        self.email = email
        self._client = httpx.Client(base_url=BASE, timeout=30.0)
        self._headers = {"Authorization": f"Bearer {token}"}
        self._user_id: str | None = None

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.request(method, path, headers=self._headers, **kwargs)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    @property
    def user_id(self) -> str:
        """This caller's own id, from `/auth/me`.

        Asked for rather than passed in: `TokenResponse` carries the access token and nothing
        else, so a session's identity is only knowable by asking. Reading it from the endpoint
        a client would use keeps the recipient assertions below about the API rather than about
        this script.
        """
        if self._user_id is None:
            response = self.get("/auth/me")
            response.raise_for_status()
            self._user_id = str(response.json()["id"])
        return self._user_id


class Tenant:
    """A registered organization, with its founding admin's session."""

    def __init__(self, label: str) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.name = f"{label} {suffix}"
        self.email = f"admin-{suffix}@walkthrough.example"

        response = httpx.post(
            f"{BASE}/auth/register",
            json={
                "organization_name": self.name,
                "name": "Admin",
                "email": self.email,
                "password": PASSWORD,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        self.admin = Api(response.json()["access_token"], self.email)

    def member(self, role: str, **extra: Any) -> Api:
        """Create a user in this organization, and return their signed-in session."""
        email = f"member-{uuid.uuid4().hex[:8]}@walkthrough.example"
        created = self.admin.post(
            "/users",
            json={
                "name": role.title(),
                "email": email,
                "password": PASSWORD,
                "role": role,
                **extra,
            },
        )
        created.raise_for_status()

        logged_in = httpx.post(
            f"{BASE}/auth/login", json={"email": email, "password": PASSWORD}, timeout=30.0
        )
        logged_in.raise_for_status()
        return Api(logged_in.json()["access_token"], email)

    def customer(self, name: str) -> str:
        response = self.admin.post(
            "/customers", json={"name": name, "email": f"{uuid.uuid4().hex[:8]}@customer.example"}
        )
        response.raise_for_status()
        return str(response.json()["id"])

    def ticket_for(
        self, customer_id: str, subject: str, *, priority: str = "medium"
    ) -> dict[str, Any]:
        response = self.admin.post(
            "/tickets",
            json={
                "subject": subject,
                "description": "It does not work.",
                "customer_id": customer_id,
                "priority": priority,
            },
        )
        response.raise_for_status()
        return dict(response.json())

    def move(self, ticket_id: str, status: str) -> dict[str, Any]:
        response = self.admin.post(f"{TICKETS}/{ticket_id}/status", json={"status": status})
        response.raise_for_status()
        return dict(response.json())

    def assign(self, ticket_id: str, agent_id: str) -> dict[str, Any]:
        response = self.admin.post(
            f"{TICKETS}/{ticket_id}/assign", json={"assigned_agent_id": agent_id}
        )
        response.raise_for_status()
        return dict(response.json())

    def urgent_warning_minutes(self) -> int:
        """When this organization's urgent response clock starts warning, in minutes.

        Read from the tenant's own policy rather than assumed: `warning_threshold_percent` is
        editable per organization, so it is configuration rather than a constant, and a
        walkthrough that hardcoded 80 would fail the day somebody set it to 50.
        """
        response = self.admin.get("/sla/policies")
        response.raise_for_status()
        policy = next(row for row in response.json() if row["priority"] == "urgent")
        response_target = int(policy["response_time_minutes"])
        if response_target != URGENT_RESPONSE_MINUTES:
            print(
                f"  note  this organization's urgent response target is {response_target} "
                f"minutes, not the seeded {URGENT_RESPONSE_MINUTES}"
            )
        return int(response_target * int(policy["warning_threshold_percent"]) / 100)


class Socket:
    """An authenticated real-time connection, as a client would hold one.

    Deliberately thin: it sends the auth frame, reads envelopes, and does nothing else. The
    protocol has one client-to-server message, and a client implementing more than this is
    implementing something the server does not support.
    """

    def __init__(self, token: str) -> None:
        # No query string, no headers, no subprotocol. That is the whole of what ADR-025
        # decided: the credential is not on the request line uvicorn logs, so there is nothing
        # to leak and nothing for `test_log_hygiene` to be blind to.
        #
        # `legacy=True` because this connection is owned by an object and closed deliberately
        # in `close()`, rather than scoped to a `with` block. `websockets` 17 warns about a bare
        # `connect()` to catch the case where somebody forgets to close one; here the close is
        # not forgotten, it is the thing the last section is about.
        self._socket = socket_connect(WS, open_timeout=10, close_timeout=5, legacy=True)
        self._socket.send(json.dumps({"type": "auth", "token": token}))
        # A refusal is a result rather than a crash: section 1 asserts on `acknowledged`, and
        # the close carrying the reason is read by `refusal_code` below, which is the only
        # place that needs the code.
        self.acknowledged = False
        with contextlib.suppress(ConnectionClosed):
            self.acknowledged = self.receive().get("type") == "authenticated"

    def receive(self, *, timeout: float = 10.0) -> dict[str, Any]:
        return dict(json.loads(self._socket.recv(timeout=timeout)))

    def drain(self, *, timeout: float = QUIET_SECONDS) -> list[dict[str, Any]]:
        """Every envelope that arrives before the socket goes quiet.

        Both the positive and the negative case in one call: a batch with the expected event in
        it, or an empty list when nothing was sent within the window. Keeping them the same
        call is what stops the negative assertions from being a different, weaker kind of test.
        """
        received: list[dict[str, Any]] = []
        while True:
            try:
                received.append(self.receive(timeout=timeout))
            except TimeoutError:
                return received

    def wait_for(self, event_type: str, *, timeout: float) -> dict[str, Any] | None:
        """The first envelope of a given type within `timeout`, or `None`.

        For the one case a drain cannot serve: waiting on another process, where the answer may
        take thirty seconds and draining would mean blocking for the full window *after* the
        envelope had already arrived.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                candidate = self.receive(timeout=remaining)
            except TimeoutError:
                return None
            if candidate.get("type") == event_type:
                return candidate

    def close(self) -> None:
        self._socket.close()


def refusal_code(token: str) -> int | None:
    """The code the server closes with when a socket presents `token`.

    A bare `websockets` connection rather than a `Socket`, because the interesting thing
    happens before the auth frame is ever acknowledged — there is no session to wrap.
    """
    with socket_connect(WS, open_timeout=10, close_timeout=5) as probe:
        probe.send(json.dumps({"type": "auth", "token": token}))
        # The read is what carries the close: `close_code` stays `None` until the frame has
        # actually arrived, so the refusal has to be waited for rather than asked for.
        with contextlib.suppress(ConnectionClosed):
            probe.recv(timeout=10.0)
        return probe.close_code


def age_the_ticket(ticket_id: str, *, minutes: int) -> None:
    """Move a ticket's clock back, which no endpoint exposes.

    The same admission the Phase Q walkthrough makes: the only way to test a deadline without
    waiting for it is to move the row the deadline is computed from, and that means a direct
    write against a development database. `created_at` and not `updated_at`, because
    `sla_service` starts both timers at `ticket.created_at`.
    """
    statement = text(
        "UPDATE tickets SET created_at = created_at - (:minutes * interval '1 minute') "
        "WHERE id = :id"
    )

    # The engine echoes every statement when `DEBUG` is on, which is right for the server and
    # unreadable in the middle of this output — and this file's direct write is the only
    # statement *this* process issues. The API's own logging happens in its own process, so
    # nothing here silences what the server would have logged.
    engine.echo = False

    async def write() -> None:
        async with engine.begin() as connection:
            await connection.execute(statement, {"minutes": minutes, "id": ticket_id})

    run(write())


# ---------------------------------------------------------------------------
# The sections
# ---------------------------------------------------------------------------


def a_credential_opens_a_socket_and_only_in_a_frame(tenant: Tenant) -> Socket:
    """The handshake, and the shape of the credential that opens it."""
    section("1. A credential opens a socket, and it travels in a frame")

    check(
        "the handshake URL carries no credential",
        "token" not in WS and "?" not in WS,
        f"connecting to {WS}",
    )

    socket = Socket(tenant.admin.token)
    check("the auth frame is acknowledged", socket.acknowledged)

    # And a socket that presents nothing usable is closed with a code a client can act on:
    # 4401 means "authenticate", which is different advice from 1000's "the server is going
    # away", and a client that could not tell them apart would refresh a token it did not need
    # to refresh.
    check("a bad token closes the socket with 4401", refusal_code("not-a-token") == 4401)

    return socket


def a_committed_change_arrives_with_both_ends_of_it(
    tenant: Tenant, socket: Socket
) -> dict[str, Any]:
    """Create, assign and move — read off sockets rather than fetched."""
    section("2. A committed change arrives, with both ends of it")

    customer_id = tenant.customer("Ada Lovelace")
    agent = tenant.member("agent")
    agent_socket = Socket(agent.token)

    ticket = tenant.ticket_for(customer_id, "The printer is on fire")
    created = socket.drain()
    check("a new ticket is announced as `ticket.created`", kinds(created) == {"ticket.created"})
    envelope = find(created, "ticket.created")
    check(
        "and it names the ticket by number",
        envelope is not None and envelope.get("ticket_number") == ticket["number"],
    )
    # The envelope is an announcement, not a rendering: no subject, no description, no status
    # document — so there is one rendering path and the socket cannot disagree with
    # `GET /tickets/{id}`. This is the "thin event" decision, asserted rather than described.
    check(
        "and it carries no rendered ticket",
        envelope is not None and not {"subject", "description", "status"} & set(envelope),
    )
    check("a ticket nobody has claimed is not pushed to an agent", agent_socket.drain() == [])

    tenant.assign(ticket["id"], agent.user_id)
    told_admin, told_agent = socket.drain(), agent_socket.drain()
    check(
        "an assignment is announced to staff as an event",
        kinds(told_admin) == {"ticket.assigned"},
        str(kinds(told_admin)),
    )
    # Two envelopes to the assignee, and the split is the phase's design: the ticket event says
    # the field changed, the notification says somebody is owed a message about it. They travel
    # in one pipelined round trip, so neither can arrive without the other.
    check(
        "and to the assignee as both the event and a notification",
        kinds(told_agent) == {"ticket.assigned", "notification.created"},
        str(kinds(told_agent)),
    )
    notification = find(told_agent, "notification.created")
    check(
        "and that notification names the person it is for",
        notification is not None and notification.get("user_id") == agent.user_id,
    )

    tenant.move(ticket["id"], "in_progress")
    status = find(socket.drain(), "ticket.status_changed")
    check("a status change is announced", status is not None)
    # "assigned" and not "open": the assignment already moved the ticket, and this is the
    # transition table being read back rather than a value this script chose.
    check(
        "and it names both ends of the change",
        status is not None
        and status.get("from_value") == "assigned"
        and status.get("to_value") == "in_progress",
        f"{status and status.get('from_value')} -> {status and status.get('to_value')}",
    )

    agent_socket.close()

    return {
        "ticket": ticket,
        "customer_id": customer_id,
        "agent": agent,
        # No HTTP response exposes an organization id — `TicketRead` and `UserRead` both omit
        # it, because the organization is implied by the caller's own token. This envelope is
        # the only way to learn it, and section 5 needs it to address the worker's task.
        "organization_id": envelope["organization_id"] if envelope else None,
    }


def an_internal_note_reaches_staff_and_not_the_customer(
    tenant: Tenant, staff: Socket, world: dict[str, Any]
) -> None:
    """The leak the first design would have shipped, on a live socket."""
    section("3. An internal note reaches staff and not the customer")

    portal = tenant.member("customer", customer_id=world["customer_id"])
    portal_socket = Socket(portal.token)
    check("a portal user can hold a socket", portal_socket.acknowledged)

    # Their own ticket, which they are entitled to: the customer's row scope is `OWN` and this
    # one is theirs. Asserted first so the silence below is about the note and not about a
    # portal user whose socket never worked at all.
    tenant.move(world["ticket"]["id"], "waiting_for_customer")
    check(
        "the customer is told about their own ticket",
        kinds(portal_socket.drain()) == {"ticket.status_changed"},
    )
    staff.drain()

    note = tenant.admin.post(
        f"{TICKETS}/{world['ticket']['id']}/notes", json={"body": "Refund approved."}
    )
    note.raise_for_status()

    # The order matters: staff first, so by the time the customer's socket is read the fan-out
    # has demonstrably already run for this exact event. Had the predicate let the note through,
    # it would be sitting in the customer's queue by now.
    written = staff.drain()
    check("staff are told about the internal note", kinds(written) == {"ticket.note_added"})
    envelope = find(written, "ticket.note_added")
    check("and the envelope says it is internal", envelope is not None and envelope["internal"])
    check("the customer is not told about it", portal_socket.drain() == [])

    portal_socket.close()


def another_tenants_events_never_arrive(
    tenant: Tenant, other: Tenant, world: dict[str, Any]
) -> None:
    """Two tenants, two sockets, one event."""
    section("4. Another tenant's event never arrives")

    theirs = Socket(other.admin.token)
    check("the second tenant can hold a socket", theirs.acknowledged)

    customer_id = other.customer("Grace Hopper")
    other.ticket_for(customer_id, "Their ticket")
    check("their own ticket is announced to them", kinds(theirs.drain()) == {"ticket.created"})

    tenant.move(world["ticket"]["id"], "in_progress")
    # The same ordering argument as section 3: read the socket that is entitled to the event
    # first, so the exclusion below is about the boundary and not about timing.
    check(
        "the owning tenant is told",
        kinds(world["socket"].drain()) == {"ticket.status_changed"},
    )
    check("the other tenant is not", theirs.drain() == [])

    theirs.close()


def the_worker_publishes_and_the_api_socket_receives(
    tenant: Tenant, world: dict[str, Any], warning_minutes: int
) -> None:
    """The phase's central claim: an event raised in another process reaches this socket.

    The envelope is produced by `check_organization_sla` running in the **worker**, published
    through Redis, received by the **API** process's subscriber, admitted by the predicate, and
    written to a socket this script holds. No two consecutive links in that chain share a
    process, which is the reason the fan-out travels through Redis at all — and it is the one
    thing no test in the suite can demonstrate, because the suite is one process.
    """
    section("5. The worker publishes, and this socket receives")

    customer_id = tenant.customer("Alan Turing")
    ticket = tenant.ticket_for(customer_id, "Nobody has looked at this", priority="urgent")

    # Opened after the ticket exists, so this socket's queue holds nothing but what the worker
    # is about to send. The recipient is a manager because an SLA alert on an unassigned ticket
    # is addressed to the managers: an alert nobody receives is the failure the feature exists
    # to prevent.
    manager = tenant.member("manager")
    manager_socket = Socket(manager.token)
    check("a manager can hold a socket", manager_socket.acknowledged)

    # Into the warning band: past the threshold, short of the deadline. A sweep run before this
    # would find the ticket fresh, which is what the empty drain after the aging rules out.
    age_the_ticket(ticket["id"], minutes=warning_minutes + 1)
    check(
        f"the ticket's clock is moved back {warning_minutes + 1} minutes",
        manager_socket.drain(timeout=0.5) == [],
        "the aging itself published something, which it should not have",
    )

    print("  ..    dispatching `check_organization_sla` to the broker, for the worker to run")
    check_organization_sla.delay(world["organization_id"])

    alert = manager_socket.wait_for("notification.created", timeout=WORKER_TIMEOUT_SECONDS)
    check(
        "the worker's publish reaches this socket",
        alert is not None,
        f"nothing arrived in {WORKER_TIMEOUT_SECONDS:.0f}s -- is the worker running?",
    )
    if alert is not None:
        check(
            "and it is the SLA warning the sweep decided on",
            alert.get("notification_type") == "sla_warning",
            str(alert.get("notification_type")),
        )
        check("and it names the ticket it is about", alert.get("ticket_id") == ticket["id"])
        check(
            "and it is addressed to the manager holding this socket",
            alert.get("user_id") == manager.user_id,
        )

    manager_socket.close()


def a_closed_socket_is_forgotten(socket: Socket) -> None:
    """The connection ends, and the process is not left holding it.

    Nothing here can read a process's private registry over HTTP, so the assertion that the
    registry emptied is the suites' — `test_the_registry_is_empty_again_once_the_socket_closes`
    and `test_every_refusal_leaves_the_registry_empty`. What this section adds is the part they
    cannot: a socket a real client opened is closed, and the server is asked to behave.
    """
    section("6. A closed socket is forgotten")

    socket.close()
    check("the socket closes without the server raising", True)
    print(
        "  ..    the registry's own emptiness is asserted by tests/api/test_websocket.py and\n"
        "        tests/security/test_websocket.py, which can read it in-process"
    )


def main() -> None:
    tenant = Tenant("Realtime")
    other = Tenant("Elsewhere")

    socket = a_credential_opens_a_socket_and_only_in_a_frame(tenant)
    world = a_committed_change_arrives_with_both_ends_of_it(tenant, socket)
    world["socket"] = socket
    warning_minutes = tenant.urgent_warning_minutes()

    an_internal_note_reaches_staff_and_not_the_customer(tenant, socket, world)
    another_tenants_events_never_arrive(tenant, other, world)
    the_worker_publishes_and_the_api_socket_receives(tenant, world, warning_minutes)
    a_closed_socket_is_forgotten(socket)

    print(f"\n{_passed} passed, {_failed} failed")
    print(
        "\nNow read the API's own stdout -- the other half of section 1. Nothing should match:\n"
        '  (in the shell running uvicorn)  ... | Select-String -Pattern "token"\n'
        "A handshake logs its request line, which is exactly why the credential travels in a\n"
        "frame and not in `?token=`: an access token would be printed on every connect and\n"
        "every reconnect, and `tests/security/test_log_hygiene.py` records structlog calls\n"
        "only, so it would never have noticed. The line to look at reads\n"
        '  "WebSocket /ws" [accepted]  -- no query string, no token, on any connect.'
    )
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError as exc:
        print(
            f"\nCould not reach {exc.request.url}. This walkthrough needs both:\n"
            "  API:    .venv/Scripts/python.exe -m uvicorn app.main:app "
            "--loop app.core.event_loop:loop_factory --port 8000\n"
            "  worker: .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker "
            "--loglevel=info --pool=solo -Q notifications,sla",
            file=sys.stderr,
        )
        sys.exit(2)
    except OSError as exc:
        print(
            f"\nCould not open a socket ({exc}). The API must be running with a "
            "WebSocket-capable server — `uvicorn[standard]` installs `websockets` as an extra.",
            file=sys.stderr,
        )
        sys.exit(2)
