"""Phases L-N end to end, against a running server.

The test suite asserts nearly all of this already, through `TestClient`. This script
exists for the one thing `TestClient` cannot show: that the whole stack works over a real
HTTP connection to a real uvicorn process — real multipart parsing, real streaming
responses, real headers on the wire, with MinIO and Postgres actually behind them.

Start the API first (from `backend/`):

    .venv/Scripts/python.exe -m uvicorn app.main:app \\
        --loop app.core.event_loop:loop_factory --port 8000

then:

    .venv/Scripts/python.exe scripts/phase_ln_walkthrough.py

Two things to know before running it. It reads the database directly for the one claim
no response exposes — the stored object key, which the API deliberately never returns —
so it needs the same `.env` the server uses. And it registers organizations it does not
delete, so point it at a development database rather than a shared one.
"""

# ruff: noqa: T201

import sys
import uuid
from typing import Any

import httpx
from sqlalchemy import text

from app.core.database import engine
from app.core.event_loop import run

BASE = "http://localhost:8000/api/v1"
PASSWORD = "correct-horse-battery-staple"

PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"walkthrough" * 8
TEXT_BODY = b"2026-09-15 ERROR the sensor drifted out of range\n"

# A word that appears in one ticket's subject and nowhere else...
SUBJECT_WORD = "drifting"
# ...and one that appears only in the body of an internal note.
INTERNAL_PHRASE = "miscalibrated"

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. The script runs to the end even after a failure.

    A walkthrough that stops at the first problem hides the rest of the story, and
    seeing the rest of the story is the reason to walk through rather than to run the
    suite, which already stops at the first problem.
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

    def ids(self, path: str, **params: Any) -> set[str]:
        """The `id` of every row a list route returns."""
        response = self.get(path, params={"limit": 100, **params})
        response.raise_for_status()
        return {row["id"] for row in response.json()}


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
        self.me: dict[str, Any] = self.admin.get("/auth/me").json()

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

    def ticket(
        self, customer_id: str, subject: str = "Something is broken", **fields: Any
    ) -> dict[str, Any]:
        response = self.admin.post(
            "/tickets",
            json={
                "subject": subject,
                "description": "It does not work.",
                "customer_id": customer_id,
                **fields,
            },
        )
        response.raise_for_status()
        return dict(response.json())


def png(filename: str = "screenshot.png") -> dict[str, Any]:
    return {"file": (filename, PNG_BODY, "image/png")}


def unauthenticated(path: str) -> httpx.Response:
    """A request with no bearer token at all, for the 401 case.

    Synchronous on purpose — the walkthrough's `async` functions exist only because the
    storage-key query needs the database loop, and `httpx.AsyncClient` would buy nothing
    for a script that makes one request at a time.
    """
    return httpx.get(f"{BASE}{path}", timeout=30.0)


async def stored_key(attachment_id: str) -> str:
    """Read a stored object key from the database.

    No response exposes it, by design — see ADR-018. Reading it here is the only way to
    assert the property that makes path traversal unreachable rather than merely
    unsanitized: that the key was composed from server-side values alone.
    """
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT storage_key FROM attachments WHERE id = :id"),
                {"id": attachment_id},
            )
        ).one()
    return str(row[0])


def search(api: Api, term: str) -> set[str]:
    response = api.get("/tickets", params={"q": term, "limit": 100})
    response.raise_for_status()
    return {row["id"] for row in response.json()}


def audit_actions(api: Api, **params: Any) -> list[str]:
    response = api.get("/audit-logs", params={"limit": 100, **params})
    response.raise_for_status()
    return [row["action"] for row in response.json()]


async def registration_is_audited() -> tuple[Tenant, dict[str, Any]]:
    section("1. Registering an organization writes its first audit row")
    tenant = Tenant("Walkthrough")
    trail = tenant.admin.get("/audit-logs")
    check("the founding admin can read the trail", trail.status_code == 200, trail.text)

    rows = trail.json()
    check(
        "registration is itself audited",
        [row["action"] for row in rows] == ["user_created"],
        str([row["action"] for row in rows]),
    )
    check(
        "the actor is the admin who just registered",
        rows[0]["actor_email"] == tenant.email,
        str(rows[0]["actor_email"]),
    )
    check(
        "the target is that same user",
        rows[0]["target_id"] == tenant.me["id"],
        f"{rows[0]['target_id']} != {tenant.me['id']}",
    )
    check("the request's provenance is recorded", rows[0]["user_agent"] is not None)

    customer = tenant.customer("Fox Mulder", "lookout@walkthrough.example")
    ticket = tenant.ticket(customer["id"], subject=f"The pyrheliometer is {SUBJECT_WORD}")
    return tenant, {"customer": customer, "ticket": ticket}


async def an_upload_is_stored_under_a_server_composed_key(
    tenant: Tenant, world: dict[str, Any]
) -> None:
    section("2. A ticket-level upload lands, and does not leak its key")
    ticket_id = world["ticket"]["id"]
    uploaded = tenant.admin.post(f"/tickets/{ticket_id}/attachments", files=png())
    check("the upload is accepted", uploaded.status_code == 201, uploaded.text)

    attachment = uploaded.json()
    check("the response carries no storage key", "storage_key" not in attachment)
    check(
        "the display name is the client's, unchanged",
        attachment["filename"] == "screenshot.png",
        attachment["filename"],
    )
    check(
        "the size is what was sent",
        attachment["size_bytes"] == len(PNG_BODY),
        str(attachment["size_bytes"]),
    )

    key = await stored_key(attachment["id"])
    check("the key has no traversal segment", ".." not in key, key)
    check("the key carries no client-supplied text", "screenshot" not in key, key)
    check(
        "the key is organization / ticket / a fresh id",
        key.count("/") == 2 and key.split("/")[1] == ticket_id,
        key,
    )

    events = tenant.admin.get(f"/tickets/{ticket_id}/events").json()
    check(
        "the timeline records the attachment exactly once",
        [event["event_type"] for event in events].count("attachment_added") == 1,
        str([event["event_type"] for event in events]),
    )
    world["attachment"] = attachment


async def an_attachment_on_an_internal_note_is_internal(
    tenant: Tenant, world: dict[str, Any]
) -> None:
    section("3. An attachment on an internal note is internal")
    other = tenant.ticket(world["customer"]["id"], subject="Unrelated")

    note = tenant.admin.post(
        f"/tickets/{other['id']}/notes",
        json={"body": f"Suspect the sensor is {INTERNAL_PHRASE}."},
    )
    check("the internal note is posted", note.status_code == 201, note.text)

    on_note = tenant.admin.post(
        f"/tickets/{other['id']}/attachments",
        files={"file": ("readings.log", TEXT_BODY, "text/plain")},
        data={"message_id": note.json()["id"]},
    )
    check("the note's attachment is accepted", on_note.status_code == 201, on_note.text)
    internal_id = on_note.json()["id"]

    events = tenant.admin.get(f"/tickets/{other['id']}/events").json()
    check(
        "no customer-visible event names the internal file",
        "attachment_added" not in [event["event_type"] for event in events],
        str([event["event_type"] for event in events]),
    )

    portal, _ = tenant.member("customer", customer_id=world["customer"]["id"])
    check(
        "the customer can reach the ticket that holds the note",
        portal.get(f"/tickets/{other['id']}").status_code == 200,
    )

    listed = tenant.admin.get(f"/tickets/{other['id']}/attachments").json()
    check(
        "staff see it",
        {row["id"] for row in listed} == {internal_id},
        str([row["id"] for row in listed]),
    )
    check(
        "the customer's list does not",
        internal_id not in portal.ids(f"/tickets/{other['id']}/attachments"),
    )

    refused = portal.get(f"/attachments/{internal_id}")
    absent = portal.get(f"/attachments/{uuid.uuid4()}")
    check("fetching it directly is a 404", refused.status_code == 404, refused.text)
    check("with the status a missing id gets", refused.status_code == absent.status_code)
    check("and a byte-identical body", refused.content == absent.content, str(refused.content))

    world["other"] = other
    world["portal"] = portal


async def validation_refuses_what_the_bytes_contradict(
    tenant: Tenant, world: dict[str, Any]
) -> None:
    section("4. Extension, declared type, and bytes must agree")
    ticket_id = world["ticket"]["id"]

    traversal = tenant.admin.post(f"/tickets/{ticket_id}/attachments", files=png("../../evil.png"))
    check("a traversal-shaped name is accepted", traversal.status_code == 201, traversal.text)
    check(
        "and flattened to its last component for display",
        traversal.json()["filename"] == "evil.png",
        traversal.json().get("filename", ""),
    )
    traversal_key = await stored_key(traversal.json()["id"])
    check("the key it produced is still server-composed", ".." not in traversal_key, traversal_key)

    disguised = tenant.admin.post(
        f"/tickets/{ticket_id}/attachments",
        files={"file": ("shot.png", b"MZ\x90\x00", "image/png")},
    )
    check("an executable renamed .png is refused", disguised.status_code == 422, disguised.text)
    check(
        "with UNSUPPORTED_FILE_TYPE",
        disguised.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE",
        disguised.text,
    )

    executable = tenant.admin.post(
        f"/tickets/{ticket_id}/attachments",
        files={"file": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
    )
    check(
        "an extension outside the allowlist is refused",
        executable.status_code == 422,
        executable.text,
    )

    lying_type = tenant.admin.post(
        f"/tickets/{ticket_id}/attachments",
        files={"file": ("shot.png", PNG_BODY, "application/pdf")},
    )
    check(
        "a declared type the extension does not own is refused",
        lying_type.status_code == 422,
        lying_type.text,
    )


async def downloads_carry_their_headers_and_nothing_else(
    tenant: Tenant, world: dict[str, Any]
) -> None:
    section("5. Download headers, and a refusal that says nothing")
    attachment_id = world["attachment"]["id"]
    download = tenant.admin.get(f"/attachments/{attachment_id}")

    check("the download succeeds", download.status_code == 200, download.text)
    check("the bytes round-trip", download.content == PNG_BODY)
    check("the type is the detected one", download.headers["content-type"] == "image/png")
    check(
        "it is an attachment, not inline",
        download.headers["content-disposition"].startswith("attachment"),
        download.headers["content-disposition"],
    )
    check(
        "the name is carried in the disposition",
        "screenshot.png" in download.headers["content-disposition"],
        download.headers["content-disposition"],
    )
    check("nosniff is set", download.headers["x-content-type-options"] == "nosniff")
    check(
        "the length matches the stored size",
        int(download.headers["content-length"]) == len(PNG_BODY),
        download.headers["content-length"],
    )

    stranger = Tenant("Stranger")
    stolen = stranger.admin.get(f"/attachments/{attachment_id}")
    never = stranger.admin.get(f"/attachments/{uuid.uuid4()}")
    check("another organization gets a 404", stolen.status_code == 404, stolen.text)
    check("byte-identical to an id that never existed", stolen.content == never.content)

    anonymous = unauthenticated(f"/attachments/{attachment_id}")
    check("no token at all is a 401", anonymous.status_code == 401, anonymous.text)

    check(
        "a third organization's trail holds only its own registration",
        audit_actions(stranger.admin) == ["user_created"],
        str(audit_actions(stranger.admin)),
    )


async def the_lifecycle_is_recorded_for_admins_only(tenant: Tenant, world: dict[str, Any]) -> None:
    section("6. The lifecycle writes rows only an admin can read")
    agent, agent_record = tenant.member("agent")
    manager, _ = tenant.member("manager")
    ticket_id = world["ticket"]["id"]

    assigned = tenant.admin.post(
        f"/tickets/{ticket_id}/assign", json={"assigned_agent_id": agent_record["id"]}
    )
    check("the ticket is assigned to a real agent", assigned.status_code == 200, assigned.text)
    check("and the assignment moved it to ASSIGNED", assigned.json()["status"] == "assigned")

    priority = tenant.admin.post(f"/tickets/{ticket_id}/priority", json={"priority": "high"})
    check("the priority is changed", priority.status_code == 200, priority.text)

    started = agent.post(f"/tickets/{ticket_id}/status", json={"status": "in_progress"})
    check("the assigned agent can start work", started.status_code == 200, started.text)

    resolved = agent.post(f"/tickets/{ticket_id}/status", json={"status": "resolved"})
    check("and resolve it", resolved.status_code == 200, resolved.text)

    closed = tenant.admin.post(f"/tickets/{ticket_id}/close")
    check("a resolved ticket closes", closed.status_code == 200, closed.text)

    recorded = audit_actions(tenant.admin)
    for expected in (
        "ticket_created",
        "ticket_assigned",
        "ticket_priority_changed",
        "ticket_status_changed",
        "ticket_resolved",
    ):
        check(f"{expected} is recorded", expected in recorded, str(recorded))

    changed = tenant.admin.get(
        "/audit-logs", params={"action": "ticket_priority_changed", "limit": 100}
    ).json()
    check("filtering by action selects exactly that action", len(changed) == 1, str(len(changed)))
    check(
        "before and after are both recorded",
        changed[0]["extra_data"].get("before") == {"priority": "medium"}
        and changed[0]["extra_data"].get("after") == {"priority": "high"},
        str(changed[0]["extra_data"]),
    )
    check("the actor survives on the row", changed[0]["actor_email"] == tenant.email)

    by_target = audit_actions(tenant.admin, target_type="ticket", target_id=ticket_id)
    check(
        "filtering by target gives that ticket's whole history",
        set(by_target)
        >= {"ticket_created", "ticket_assigned", "ticket_priority_changed", "ticket_resolved"},
        str(by_target),
    )

    by_actor = audit_actions(tenant.admin, actor_user_id=agent_record["id"])
    check(
        "filtering by actor gives that person's actions",
        set(by_actor) == {"ticket_status_changed", "ticket_resolved"},
        str(by_actor),
    )

    check("a manager is refused", manager.get("/audit-logs").status_code == 403)
    check("an agent is refused", agent.get("/audit-logs").status_code == 403)

    world["agent"] = agent
    world["agent_record"] = agent_record


async def search_reaches_every_field_and_no_further(tenant: Tenant, world: dict[str, Any]) -> None:
    section("7. Search reaches every field §14 names, and no further")
    ticket_id = world["ticket"]["id"]
    other_id = world["other"]["id"]
    staff = tenant.admin

    check("by a word in the subject", search(staff, SUBJECT_WORD) == {ticket_id})
    check(
        "by the ticket's number",
        search(staff, str(world["ticket"]["number"])) == {ticket_id},
        str(search(staff, str(world["ticket"]["number"]))),
    )
    check(
        "by the customer's email",
        search(staff, "lookout@walkthrough.example") == {ticket_id, other_id},
        str(search(staff, "lookout@walkthrough.example")),
    )
    check(
        "by a word in a message body",
        search(staff, "sensor") == {other_id},
        str(search(staff, "sensor")),
    )
    check("a term nobody wrote returns nothing", search(staff, "zzqqxx") == set())

    check(
        "a phrase only the internal note contains finds the ticket for staff",
        search(staff, INTERNAL_PHRASE) == {other_id},
        str(search(staff, INTERNAL_PHRASE)),
    )
    check(
        "and finds nothing for the customer who owns that ticket",
        search(world["portal"], INTERNAL_PHRASE) == set(),
        str(search(world["portal"], INTERNAL_PHRASE)),
    )

    percent_customer = tenant.customer("100% Discount", "percent@walkthrough.example")
    percent_ticket = tenant.ticket(percent_customer["id"], subject="An ordinary ticket")
    check(
        "a % in the term is a literal character, not a wildcard",
        search(staff, "%") == {percent_ticket["id"]},
        str(search(staff, "%")),
    )

    agent = world["agent"]
    out_of_scope = tenant.ticket(world["customer"]["id"], subject="Nobody owns this one")
    check(
        "a term matching a ticket outside the caller's scope returns nothing",
        search(agent, "Nobody") == set(),
        str(search(agent, "Nobody")),
    )
    my_queue = agent.ids("/tickets", assigned_agent_id=world["agent_record"]["id"])
    check(
        "and an agent's queue filter is their own queue",
        out_of_scope["id"] not in my_queue,
        str(my_queue),
    )


async def sorting_and_paging_are_stable(tenant: Tenant, world: dict[str, Any]) -> None:
    section("8. Sorting, filtering, and a stable page boundary")
    for priority in ("low", "urgent", "high"):
        tenant.ticket(world["customer"]["id"], priority=priority)

    descending = tenant.admin.get(
        "/tickets", params={"sort": "priority", "order": "desc", "limit": 100}
    ).json()
    check(
        "priority desc leads with URGENT",
        descending[0]["priority"] == "urgent",
        str([row["priority"] for row in descending]),
    )

    ascending = tenant.admin.get(
        "/tickets", params={"sort": "priority", "order": "asc", "limit": 100}
    ).json()
    check(
        "and asc leads with LOW",
        ascending[0]["priority"] == "low",
        str([row["priority"] for row in ascending]),
    )

    first = tenant.admin.get(
        "/tickets", params={"sort": "created_at", "limit": 2, "offset": 0}
    ).json()
    second = tenant.admin.get(
        "/tickets", params={"sort": "created_at", "limit": 2, "offset": 2}
    ).json()
    overlap = {row["id"] for row in first} & {row["id"] for row in second}
    check("two pages over the same sort key do not overlap", not overlap, str(overlap))

    unassigned = tenant.admin.get("/tickets", params={"unassigned": True, "limit": 100}).json()
    check(
        "every unassigned ticket really has no agent",
        all(row["assigned_agent_id"] is None for row in unassigned),
        str([row["assigned_agent_id"] for row in unassigned]),
    )
    check(
        "and the assigned one is not among them",
        world["ticket"]["id"] not in {row["id"] for row in unassigned},
    )

    both = tenant.admin.get(
        "/tickets", params={"unassigned": True, "assigned_agent_id": str(uuid.uuid4())}
    )
    check("asking for both at once is a 422", both.status_code == 422, both.text)

    check(
        "an unknown sort key is a 422, not a silent default",
        tenant.admin.get("/tickets", params={"sort": "subject"}).status_code == 422,
    )

    window = tenant.admin.get(
        "/tickets",
        params={"created_after": "2000-01-01T00:00:00Z", "created_before": "2000-01-02T00:00:00Z"},
    ).json()
    check("a window before anything was created is empty", window == [], str(window))

    inverted = tenant.admin.get(
        "/tickets",
        params={"created_after": "2030-01-01T00:00:00Z", "created_before": "2020-01-01T00:00:00Z"},
    )
    check("an inverted window is empty rather than an error", inverted.status_code == 200)


async def main() -> None:
    tenant, world = await registration_is_audited()
    await an_upload_is_stored_under_a_server_composed_key(tenant, world)
    await an_attachment_on_an_internal_note_is_internal(tenant, world)
    await validation_refuses_what_the_bytes_contradict(tenant, world)
    await downloads_carry_their_headers_and_nothing_else(tenant, world)
    await the_lifecycle_is_recorded_for_admins_only(tenant, world)
    await search_reaches_every_field_and_no_further(tenant, world)
    await sorting_and_paging_are_stable(tenant, world)

    print(f"\n{_passed} passed, {_failed} failed")
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        run(main())
    except httpx.ConnectError:
        print(
            "Could not reach the API at http://localhost:8000. Start it from backend/ with:\n"
            "  .venv/Scripts/python.exe -m uvicorn app.main:app "
            "--loop app.core.event_loop:loop_factory --port 8000",
            file=sys.stderr,
        )
        sys.exit(2)
