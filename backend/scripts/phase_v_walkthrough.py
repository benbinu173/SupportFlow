"""Phase V end to end: a conversation summarized, and a second ask that costs nothing.

§20 is four sentences. The suites prove each of them separately — a summary row goes
`pending` to `completed` with no confidence, it writes nothing onto the ticket, a second ask
with nothing new returns the stored row and writes one `was_cached` ledger row, one new
message makes it stale. What no test in the phase can show is the thing the phase is *for*:

* **That a real model summarizes a real conversation, today, through the worker, with the key
  in `.env`.** Every other test in Phase V runs against `FakeProvider`, which is exactly right
  and exactly why something has to prove the live path is not well-tested fiction. The script
  posts two messages over HTTP and reads back what a worker process wrote.
* **That §20's fourth sentence saves real money.** *"Avoid regenerating after every tiny
  message if unnecessary"* is a claim about a call that did not happen, and the evidence is
  the **bill**: the script reads `/analytics/overview` before and after the second summarize
  and asserts `cached_calls` went up by one while `cost_usd` did not move at all. A cached
  call that quietly spent something would show up here and nowhere else.
* **That a summary and a classified ticket are different things.** Section 2 snapshots the
  ticket after its §18 analysis has settled and asserts every field is identical after the
  summary — §20 stores a summary, it does not apply one.

**Everything here is HTTP.** Unlike the Phase T script, this one never touches the database or
imports a service — Phase V is the phase that gave §20 a route, so a walkthrough that reached
around the route would be bypassing the thing it is walking through. That is also why the
worker must be running for section 2 to finish.

Four steps, from `backend/`:

    # 1. the datastores (Docker Desktop must already be running)
    docker compose up -d postgres redis

    # 2. the API
    .venv/Scripts/python.exe -m uvicorn app.main:app \\
        --loop app.core.event_loop:loop_factory --port 8000

    # 3. the worker, in a third terminal — and `-Q` matters
    .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker \\
        --loglevel=info --pool=solo -Q notifications,sla,ai

    # then
    .venv/Scripts/python.exe scripts/phase_v_walkthrough.py

`AI_API_KEY` must be set in `.env`, and it is a real spend — small, and nothing on Groq's
free tier. With the key empty the script prints what to do and exits `0`: an absent key is a
legal configuration and not a failure of this phase.

**The `-Q` is not decoration.** `analyze_ticket` — the task summarization rides on — is routed
to the `ai` queue, and a worker started without `-Q` consumes only the default one. The
symptom is distinctive here: the creation analysis settles, because it was queued by an
earlier run or not at all, and then section 2 times out with a `pending` summary and no error
anywhere.

**Section 1 waits for the ticket's own analysis before it snapshots.** §18 runs on ticket
creation, and section 2's claim is that *the summary* changed nothing — so the AI columns have
to be final before the baseline is taken. Without that wait the comparison would be measuring
the classification call landing, which is a different statement and a flaky one.

It **registers two organizations**, and §45 limits registration to five an hour per address,
so a third run inside the hour reports `429` rather than a failed assertion:

    docker compose exec redis redis-cli -n 0 --scan --pattern 'ratelimit:register:*'

And it leaves everything behind — the organizations, the customer, the ticket, the messages,
the `ai_analyses` rows, and the ledger rows. A spend record that can be tidied away is not a
spend record.
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
        `POST /auth/login`, the same two steps a real portal account takes. It is what makes
        the first message a *customer* message rather than an agent's — the route takes
        `sender_type` from the caller's role, so posting as the admin would quietly exercise
        only half of §20's eligibility rule.
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

    def summarize(self, ticket_id: str) -> dict[str, Any]:
        """POST §20's route and return the row it answered with."""
        response = self.post(f"/tickets/{ticket_id}/ai/summarize")
        assert response.status_code == 202, response.text
        return dict(response.json())

    def analyses(self, ticket_id: str) -> list[dict[str, Any]]:
        response = self.get(f"/tickets/{ticket_id}/ai/analyses")
        assert response.status_code == 200, response.text
        return list(response.json())

    def row_for(self, ticket_id: str, operation: str) -> dict[str, Any] | None:
        """The row the read route serves for `operation`, or `None` if there is none.

        `/ai/analyses` serves the newest row of each kind, one per operation, so this is the
        summary's current state and not a history of it. That is the right thing to poll: it
        is what the staff screen would be showing.
        """
        rows = [row for row in self.analyses(ticket_id) if row["operation"] == operation]
        return rows[0] if rows else None

    def summary_row(self, ticket_id: str) -> dict[str, Any] | None:
        return self.row_for(ticket_id, "summarize")

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


# ---------------------------------------------------------------------------
# The sections
# ---------------------------------------------------------------------------


def a_ticket_with_a_conversation(tenant: Tenant) -> tuple[str, dict[str, Any]]:
    section("1. §20 sentences 1-2: a conversation exists and nothing is summarized yet")

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
    print("  the note is staff-only and is still part of the conversation -- §20's decision.")

    check("no summary exists yet", tenant.summary_row(ticket_id) is None)

    queued = tenant.summarize(ticket_id)
    print(f"  queued: {queued['operation']}  status={queued['status']}")
    check("the route accepted the request", queued["operation"] == "summarize")
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
    return ticket_id, baseline


def the_worker_summarizes_it(
    tenant: Tenant, ticket_id: str, baseline: dict[str, Any]
) -> dict[str, Any] | None:
    section("2. §20 sentence 1: the worker summarizes it, in another process")

    started = time.monotonic()
    row = wait_until_settled(tenant, ticket_id, "summarize")
    elapsed = time.monotonic() - started
    print(f"  settled after {elapsed:.1f}s: status={row and row['status']}")

    if row is None or row["status"] != "completed":
        check("the summary completed", False, str(row))
        if row and row["status"] == "failed":
            print(f"        failed: {row['error_message']}")
        return None

    check("the summary completed", True)
    summary = (row["result"] or {}).get("summary", "")
    print(f"  summary: {summary!r}")
    check("it is a real sentence somebody could read", len(summary) > 40, str(len(summary)))
    # §20 asks for a summary rather than a judgement, and `ConversationSummary` has one field.
    check(
        "the row carries no confidence",
        row["confidence"] is None,
        repr(row["confidence"]),
    )
    check(
        "the call was billed for its tokens",
        bool(row["prompt_tokens"]) and bool(row["completion_tokens"]),
        f"{row['prompt_tokens']}/{row['completion_tokens']}",
    )

    # §20 stores a summary; it does not apply one. Every field the §18 analysis wrote is
    # compared against the snapshot taken after that analysis settled, so this is a statement
    # about the summary and not about the classification landing late.
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
    )
    changed = {
        field: (baseline[field], after[field])
        for field in watched
        if baseline[field] != after[field]
    }
    check("the summary changed nothing on the ticket", not changed, str(changed))
    print(f"  priority={after['priority']!r}  recommended={after['ai_recommended_priority']!r}")
    return row


def spend(usage: dict[str, Any]) -> str:
    """One `/analytics/overview` reading, in a line — the three numbers §20 turns on."""
    return f"calls={usage['calls']} cached={usage['cached_calls']} cost={usage['cost_usd']}"


def asking_again_costs_nothing(tenant: Tenant, ticket_id: str, row: dict[str, Any]) -> None:
    section("3. §20 sentence 4: asking again with nothing new costs nothing")

    before = tenant.usage()
    print(f"  before: {spend(before)}")

    again = tenant.summarize(ticket_id)

    # The same row, and this is what makes it the *stored* summary rather than a fresh one
    # that happens to agree — a regeneration would carry a new id.
    check("the same row came back", again["id"] == row["id"], f"{again['id']} vs {row['id']}")
    # `202` covers both branches, so the status is the discriminator: `pending` would mean a
    # second row was created and queued, and only the cache-hit branch returns `completed`.
    check(
        "and it came back completed, not queued again",
        again["status"] == "completed",
        again["status"],
    )

    # Nothing is waited for here: the ledger row for a cache hit is staged and committed inside
    # the request, because no provider is ever asked and there is no worker in the loop.
    after = tenant.usage()
    print(f"  after:  {spend(after)}")

    check(
        "the cached call is counted",
        after["cached_calls"] == before["cached_calls"] + 1,
        f"{before['cached_calls']} -> {after['cached_calls']}",
    )
    check(
        "the call count moved with it -- cached_calls is a subset, not a deduction",
        after["calls"] == before["calls"] + 1,
        f"{before['calls']} -> {after['calls']}",
    )
    # The whole point: the call that did not happen bought nothing, so the bill is unchanged.
    check(
        "and the bill did not move at all",
        after["cost_usd"] == before["cost_usd"],
        f"{before['cost_usd']} -> {after['cost_usd']}",
    )


def one_new_message_makes_it_stale(tenant: Tenant, ticket_id: str, row: dict[str, Any]) -> None:
    section("4. §20 sentence 3: one new message makes the summary stale")

    said = tenant.portal.post(
        f"/tickets/{ticket_id}/messages",
        json={"body": "Still failing -- here is the exact error: 503 Service Unavailable."},
    )
    said.raise_for_status()
    check("a new customer message was posted", True)

    queued = tenant.summarize(ticket_id)
    print(f"  queued: {queued['operation']}  status={queued['status']}")
    check("a new row was queued rather than the old one served", queued["id"] != row["id"])
    check("and it is a fresh one", queued["status"] in {"pending", "processing", "completed"})

    settled = wait_until_settled(tenant, ticket_id, "summarize")
    if settled is None or settled["status"] != "completed":
        check("the regenerated summary completed", False, str(settled))
        return
    check("the regenerated summary completed", True)
    summary = (settled["result"] or {}).get("summary", "")
    print(f"  summary: {summary!r}")
    check("it is a different summary", summary != (row["result"] or {}).get("summary", ""))
    # §6 keeps the model's earlier answers, and §20's "store the latest" is the read route
    # serving the newest rather than a column that overwrote the first.
    print(
        "  the superseded summary is still in the table -- §6 keeps it for comparison.\n"
        "  The read route serves the newest per operation, which is why it is not listed here."
    )


def a_stranger_cannot_summarize(tenant: Tenant, ticket_id: str) -> None:
    section("5. §53: the same ticket id, asked by another tenant")

    stranger = Tenant("Bystander")
    response = stranger.post(f"/tickets/{ticket_id}/ai/summarize")

    # A 404 and not a 403, for the reason ADR-009 gives: a tenant must not be able to size
    # another's desk by watching which ids are refused. A foreign ticket and a nonexistent one
    # are the same answer.
    check("the stranger's request is a 404", response.status_code == 404, f"{response.status_code}")
    check(
        "and it names no ticket",
        response.json()["error"]["code"] == "TICKET_NOT_FOUND",
        response.text,
    )
    check("the owner still reads their own summary", tenant.summary_row(ticket_id) is not None)


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

    tenant = Tenant("Phase V")
    ticket_id, baseline = a_ticket_with_a_conversation(tenant)

    row = the_worker_summarizes_it(tenant, ticket_id, baseline)
    if row is not None:
        asking_again_costs_nothing(tenant, ticket_id, row)
        one_new_message_makes_it_stale(tenant, ticket_id, row)
    a_stranger_cannot_summarize(tenant, ticket_id)

    print(f"\n{_passed} passed, {_failed} failed")

    print(
        "\nThree things this script could not do or show for itself.\n"
        "\n"
        "1. The notification that is *not* sent. §18's alert fires for a run that changed a\n"
        "   ticket column, and a summary changes none -- so `notify_analysis_completed` is\n"
        "   never reached. There is no response surface for a message that was not sent, and\n"
        "   an absent notification looks exactly like a notification with nobody to send to.\n"
        "   Read it in psql instead:\n"
        f"     docker compose exec postgres psql -U supportflow -c \\\n"
        f'       "SELECT notification_type, user_id FROM notifications\\\n'
        f"        WHERE ticket_id = '{ticket_id}'\"\n"
        "\n"
        "2. §20's over-context behaviour. A conversation longer than the model's window is\n"
        "   refused by the provider and the row is marked failed, with the reason on the\n"
        "   ticket (`AIAnalysis.error_message`). Producing one honestly is not something a\n"
        "   walkthrough can do -- it would mean pasting kilobytes of filler -- so it is\n"
        "   recorded as a limitation rather than exercised. See ADR-030, Decision 10.\n"
        "\n"
        "3. The freshness comparison itself. Section 3's claim is that a *database* clock\n"
        "   comparison found the conversation unmoved, so the script cannot manufacture a\n"
        "   message and a summary in the same instant to test the boundary. The deterministic\n"
        "   proof is `tests/integration/test_ai_summary.py`, which scripts the provider and\n"
        "   controls both timestamps.\n"
        "\n"
        f"Left behind: two organizations ({tenant.name}, and one named Bystander), a customer,\n"
        "a portal login, a ticket, three messages, three ai_analyses rows, and the ledger rows\n"
        "the calls wrote. Nothing here deletes anything."
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
