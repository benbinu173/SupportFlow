"""Phase W end to end: a draft nobody sent, and the moment a person sends it.

§21 is five sentences and a workflow diagram, and §41 lists what a person may do with what it
produces — *regenerate, edit, accept*. The suites prove each verb separately: a `SUGGEST_RESPONSE`
row goes `pending` to `completed`, the worker writes one internal `ai_draft` message, a second
request writes a second draft and audits `AI_RESPONSE_REGENERATED`, accepting posts a public
`agent` message and leaves the draft untouched, and the audit row carries both bodies and
whether they differ. What no test in the phase can show is the thing the phase is *for*:

* **That a real model drafts a reply to a real conversation, today, through the worker, with
  the key in `.env`.** Every other test in Phase W runs against `FakeProvider`, which is exactly
  right and exactly why something has to prove the live path is not well-tested fiction. The
  script posts a complaint, a follow-up and an internal note over HTTP, asks for a draft, and
  reads back what a worker process wrote.
* **That §21's containment is real and not a policy.** *"AI must NEVER automatically send a
  customer-facing response"* is asserted here the only way it can be asserted from outside: the
  **customer's own portal login** asks for the ticket's thread and does not get the draft, in
  section 2, before anybody has sent anything. Section 4 then shows the same login receiving the
  reply and still not the draft.
* **That accepting spends nothing.** §41's accept calls no model — the text was written by a
  person and the model was paid for when the draft was asked for — and the evidence is the bill:
  the script reads `/analytics/overview` either side of the acceptance and asserts `calls` and
  `cost_usd` did not move at all.

**Everything here is HTTP.** Unlike the Phase T script, this one never touches the database or
imports a service — Phase W is the phase that gave §21 its routes, so a walkthrough that reached
around the route would be bypassing the thing it is walking through. That is also why the worker
must be running for section 2 to finish.

Five steps, from `backend/`:

    # 1. the datastores (Docker Desktop must already be running)
    docker compose up -d postgres redis

    # 2. the API
    .venv/Scripts/python.exe -m uvicorn app.main:app \\
        --loop app.core.event_loop:loop_factory --port 8000

    # 3. the worker, in a third terminal — and `-Q` matters
    .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker \\
        --loglevel=info --pool=solo -Q notifications,sla,ai

    # then
    .venv/Scripts/python.exe scripts/phase_w_walkthrough.py

`AI_API_KEY` must be set in `.env`, and it is a real spend — small, and nothing on Groq's
free tier. With the key empty the script prints what to do and exits `0`: an absent key is a
legal configuration and not a failure of this phase.

**The `-Q` is not decoration.** `analyze_ticket` — the task a draft rides on — is routed to the
`ai` queue, and a worker started without `-Q` consumes only the default one. The symptom is
distinctive here: the creation analysis settles, because it was queued by an earlier run or not
at all, and then section 2 times out with a `pending` draft and no error anywhere.

**Section 1 waits for the ticket's own analysis before it takes the baseline**, for Phase V's
reason: §18 runs on ticket creation, and section 2's claim is that *the draft* changed nothing —
so the AI columns have to be final before the comparison is made.

It **registers two organizations**, and §45 limits registration to five an hour per address,
so a third run inside the hour reports `429` rather than a failed assertion:

    docker compose exec redis redis-cli -n 0 --scan --pattern 'ratelimit:register:*'

And it leaves everything behind — the organizations, the customer, the ticket, the messages
including the drafts, the `ai_analyses` rows, the audit rows, and the ledger rows. A spend
record that can be tidied away is not a spend record.
"""

# ruff: noqa: T201

import sys
import time
import uuid
from typing import Any

import httpx

from app.core.config import get_settings

BASE = "http://localhost:8000/api/v1"
PASSWORD = "correct-horse-battery-staple"

#: How long a section will wait for the worker. Generous, because it is waiting on a real
#: provider over the network; a timeout here means the worker is not running or is not
#: listening on the `ai` queue, and the closing note says so.
WORKER_TIMEOUT = 120.0

#: The two statuses that mean the row is finished, either way.
TERMINAL = {"completed", "failed"}

SUBJECT = "Cannot download my policy document"
DESCRIPTION = (
    "I have been trying to download my policy document from the portal since Tuesday and it "
    "fails every time. I have tried Chrome, Firefox and Safari."
)
FIRST_REPLY = "Tried again this morning in Safari -- same error, it never starts downloading."
INTERNAL_NOTE = (
    "Reproduced against the file store: it is degraded and serving 503s to everyone. "
    "Platform ticket PLAT-4417 is open. Do not promise a date until that closes."
)

#: What the agent actually sends in section 4. **Deliberately not the model's text**, which is
#: §41's *edit* rather than its *accept*: the audit row has a `before` and an `after` and they
#: have to differ for the row to be carrying anything.
SENT_BODY = (
    "Hello Ada,\n\n"
    "Thank you for your patience, and sorry for the trouble. We have found the fault on our "
    "side -- the file store that serves policy documents is degraded -- and the platform team "
    "is replacing it now. Downloads should start working again shortly, and you will not need "
    "to change anything on your side.\n\n"
    "If it is still failing tomorrow morning, reply here and I will chase it directly.\n\n"
    "Kind regards,\nSupport"
)

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. The script runs to the end even after a failure.

    A walkthrough that stopped at the first problem would hide the rest of the story, and
    seeing the rest of the story is why one walks through rather than running the suite.
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


class Principal:
    """A token and the calls that carry it. An admin, or a customer's own login."""

    def __init__(self, token: str) -> None:
        self.token = token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.get(f"{BASE}{path}", headers=self.headers, timeout=30.0, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.post(f"{BASE}{path}", headers=self.headers, timeout=60.0, **kwargs)


class Tenant(Principal):
    """A registered organization and its founding admin, and the calls this script makes."""

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
        super().__init__(str(response.json()["access_token"]))

    def raise_ticket(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """A customer, a ticket carrying a real complaint, and the customer's own login.

        The portal login is created through `POST /users` and then authenticates through
        `POST /auth/login`, the same two steps a real portal account takes. **It is what makes
        section 2's containment claim possible**: an administrator holds `MESSAGE_READ_INTERNAL`
        and can read the draft, so a script that only ever asked as the admin could not tell
        "the customer cannot see this" from "nobody can".
        """
        suffix = uuid.uuid4().hex[:8]
        customer = self.post(
            "/customers", json={"name": "Ada Lovelace", "email": f"ada-{suffix}@customer.example"}
        )
        customer.raise_for_status()
        customer_id = str(customer.json()["id"])

        address = f"portal-{suffix}@customer.example"
        created = self.post(
            "/users",
            json={
                "name": "Ada Lovelace",
                "email": address,
                "password": PASSWORD,
                "role": "customer",
                "customer_id": customer_id,
            },
        )
        created.raise_for_status()
        login = httpx.post(
            f"{BASE}/auth/login", json={"email": address, "password": PASSWORD}, timeout=30.0
        )
        login.raise_for_status()
        self.portal = Principal(str(login.json()["access_token"]))

        ticket = self.post(
            "/tickets",
            json={"subject": SUBJECT, "description": DESCRIPTION, "customer_id": customer_id},
        )
        ticket.raise_for_status()
        return dict(ticket.json()), dict(customer.json())

    def suggest(self, ticket_id: str) -> dict[str, Any]:
        """POST §21's route and return the row it answered with."""
        response = self.post(f"/tickets/{ticket_id}/ai/suggest-response")
        assert response.status_code == 202, response.text
        return dict(response.json())

    def accept(self, ticket_id: str, draft_id: str, body: str) -> httpx.Response:
        """POST §41's accept with `body` as the text to send."""
        return self.post(f"/tickets/{ticket_id}/ai/drafts/{draft_id}/accept", json={"body": body})

    def analyses(self, ticket_id: str) -> list[dict[str, Any]]:
        response = self.get(f"/tickets/{ticket_id}/ai/analyses")
        assert response.status_code == 200, response.text
        return list(response.json())

    def row_for(self, ticket_id: str, operation: str) -> dict[str, Any] | None:
        """The row the read route serves for `operation`, or `None` if there is none.

        `/ai/analyses` serves the newest row of each kind, one per operation, so this is the
        draft's current state and not a history of it. That is the right thing to poll: it is
        what the staff screen would be showing.
        """
        rows = [row for row in self.analyses(ticket_id) if row["operation"] == operation]
        return rows[0] if rows else None

    def draft_row(self, ticket_id: str) -> dict[str, Any] | None:
        return self.row_for(ticket_id, "suggest_response")

    def thread(self, ticket_id: str, *, as_portal: bool = False) -> list[dict[str, Any]]:
        """The ticket's messages, as the admin (who sees internal rows) or as the customer."""
        who = self.portal if as_portal else self
        response = who.get(f"/tickets/{ticket_id}/messages", params={"limit": 100})
        assert response.status_code == 200, response.text
        return list(response.json())

    def drafts_in(self, ticket_id: str, *, as_portal: bool = False) -> list[dict[str, Any]]:
        """Every `ai_draft` row in a thread as that caller reads it."""
        return [
            row
            for row in self.thread(ticket_id, as_portal=as_portal)
            if row["sender_type"] == "ai_draft"
        ]

    def audit(self, ticket_id: str) -> list[dict[str, Any]]:
        """This ticket's audit rows, oldest first — newest-first from the endpoint.

        Read through `/audit-logs` rather than a database client, for the reason the script
        gives about the database generally: §34's rows are read by the viewer, and the trail is
        where §41's regenerate and §41's accept are told apart.
        """
        response = self.get(
            "/audit-logs",
            params={"target_type": "ticket", "target_id": ticket_id, "limit": 100},
        )
        assert response.status_code == 200, response.text
        return list(reversed(response.json()))

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        response = self.get(f"/tickets/{ticket_id}")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def usage(self) -> dict[str, Any]:
        response = self.get("/analytics/overview")
        assert response.status_code == 200, response.text
        return dict(response.json()["ai_usage"])


def wait_until_settled(
    tenant: Tenant, ticket_id: str, operation: str, *, timeout: float = WORKER_TIMEOUT
) -> dict[str, Any] | None:
    """Poll the read route until the row for `operation` is terminal, or the timeout expires.

    Polling the API rather than watching the worker's log, because that is what a client does:
    the route exists so a browser can show a queued analysis instead of an empty list, and this
    is that route being used for its purpose.

    A timeout returns whatever it last saw — `None` if no row exists at all — so the caller
    reports the real state instead of a bare "timed out".
    """
    deadline = time.monotonic() + timeout
    while True:
        row = tenant.row_for(ticket_id, operation)
        if row is not None and row["status"] in TERMINAL:
            return row
        if time.monotonic() >= deadline:
            return row
        time.sleep(2.0)


def spend(usage: dict[str, Any]) -> str:
    """One `/analytics/overview` reading, in a line — the numbers §41's accept turns on."""
    return f"calls={usage['calls']} cached={usage['cached_calls']} cost={usage['cost_usd']}"


# ---------------------------------------------------------------------------
# The sections
# ---------------------------------------------------------------------------


def a_ticket_with_a_conversation(tenant: Tenant) -> tuple[str, dict[str, Any]]:
    section("1. §21 sentences 1-2: a ticket with a conversation, and nothing drafted yet")

    ticket, customer = tenant.raise_ticket()
    ticket_id = str(ticket["id"])
    check("the ticket was created", ticket["status"] == "open", str(ticket["status"]))
    print(f"  customer: {customer['name']}  ticket: {ticket['number']}")

    # Wait for the creation analysis, so the baseline section 2 compares against is final.
    analyzed = wait_until_settled(tenant, ticket_id, "classify")
    print(f"  the ticket's own §18 analysis settled: {analyzed and analyzed['status']}")
    baseline = tenant.ticket(ticket_id)

    said = tenant.portal.post(f"/tickets/{ticket_id}/messages", json={"body": FIRST_REPLY})
    said.raise_for_status()
    noted = tenant.post(f"/tickets/{ticket_id}/notes", json={"body": INTERNAL_NOTE})
    noted.raise_for_status()
    check("a customer message and an internal note were posted", True)
    print("  the note is staff-only, and it is in the prompt -- the model reads it as context.")

    check("no draft exists yet", tenant.draft_row(ticket_id) is None)
    check("and no ai_draft message is in the thread", tenant.drafts_in(ticket_id) == [])

    queued = tenant.suggest(ticket_id)
    print(f"  queued: {queued['operation']}  status={queued['status']}")
    check("the route accepted the request", queued["operation"] == "suggest_response")
    # A `pending` row is the queued case; a `completed` one means the worker won the race,
    # which is legal and means the *next* section has nothing to wait for.
    check(
        "it answered with one row, queued or already done",
        queued["status"] in {"pending", "processing", "completed"},
        queued["status"],
    )
    check(
        "the row names the model that will be asked",
        queued["provider"] == get_settings().AI_PROVIDER
        and queued["model"] == get_settings().AI_MODEL,
        f"{queued['provider']}/{queued['model']}",
    )
    # §21's containment, stated as the thing this route did *not* do. The response is a record
    # of a queued call; nothing customer-visible exists, and nothing will until section 4.
    check(
        "the response is not the reply -- nothing has been sent",
        "body" not in queued or queued["result"] is None,
        str(queued["result"]),
    )
    return ticket_id, baseline


def the_worker_drafts_it(
    tenant: Tenant, ticket_id: str, baseline: dict[str, Any]
) -> dict[str, Any] | None:
    section("2. §21 sentence 1: the worker drafts it, in another process -- and it is staff-only")

    started = time.monotonic()
    row = wait_until_settled(tenant, ticket_id, "suggest_response")
    elapsed = time.monotonic() - started
    print(f"  settled after {elapsed:.1f}s: status={row and row['status']}")

    if row is None or row["status"] != "completed":
        check("the draft completed", False, str(row))
        if row and row["status"] == "failed":
            print(f"        failed: {row['error_message']}")
        return None

    check("the draft completed", True)
    body = (row["result"] or {}).get("body", "")
    print(f"  draft: {body!r}")
    check("it is a real reply somebody could send", len(body) > 40, str(len(body)))
    # §41 says to show confidence where it is meaningful, and `SuggestedReply` has no field for
    # one: a reply is edited rather than scored.
    check("the row carries no confidence", row["confidence"] is None, repr(row["confidence"]))
    check(
        "the call was billed for its tokens",
        bool(row["prompt_tokens"]) and bool(row["completion_tokens"]),
        f"{row['prompt_tokens']}/{row['completion_tokens']}",
    )

    # §21 stores a draft; it does not apply one. Every field the §18 analysis wrote is compared
    # against the snapshot taken after that analysis settled.
    after = tenant.ticket(ticket_id)
    watched = (
        "category",
        "subcategory",
        "sentiment",
        "sentiment_confidence",
        "ai_classification_confidence",
        "ai_recommended_priority",
        "priority",
        "status",
        "first_response_at",
    )
    changed = {
        field: (baseline[field], after[field])
        for field in watched
        if baseline[field] != after[field]
    }
    check("the draft changed nothing on the ticket", not changed, str(changed))

    # **The containment claim, and this is the line the section exists for.** The draft is in
    # the thread as `ai_draft` and internal -- it is not customer-visible, and the customer's own
    # portal login is what proves it. §21: "AI must NEVER automatically send a customer-facing
    # response."
    staff = tenant.drafts_in(ticket_id)
    check("the worker wrote one ai_draft row into the thread", len(staff) == 1, str(len(staff)))
    if staff:
        check(
            "it is internal, and it was written by nobody in the tenant",
            staff[0]["is_internal"] is True and staff[0]["sender_user_id"] is None,
        )
        check("its body is the model's own text", staff[0]["body"] == body)

    customer_view = tenant.thread(ticket_id, as_portal=True)
    check(
        "the customer's own read does not contain the draft",
        tenant.drafts_in(ticket_id, as_portal=True) == [],
    )
    check(
        "and it does not contain the draft's text",
        body not in [row["body"] for row in customer_view],
    )
    check(
        "but the customer's own message is still there, so this is a filter and not an empty list",
        FIRST_REPLY in [row["body"] for row in customer_view],
    )
    print(f"  thread as staff: {len(tenant.thread(ticket_id))} rows, one of them the draft.")
    print(f"  thread as the customer: {len(customer_view)} rows, none of them the draft.")
    return row


def asking_again_is_regenerating(tenant: Tenant, ticket_id: str, row: dict[str, Any]) -> None:
    section("3. §41's regenerate: asking again is a second draft, and the trail says which")

    before = tenant.audit(ticket_id)
    regenerated_before = [entry for entry in before if entry["action"] == "ai_response_regenerated"]
    check("no regeneration has been recorded yet", regenerated_before == [])

    again = tenant.suggest(ticket_id)
    print(f"  queued: {again['operation']}  status={again['status']}")
    check("a second row was queued rather than the first one served", again["id"] != row["id"])
    check("and it is a fresh one", again["status"] in {"pending", "processing", "completed"})

    # **The verb is in the trail and nowhere else.** Both requests answered `202` with a
    # `pending` row and both produce a completed draft, so nothing a client reads tells them
    # apart -- deliberately, because from the caller's point of view asking again *is*
    # regenerating. `audit_logs` is where §41's second verb is kept distinct from its first.
    regenerated = [
        entry for entry in tenant.audit(ticket_id) if entry["action"] == "ai_response_regenerated"
    ]
    check(
        "the second ask is recorded as a regeneration", len(regenerated) == 1, str(len(regenerated))
    )
    if regenerated:
        check(
            "and it names the operation, not just the ticket",
            regenerated[0]["extra_data"].get("operations") == ["suggest_response"],
            str(regenerated[0]["extra_data"]),
        )
    requested = [
        entry
        for entry in tenant.audit(ticket_id)
        if entry["action"] == "ai_analysis_requested"
        and "suggest_response" in entry["extra_data"].get("operations", [])
    ]
    check("the first ask is still recorded as a request", len(requested) == 1, str(len(requested)))

    settled = wait_until_settled(tenant, ticket_id, "suggest_response")
    if settled is None or settled["status"] != "completed":
        check("the regenerated draft completed", False, str(settled))
        return
    check("the regenerated draft completed", True)
    second = (settled["result"] or {}).get("body", "")
    print(f"  draft: {second!r}")
    # §6 keeps the model's earlier answers rather than overwriting them, and a regeneration is
    # a second draft rather than a replacement -- both are in the thread.
    check("both drafts are in the thread", len(tenant.drafts_in(ticket_id)) == 2)
    print("  the superseded draft is still there -- §6 keeps it for comparison, and the read")
    print("  route serves the newest row per operation, which is why only one shows above.")


def a_person_sends_one(tenant: Tenant, ticket_id: str) -> None:
    section("4. §41's accept: a person sends it, and the draft stays behind")

    drafts = tenant.drafts_in(ticket_id)
    if not drafts:
        check("there is a draft to accept", False)
        return
    # The thread is oldest first, so the last draft is the one the regeneration just produced.
    draft = drafts[-1]
    print(f"  accepting draft {draft['id']}")

    before = tenant.usage()
    print(f"  before: {spend(before)}")

    response = tenant.accept(ticket_id, str(draft["id"]), SENT_BODY)
    check("the acceptance answered 201", response.status_code == 201, response.text)
    if response.status_code != 201:
        return
    sent = response.json()
    check("it is a public message", sent["is_internal"] is False)
    check(
        "and it is the agent's own reply, not the model's",
        sent["sender_type"] == "agent",
        sent["sender_type"],
    )
    check("carrying the text the person wrote", sent["body"] == SENT_BODY)

    # §41's accept calls no model: the text was written by a person and the model was paid for
    # when the draft was asked for. The evidence is the bill, not an inspection of the imports.
    after = tenant.usage()
    print(f"  after:  {spend(after)}")
    check("the acceptance made no model call", after["calls"] == before["calls"])
    check("and spent nothing at all", after["cost_usd"] == before["cost_usd"])

    # **The message and the draft are two rows.** Accepting re-authors the text; it does not
    # promote the draft, which is what keeps `messages` append-only and Phase V's freshness
    # watermark sound.
    still_there = [row for row in tenant.drafts_in(ticket_id) if row["id"] == draft["id"]]
    check("the draft row is still in the thread", len(still_there) == 1)
    if still_there:
        check(
            "and it is unchanged -- same body, still internal",
            still_there[0]["body"] == draft["body"] and still_there[0]["is_internal"] is True,
        )

    customer_view = tenant.thread(ticket_id, as_portal=True)
    check("the customer now has the reply", SENT_BODY in [row["body"] for row in customer_view])
    check("and still no draft", tenant.drafts_in(ticket_id, as_portal=True) == [])
    check(
        "and still not the model's text",
        all(row["body"] != draft["body"] for row in customer_view),
        "the draft's body reached the customer",
    )

    # §34's before/after is where §41's *edit* is recorded -- there is no edit route, because
    # there is nothing to edit: the client sends the text it wants sent, and the row compares it
    # to what the model wrote.
    accepted = [
        entry for entry in tenant.audit(ticket_id) if entry["action"] == "ai_response_accepted"
    ]
    check("the acceptance is in the audit trail", len(accepted) == 1, str(len(accepted)))
    if accepted:
        extra = accepted[0]["extra_data"]
        check("it records what the model offered", extra.get("before") == draft["body"])
        check("and what actually went out", extra.get("after") == SENT_BODY)
        check("and that the agent edited it", extra.get("edited") is True)
        check("and which draft it came from", extra.get("draft_id") == draft["id"])


def a_stranger_cannot_ask(tenant: Tenant, ticket_id: str) -> None:
    section("5. §53: the same ticket id, asked by another tenant")

    stranger = Tenant("Bystander")
    response = stranger.post(f"/tickets/{ticket_id}/ai/suggest-response")

    # A 404 and not a 403, for the reason ADR-009 gives: a tenant must not be able to size
    # another's desk by watching which ids are refused. A foreign ticket and a nonexistent one
    # are the same answer.
    check("the stranger's request is a 404", response.status_code == 404, f"{response.status_code}")
    check(
        "and it names no ticket",
        response.json()["error"]["code"] == "TICKET_NOT_FOUND",
        response.text,
    )
    check("the owner still reads their own drafts", tenant.drafts_in(ticket_id) != [])


def main() -> None:
    # The section headings carry §, and a redirected stdout on Windows defaults to the locale
    # encoding — cp1252, which has no §. It round-trips as a replacement character, so the
    # transcript this script exists to produce arrives unreadable. Asking for UTF-8 explicitly
    # is one line and is the difference between a record and a mess of question marks.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = get_settings()
    if not settings.AI_API_KEY:
        print(
            "AI_API_KEY is not set, so there is nothing live to walk through.\n\n"
            "Add a key to .env and run again -- its shape follows AI_PROVIDER:\n"
            "  AI_PROVIDER=anthropic   AI_MODEL=claude-sonnet-5        AI_API_KEY=sk-ant-...\n"
            "  AI_PROVIDER=groq        AI_MODEL=openai/gpt-oss-120b   AI_API_KEY=gsk_...\n\n"
            "This is a legal configuration, not a failure: the provider module refuses at the\n"
            "point of use with 'AI_API_KEY is not configured', and the rest of the suite runs\n"
            "without one."
        )
        sys.exit(0)

    print(f"provider={settings.AI_PROVIDER} model={settings.AI_MODEL}")

    tenant = Tenant("Phase W")
    ticket_id, baseline = a_ticket_with_a_conversation(tenant)

    row = the_worker_drafts_it(tenant, ticket_id, baseline)
    if row is not None:
        asking_again_is_regenerating(tenant, ticket_id, row)
        a_person_sends_one(tenant, ticket_id)
    a_stranger_cannot_ask(tenant, ticket_id)

    print(f"\n{_passed} passed, {_failed} failed")

    print(
        "\nFour things this script could not do or show for itself.\n"
        "\n"
        "1. The notification that is *not* sent. §18's alert fires for a run that changed a\n"
        "   ticket column, and a draft changes none -- so `notify_analysis_completed` is never\n"
        "   reached, for §20's reason: the person who asked for the draft is the person about to\n"
        "   read it. There is no response surface for a message that was not sent, and an absent\n"
        "   notification looks exactly like a notification with nobody to send to. Read it in\n"
        "   psql instead:\n"
        f"     docker compose exec postgres psql -U supportflow -c \\\n"
        f'       "SELECT notification_type, user_id FROM notifications\\\n'
        f"        WHERE ticket_id = '{ticket_id}'\"\n"
        "\n"
        "2. §41's edit in the other direction. Section 4 edits deliberately, so the audit row's\n"
        "   `edited` is `True` and `before` differs from `after`. The faithful case -- an agent\n"
        "   sending the model's text unchanged, with `edited: false` and the two bodies equal --\n"
        "   is a different request and is asserted in `tests/integration/test_ai_draft_\n"
        "   acceptance.py`, which controls both bodies. Showing it here would mean sending the\n"
        "   customer a second, redundant reply.\n"
        "\n"
        '3. §21\'s "relevant knowledge" step. The workflow diagram puts a retrieval step between\n'
        "   the ticket context and the model, and it is §22's -- Phase X. The prompt this script\n"
        "   exercised has two blocks, the ticket's words and the conversation's, and the third is\n"
        "   added there rather than stubbed here. See ADR-008's precedent and ADR-031.\n"
        "\n"
        "4. A draft of a ticket nobody has replied to. §20 refuses an empty conversation and\n"
        "   §21 must not, because a freshly raised ticket is exactly what a draft reply is most\n"
        "   useful for -- the material is the description, which `create_ticket` puts on the\n"
        "   ticket and never on a message. It is covered in the integration suite, where the\n"
        "   scripted provider makes it deterministic; here every ticket has a conversation.\n"
        "\n"
        f"Left behind: two organizations ({tenant.name}, and one named Bystander), a customer,\n"
        "a portal login, a ticket, the messages including both drafts and the reply that went\n"
        "out, two ai_analyses rows, the audit rows, and the ledger rows the calls wrote.\n"
        "Nothing here deletes anything."
    )
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError as exc:
        print(
            f"\nCould not reach {exc.request.url}. This walkthrough needs the API:\n"
            "  .venv/Scripts/python.exe -m uvicorn app.main:app "
            "--loop app.core.event_loop:loop_factory --port 8000",
            file=sys.stderr,
        )
        sys.exit(2)
