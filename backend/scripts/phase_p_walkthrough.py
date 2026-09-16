"""Phase P end to end: a notification written by a request, and an email sent by a worker.

The test suite asserts the policy, the task, and every route already, through
`TestClient`. This script exists for the three things a test cannot show:

* **That the message left the process.** Everything in the suite replaces `send_email`
  or the enqueue. Here a real worker picks up a real task from a real Redis, opens a real
  SMTP connection to Mailpit, and the message is read back out of Mailpit's HTTP API. The
  chain API → broker → worker → SMTP → Mailpit has no seam in it.
* **That the delivery was recorded on the row**, and that the API does not expose that
  column — which is the difference between the notification being the source of truth and
  the email being it.
* **That the whole thing survives a real HTTP connection to a real uvicorn**, with the
  bytes on the wire rather than in a transport.

Three processes are needed, and the worker is the one this phase added.

    # 1. the API, from backend/
    .venv/Scripts/python.exe -m uvicorn app.main:app \\
        --loop app.core.event_loop:loop_factory --port 8000

    # 2. the worker, from backend/
    .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker \\
        --loglevel=info --pool=solo

    # 3. this script, from backend/
    .venv/Scripts/python.exe scripts/phase_p_walkthrough.py

`--pool=solo` on Windows, not `prefork`: the default pool forks, and fork is not available
on Windows. `solo` runs one task at a time in the main process, which is what makes
`app/core/event_loop.py`'s `run()` usable from a task at all (ADR-011).

Two things to know before running it. It reads the database directly for the two claims no
response exposes — `emailed_at`, and whether a refused read wrote anything — so it needs
the same `.env` the server uses. And it registers organizations it does not delete, so
point it at a development database rather than a shared one.
"""

# ruff: noqa: T201

import asyncio
import sys
import time
import uuid
from datetime import datetime
from typing import Any, cast

import httpx
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import engine
from app.core.event_loop import run

BASE = "http://localhost:8000/api/v1"
PASSWORD = "correct-horse-battery-staple"

# Mailpit's own HTTP API, which is why it is the local mail server of choice here: the
# message can be read back by the same script that caused it, rather than by a person
# looking at a UI.
MAILPIT = "http://localhost:8025"

# Long enough for a solo worker to pick up a task, open an SMTP connection, and have
# Mailpit index the result. The poll below is what makes this reliable rather than slow.
EMAIL_TIMEOUT_SECONDS = 25.0

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. The script runs to the end even after a failure.

    A walkthrough that stops at the first problem hides the rest of the story, and seeing
    the rest of the story is why one walks through rather than running the suite, which
    already stops at the first problem.
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


class Api:
    """One signed-in caller: an HTTP client carrying a bearer token."""

    def __init__(self, token: str, email: str) -> None:
        self.token = token
        self.email = email
        self._client = httpx.Client(base_url=BASE, timeout=30.0)

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._client.request(
            method, path, headers={"Authorization": f"Bearer {self.token}"}, **kwargs
        )

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def notifications(self, **params: Any) -> list[dict[str, Any]]:
        response = self.get("/notifications", params=params)
        response.raise_for_status()
        return list(response.json())

    def unread(self) -> int:
        response = self.get("/notifications/unread-count")
        response.raise_for_status()
        return int(response.json()["unread"])


class Tenant:
    """A registered organization, with its founding admin's session."""

    def __init__(self, label: str) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.name = f"{label} {suffix}"
        self.email = f"admin-{suffix}@walkthrough.example"
        self._counter = 0

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

    def member(self, role: str, **extra: Any) -> tuple[Api, dict[str, Any]]:
        """Create a user in this organization, and return their session and record."""
        self._counter += 1
        email = f"member{self._counter}-{uuid.uuid4().hex[:6]}@walkthrough.example"

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
        return Api(logged_in.json()["access_token"], email), dict(created.json())

    def customer(self, name: str, email: str) -> dict[str, Any]:
        response = self.admin.post("/customers", json={"name": name, "email": email})
        response.raise_for_status()
        return dict(response.json())

    def ticket(self, customer_id: str, subject: str = "Something is broken") -> dict[str, Any]:
        response = self.admin.post(
            "/tickets",
            json={
                "subject": subject,
                "description": "It does not work.",
                "customer_id": customer_id,
            },
        )
        response.raise_for_status()
        return dict(response.json())

    def assign(self, ticket_id: str, agent_id: str) -> httpx.Response:
        return self.admin.post(f"/tickets/{ticket_id}/assign", json={"assigned_agent_id": agent_id})

    def move(self, ticket_id: str, status: str) -> httpx.Response:
        return self.admin.post(f"/tickets/{ticket_id}/status", json={"status": status})


# ---------------------------------------------------------------------------
# Mailpit
# ---------------------------------------------------------------------------


async def email_to(address: str, *, subject_contains: str = "") -> dict[str, Any] | None:
    """Poll Mailpit until a message for `address` arrives. `None` if it never does.

    Polled rather than slept on, because the delivery is asynchronous by design — the
    request returned before the worker had even seen the task — and a fixed sleep would be
    either flaky or slow depending on the machine.
    """
    deadline = time.monotonic() + EMAIL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        # Off the loop. This script is async only because it reads the database, and
        # `httpx` has no async client here — so the blocking call goes to a thread rather
        # than stalling the loop the database is being read on.
        response = await asyncio.to_thread(httpx.get, f"{MAILPIT}/api/v1/messages", timeout=30.0)
        response.raise_for_status()
        for message in response.json()["messages"]:
            recipients = {recipient["Address"] for recipient in message.get("To", [])}
            if address in recipients and subject_contains in message["Subject"]:
                return dict(message)
        await asyncio.sleep(0.5)
    return None


def email_text(message_id: str) -> str:
    """The plain-text body of a delivered message."""
    response = httpx.get(f"{MAILPIT}/api/v1/message/{message_id}", timeout=30.0)
    response.raise_for_status()
    return str(response.json()["Text"])


# ---------------------------------------------------------------------------
# The database, for the two things no response exposes
# ---------------------------------------------------------------------------


async def delivery_stamp(notification_id: str) -> datetime | None:
    """`notifications.emailed_at`, which is deliberately not in the API's payload."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT emailed_at FROM notifications WHERE id = :id"),
                {"id": notification_id},
            )
        ).one()
    # `Row` is untyped — the driver decides what a `timestamptz` becomes, and SQLAlchemy
    # cannot narrow it from the statement text — so the annotation is asserted here rather
    # than checked. `timestamptz` is a `datetime` or NULL, which is what this says.
    return cast("datetime | None", row[0])


async def stored_rows(ticket_id: str) -> int:
    """How many notification rows exist for a ticket, across every user."""
    async with engine.connect() as connection:
        return int(
            (
                await connection.execute(
                    text("SELECT count(*) FROM notifications WHERE ticket_id = CAST(:id AS uuid)"),
                    {"id": ticket_id},
                )
            ).scalar_one()
        )


async def recipient_of(notification_id: str) -> str:
    """The `user_id` on a notification row — the field the API deliberately omits."""
    return str(await _notification_field(notification_id, "user_id"))


async def organization_of(notification_id: str) -> str:
    """The `organization_id` on a notification row, likewise omitted."""
    return str(await _notification_field(notification_id, "organization_id"))


async def _notification_field(notification_id: str, field: str) -> object:
    """Read one field of one notification row.

    Two literal statements rather than one built from `field`, because a table name
    interpolated from a variable is the shape SQL injection arrives in — and a reader of
    the two-line version does not have to establish that the variable is safe. The
    parameter is not, and stays a bound parameter.
    """
    statement = (
        text("SELECT user_id FROM notifications WHERE id = CAST(:id AS uuid)")
        if field == "user_id"
        else text("SELECT organization_id FROM notifications WHERE id = CAST(:id AS uuid)")
    )
    async with engine.connect() as connection:
        row = (await connection.execute(statement, {"id": notification_id})).one()
    return row[0]


# ---------------------------------------------------------------------------
# 1. The whole chain, once
# ---------------------------------------------------------------------------


async def an_assignment_becomes_a_row_and_an_email() -> tuple[Tenant, dict[str, Any]]:
    section("1. An assignment writes a notification and sends the email")
    tenant = Tenant("Walkthrough")
    agent, agent_record = tenant.member("agent")
    customer = tenant.customer("Dana Scully", f"scully-{uuid.uuid4().hex[:6]}@walkthrough.example")
    ticket = tenant.ticket(customer["id"], subject="The spectrometer is drifting")

    assigned = tenant.assign(ticket["id"], agent_record["id"])
    check("the assignment succeeds", assigned.status_code == 200, assigned.text)

    inbox = agent.notifications()
    check("the agent has exactly one notification", len(inbox) == 1, str(inbox))
    if not inbox:
        return tenant, {"ticket": ticket, "customer": customer, "agent": agent, "inbox": inbox}

    notification = inbox[0]
    check(
        "it is the assignment type",
        notification["notification_type"] == "ticket_assigned",
        notification["notification_type"],
    )
    check(
        "the body names the ticket by number and subject",
        notification["body"] == f"#{ticket['number']} - The spectrometer is drifting",
        notification["body"],
    )
    check("the ticket id is on it", notification["ticket_id"] == ticket["id"])

    message = await email_to(agent.email, subject_contains="Ticket assigned to you")
    check("the email arrived at Mailpit", message is not None, agent.email)
    if message is not None:
        check(
            "the subject is bracketed with the product name",
            message["Subject"] == f"[{get_settings().PROJECT_NAME}] Ticket assigned to you",
            message["Subject"],
        )
        body = email_text(message["ID"])
        check("the body carries the ticket reference", notification["body"] in body, body[:120])
        check("the body tells the reader where to go", "Open " in body, body[:120])

    stamp = await delivery_stamp(notification["id"])
    check("the row records that the email went out", stamp is not None, "emailed_at is NULL")

    return tenant, {
        "ticket": ticket,
        "customer": customer,
        "agent": agent,
        "agent_record": agent_record,
        "notification": notification,
    }


async def the_api_does_not_expose_the_delivery(world: dict[str, Any]) -> None:
    section("2. The payload carries no tenant, recipient, or delivery state")
    agent = world["agent"]
    payload = agent.notifications()[0]

    check(
        "the fields are exactly the ones a client renders",
        set(payload)
        == {
            "id",
            "notification_type",
            "title",
            "body",
            "ticket_id",
            "read_at",
            "created_at",
        },
        str(sorted(payload)),
    )
    check("no emailed_at", "emailed_at" not in payload)
    check("no organization_id", "organization_id" not in payload)
    check("no user_id", "user_id" not in payload)

    # Searched for as a substring rather than only as a key name, because the reason the
    # field is absent is that a client has no use for it — and a tenant id leaking into,
    # say, a `body` would be the same disclosure in a different shape. Both ids are read
    # from the row rather than guessed, so the check is against the real values.
    serialized = str(payload)
    check(
        "the recipient's id appears nowhere in it",
        str(await recipient_of(payload["id"])) not in serialized,
    )
    check(
        "and neither does the organization's",
        str(await organization_of(payload["id"])) not in serialized,
    )


# ---------------------------------------------------------------------------
# 3. Who each event reaches
# ---------------------------------------------------------------------------


async def a_reply_notifies_the_assignee_and_a_reply_by_staff_does_not(
    tenant: Tenant, world: dict[str, Any]
) -> None:
    section("3. A customer reply notifies the assignee; a staff reply notifies nobody")
    agent, ticket = world["agent"], world["ticket"]
    customer = world["customer"]

    portal, _ = tenant.member("customer", customer_id=customer["id"])
    replied = portal.post(f"/tickets/{ticket['id']}/messages", json={"body": "Any news on this?"})
    check("the customer's reply is accepted", replied.status_code == 201, replied.text)

    types = [row["notification_type"] for row in agent.notifications()]
    check(
        "the assignee is told about the customer's reply",
        types[0] == "new_customer_reply",
        str(types),
    )
    reply_email = await email_to(agent.email, subject_contains="New reply from the customer")
    check("and an email is sent for it", reply_email is not None)

    staff_reply = agent.post(f"/tickets/{ticket['id']}/messages", json={"body": "Looking into it."})
    check("the agent's own reply is accepted", staff_reply.status_code == 201, staff_reply.text)
    note = agent.post(f"/tickets/{ticket['id']}/notes", json={"body": "Escalating internally."})
    check("the internal note is accepted", note.status_code == 201, note.text)

    check(
        "neither added a notification",
        [row["notification_type"] for row in agent.notifications()] == types,
        str([row["notification_type"] for row in agent.notifications()]),
    )


async def resolving_reaches_every_portal_login(tenant: Tenant, world: dict[str, Any]) -> None:
    section("4. Resolving notifies every portal login the customer has")
    ticket = world["ticket"]
    customer = world["customer"]

    # A second login for the *same* customer record. Nothing forbids it, and until this
    # phase nothing depended on how many there were: resolving used to resolve the
    # customer to one login and raise `MultipleResultsFound` — a 500 — the moment a
    # customer had two. Two is the whole point of this section.
    first, _ = tenant.member("customer", customer_id=customer["id"])
    second, _ = tenant.member("customer", customer_id=customer["id"])

    # Already assigned to this agent since section 1, and re-assigning an assigned ticket
    # to the same person is not an edge in `TICKET_TRANSITIONS`.
    tenant.move(ticket["id"], "in_progress")
    resolved = tenant.move(ticket["id"], "resolved")
    check("the resolution succeeds", resolved.status_code == 200, resolved.text)

    for label, portal in (("first", first), ("second", second)):
        theirs = portal.notifications()
        check(
            f"the {label} portal login is notified",
            [row["notification_type"] for row in theirs] == ["ticket_resolved"],
            str([row["notification_type"] for row in theirs]),
        )
        check(
            f"the {label} login's email arrived",
            await email_to(portal.email, subject_contains="Your ticket has been resolved")
            is not None,
        )


async def a_customer_with_no_login_is_told_nobody(tenant: Tenant, world: dict[str, Any]) -> None:
    section("5. A customer with no portal login produces no row at all")

    # A fresh agent, so the row count before the resolution is exactly the assignment's:
    # one. If resolving added one for a customer nobody can log in as, the count after
    # would be two.
    agent, agent_record = tenant.member("agent")
    record = tenant.customer("No Login", f"nologin-{uuid.uuid4().hex[:6]}@walkthrough.example")
    ticket = tenant.ticket(record["id"], subject="Nobody can sign in as this customer")

    tenant.assign(ticket["id"], agent_record["id"])
    tenant.move(ticket["id"], "in_progress")

    before = await stored_rows(ticket["id"])
    check("the assignment wrote its one row", before == 1, f"{before} rows")

    resolved = tenant.move(ticket["id"], "resolved")
    check("the resolution still succeeds", resolved.status_code == 200, resolved.text)
    check(
        "and wrote no row, because there is nobody to write it for",
        await stored_rows(ticket["id"]) == 1,
        f"{await stored_rows(ticket['id'])} rows",
    )
    check("the new agent's inbox holds only the assignment", len(agent.notifications()) == 1)


# ---------------------------------------------------------------------------
# 6. Reading, and who cannot
# ---------------------------------------------------------------------------


async def reading_and_clearing_the_badge(tenant: Tenant, world: dict[str, Any]) -> None:
    section("6. The badge, marking one read, and marking all read")
    agent = world["agent"]

    before = agent.unread()
    check("the assignee has unread notifications", before > 0, str(before))

    first_page = agent.notifications(limit=2)
    check("a page of two comes back", len(first_page) == 2, str(len(first_page)))
    check(
        "the list is newest first",
        [row["id"] for row in first_page] == [row["id"] for row in agent.notifications()][:2],
    )
    check(
        "unread_only filters to the same set while everything is unread",
        len(agent.notifications(unread_only=True)) == before,
    )

    marked = agent.post(f"/notifications/{first_page[0]['id']}/read")
    check("marking one read succeeds", marked.status_code == 200, marked.text)
    check("it reports a read time", marked.json()["read_at"] is not None)
    again = agent.post(f"/notifications/{first_page[0]['id']}/read")
    check(
        "marking it again does not move the timestamp",
        again.json()["read_at"] == marked.json()["read_at"],
        f"{again.json()['read_at']} != {marked.json()['read_at']}",
    )
    check("the badge drops by one", agent.unread() == before - 1, str(agent.unread()))

    cleared = agent.post("/notifications/read-all")
    check("clearing succeeds", cleared.status_code == 200, cleared.text)
    check("it reports what changed", cleared.json()["marked_read"] == before - 1)
    check("the badge is empty", agent.unread() == 0, str(agent.unread()))
    check(
        "a second clear changes nothing",
        agent.post("/notifications/read-all").json() == {"marked_read": 0},
    )


async def a_colleague_cannot_read_it(tenant: Tenant, world: dict[str, Any]) -> None:
    section("7. A colleague's notification is a 404, and the row is untouched")
    colleague, _ = tenant.member("agent")
    target = world["notification"]
    owner = world["agent"]

    marked = owner.post(f"/notifications/{target['id']}/read")
    check("the owner can read it", marked.status_code == 200, marked.text)
    stamp_before = marked.json()["read_at"]

    refused = colleague.post(f"/notifications/{target['id']}/read")
    absent = colleague.post(f"/notifications/{uuid.uuid4()}/read")
    check("a colleague is refused", refused.status_code == 404, refused.text)
    check(
        "with the same body as an id nobody wrote",
        refused.json() == absent.json(),
        f"{refused.json()} != {absent.json()}",
    )
    check("the colleague's inbox is empty", colleague.notifications() == [])
    check("the colleague's badge is zero", colleague.unread() == 0)

    # Read back through the *list*, which is the endpoint a client renders, and by id:
    # the target is the oldest notification, so `[0]` would be a different row and two
    # read times from two rows would differ for a reason that has nothing to do with the
    # refusal. (The first draft compared exactly those two rows and failed by 79ms.)
    def as_stored(rows: list[dict[str, Any]]) -> str | None:
        return cast("str | None", next(row["read_at"] for row in rows if row["id"] == target["id"]))

    unchanged = as_stored(owner.notifications())
    check(
        "the owner's read time did not move",
        unchanged == stamp_before,
        f"{unchanged} != {stamp_before}",
    )
    check(
        "and a refused clear-all changed nothing",
        colleague.post("/notifications/read-all").json() == {"marked_read": 0},
    )


async def another_organization_cannot_read_it(world: dict[str, Any]) -> None:
    section("8. Another tenant's 404 is identical to a missing one")
    outsider = Tenant("Outsider")
    target = world["notification"]

    refused = outsider.admin.post(f"/notifications/{target['id']}/read")
    absent = outsider.admin.post(f"/notifications/{uuid.uuid4()}/read")

    check("the outsider is refused", refused.status_code == 404, refused.text)
    check(
        "with the same body as an id nobody wrote",
        refused.json() == absent.json(),
        f"{refused.json()} != {absent.json()}",
    )
    check("the outsider's inbox is empty", outsider.admin.notifications() == [])
    check(
        "and the outsider cannot clear the badge",
        outsider.admin.post("/notifications/read-all").json() == {"marked_read": 0},
    )


async def main() -> None:
    tenant, world = await an_assignment_becomes_a_row_and_an_email()
    if "notification" in world:
        await the_api_does_not_expose_the_delivery(world)
        await a_reply_notifies_the_assignee_and_a_reply_by_staff_does_not(tenant, world)
        await resolving_reaches_every_portal_login(tenant, world)
        await a_customer_with_no_login_is_told_nobody(tenant, world)
        await reading_and_clearing_the_badge(tenant, world)
        await a_colleague_cannot_read_it(tenant, world)
        await another_organization_cannot_read_it(world)
    else:
        check("a notification was written for the assignment", False, "nothing was written")

    print(f"\n{_passed} passed, {_failed} failed")
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        run(main())
    except httpx.ConnectError as exc:
        print(
            f"\nCould not reach {exc.request.url}. This walkthrough needs all three:\n"
            "  API:    .venv/Scripts/python.exe -m uvicorn app.main:app "
            "--loop app.core.event_loop:loop_factory --port 8000\n"
            "  worker: .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker "
            "--loglevel=info --pool=solo\n"
            "  mail:   docker compose up -d mailpit",
            file=sys.stderr,
        )
        sys.exit(2)
