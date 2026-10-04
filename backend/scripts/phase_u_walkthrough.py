"""Phase U end to end: a ticket raised, a worker that runs it, and a ticket that changed.

§18 writes the analysis out in nine steps. The suites prove each of them separately — the
route returns before any model is asked, the worker flips two rows from `pending` to
`completed`, the ticket gains six fields and not a seventh, a redelivery does nothing. What
no test in the phase can show is the thing the phase is *for*:

* **That a real model classifies a real ticket, today, through the worker, with the key in
  `.env`.** Every other test in Phase U runs against `FakeProvider`, which is exactly right
  and exactly why something has to prove the live path is not well-tested fiction. The
  script raises a ticket over HTTP and then reads back what a worker process wrote.
* **That `tickets.priority` is untouched while `ai_recommended_priority` is filled.** §6's
  separation is the phase's central claim and it is a claim about two columns on one row.
  `tests/integration/test_ai_analysis.py` proves it deterministically against a scripted
  provider; this shows it on a row a real model answered.
* **That §28's numbers moved because somebody else's process spent them.**
  `/analytics/overview` is read from a *different* process than the one that made the calls,
  which is a stronger statement than an in-process read: the ledger is in the database, not
  in a session that still holds it.

**Everything here is HTTP.** Unlike the Phase T script, this one never touches the database
or imports a service — Phase U is the phase that gave the flow a route, so a walkthrough
that reached around the route would be bypassing the thing it is walking through. That is
also why the worker must be running for section 2 to finish.

Three steps, from `backend/`:

    # 1. the datastores (Docker Desktop must already be running)
    docker compose up -d postgres redis

    # 2. the API
    .venv/Scripts/python.exe -m uvicorn app.main:app \\
        --loop app.core.event_loop:loop_factory --port 8000

    # 3. the worker, in a third terminal — and `-Q` matters
    .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker \\
        --loglevel=info --pool=solo -Q notifications,sla,ai

    # then
    .venv/Scripts/python.exe scripts/phase_u_walkthrough.py

`AI_API_KEY` must be set in `.env`, and it is a real spend — small, and nothing on Groq's
free tier. With the key empty the script prints what to do and exits `0`: an absent key is a
legal configuration and not a failure of this phase.

**The `-Q` is not decoration.** `analyze_ticket` is routed to the `ai` queue, and a worker
started without `-Q` consumes only the default one — so the ticket is raised, the rows stay
`pending`, and section 2 times out. That failure looks like a bug in the analysis and is not.

It **registers two organizations**, and §45 limits registration to five an hour per address,
so a third run inside the hour reports `429` rather than a failed assertion:

    docker compose exec redis redis-cli -n 0 --scan --pattern 'ratelimit:register:*'

And it leaves everything behind — the organizations, the customer, the ticket, the
`ai_analyses` rows, and the ledger rows. A spend record that can be tidied away is not a
spend record.
"""

# ruff: noqa: T201

import sys
import time
import uuid
from typing import Any

import httpx

from app.core.config import get_settings
from app.models.enums import Sentiment, TicketPriority

BASE = "http://localhost:8000/api/v1"
PASSWORD = "correct-horse-battery-staple"

#: How long section 2 will wait for the worker. Generous, because it is waiting on a real
#: provider over the network; a timeout here means the worker is not running or is not
#: listening on the `ai` queue, and the closing note says so.
WORKER_TIMEOUT = 120.0

#: The two statuses that mean the row is finished, either way. A row in `pending` or
#: `processing` is one the worker has not settled, and the difference between them is *when*
#: the worker picked it up rather than whether the analysis is done.
TERMINAL = {"completed", "failed"}

#: The ticket is created at `low` on purpose, so that `tickets.priority` has a value the
#: model's recommendation is unlikely to match. If the worker wrote `priority`, the two
#: columns would be equal after the run below; the assertion in section 3 is what notices.
CREATED_AT_PRIORITY = "low"

SUBJECT = "Duplicate charge on order 88213"
DESCRIPTION = (
    "I was charged twice for order 88213 on the 14th of this month and nobody has replied "
    "to my last three emails. I am going to dispute the charge with my bank."
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


class Tenant:
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
        self.token = str(response.json()["access_token"])

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.get(f"{BASE}{path}", headers=self.headers, timeout=30.0, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.post(f"{BASE}{path}", headers=self.headers, timeout=60.0, **kwargs)

    def raise_ticket(self) -> dict[str, Any]:
        """A customer and a ticket carrying a real complaint. Returns the created ticket.

        The response is the whole of §18's first two steps: the ticket exists, and the
        analysis is a message to a broker rather than a call made while this request was
        open. Section 1 asserts that from the body, which carries no analysis at all.
        """
        customer = self.post(
            "/customers",
            json={"name": "Ada Lovelace", "email": f"ada-{uuid.uuid4().hex[:8]}@customer.example"},
        )
        customer.raise_for_status()

        ticket = self.post(
            "/tickets",
            json={
                "subject": SUBJECT,
                "description": DESCRIPTION,
                "customer_id": customer.json()["id"],
                "priority": CREATED_AT_PRIORITY,
            },
        )
        ticket.raise_for_status()
        return dict(ticket.json())

    def analyses(self, ticket_id: str) -> list[dict[str, Any]]:
        """The ticket's latest analysis per operation, as the read route serves it."""
        response = self.get(f"/tickets/{ticket_id}/ai/analyses")
        assert response.status_code == 200, response.text
        return list(response.json())

    def overview(self) -> dict[str, Any]:
        response = self.get("/analytics/overview")
        assert response.status_code == 200, response.text
        return dict(response.json())


def wait_until_settled(
    tenant: Tenant, ticket_id: str, *, timeout: float = WORKER_TIMEOUT
) -> list[dict[str, Any]]:
    """Poll the read route until every row it serves is terminal, or the timeout expires.

    Polling the API rather than watching the worker's log, because that is what a client
    does: the route exists so a browser can show a queued analysis instead of an empty list,
    and this is that route being used for its purpose.
    """
    deadline = time.monotonic() + timeout
    rows: list[dict[str, Any]] = []
    while True:
        rows = tenant.analyses(ticket_id)
        if rows and all(row["status"] in TERMINAL for row in rows):
            return rows
        if time.monotonic() >= deadline:
            return rows
        time.sleep(2.0)


# ---------------------------------------------------------------------------
# The sections
# ---------------------------------------------------------------------------


def raising_a_ticket_queues_without_waiting(tenant: Tenant) -> dict[str, Any]:
    section("1. §18 steps 1-2: the ticket is raised, and the analysis is a message")

    ticket = tenant.raise_ticket()
    ticket_id = str(ticket["id"])

    check("the ticket was created", ticket["status"] == "open", str(ticket))
    # §16's "the API should not wait unnecessarily for the LLM", read from the body: an
    # analysis that had already happened would have filled these six fields, and an analysis
    # that was happening *during* the request would have held the response open.
    concluded = ("category", "subcategory", "sentiment", "ai_recommended_priority")
    check(
        "nothing has been concluded yet",
        all(ticket[field] is None for field in concluded),
        str({field: ticket[field] for field in concluded}),
    )

    rows = tenant.analyses(ticket_id)
    settings = get_settings()
    print(f"  queued: {[row['operation'] for row in rows]}")
    print(f"  their status right now: {sorted({row['status'] for row in rows})}")

    check("two rows exist, one per operation", len(rows) == 2, str(len(rows)))
    # The status is **not** asserted here, and that is the honest reading rather than a
    # weakened check. A worker is running while this script talks to the API, and on a fast
    # machine both rows are already `completed` by the time this read returns — so a
    # `{"pending"}` assertion is a coin flip, and one that passes on a slow day and fails on
    # a fast one tests the machine rather than the code. §16's claim has a race-free half,
    # and it is the one asserted above: the *creation response* carried none of the six AI
    # fields, so the request did not wait for a model. What is left here is that the rows
    # exist, one per operation, before anything asked them to run.
    check(
        "they name the provider and model that will be asked",
        {row["provider"] for row in rows} == {settings.AI_PROVIDER}
        and {row["model"] for row in rows} == {settings.AI_MODEL},
        str({(row["provider"], row["model"]) for row in rows}),
    )
    # §41's "make it clear this is AI-generated", as data rather than as a label a UI keeps
    # after the row behind it changed — and §4's "AI output is untrusted data": a row that has
    # not finished has nothing to trust. Conditional rather than a set comparison, because a
    # `result` on a completed row is a JSON object and a dict is not hashable — which is how
    # this line first failed, in the run where the worker won the race.
    unfinished = [row for row in rows if row["status"] not in TERMINAL]
    check(
        "no unfinished row carries a result",
        all(row["result"] is None for row in unfinished),
        f"{len(unfinished)} of {len(rows)} still running",
    )

    return ticket


def the_worker_runs_it(tenant: Tenant, ticket_id: str) -> dict[str, Any]:
    section("2. §18 steps 3-7: the worker classifies it, in another process")

    started = time.monotonic()
    rows = wait_until_settled(tenant, ticket_id)
    elapsed = time.monotonic() - started

    statuses = {row["operation"]: row["status"] for row in rows}
    print(f"  settled after {elapsed:.1f}s: {statuses}")

    check(
        "both operations finished",
        set(statuses.values()) == {"completed"},
        str(statuses),
    )
    # A row that failed is a real outcome and section 4 reads its reason, but it is not the
    # outcome this walkthrough is walking through, so it stops here rather than asserting a
    # half-filled ticket below.
    if set(statuses.values()) != {"completed"}:
        for row in rows:
            if row["status"] == "failed":
                print(f"        {row['operation']} failed: {row['error_message']}")
        return {}

    # Read the ticket back from the route a client reads, in a process that did not write it.
    ticket = dict(tenant.get(f"/tickets/{ticket_id}").json())
    for field in (
        "category",
        "subcategory",
        "sentiment",
        "sentiment_confidence",
        "ai_recommended_priority",
        "ai_classification_confidence",
    ):
        print(f"  {field}: {ticket[field]!r}")
    return ticket


def the_recommendation_is_not_the_priority(ticket: dict[str, Any]) -> None:
    section("3. §6: the model's band lands beside the priority, never on it")

    if not ticket:
        check("the ticket was read back", False, "section 2 did not complete")
        return

    check(
        "the descriptive fields were filled",
        bool(ticket["category"])
        and bool(ticket["subcategory"])
        and ticket["sentiment"] is not None,
        str({k: ticket[k] for k in ("category", "subcategory", "sentiment")}),
    )
    check("the sentiment is one of the three §19 allows", ticket["sentiment"] in set(Sentiment))
    check(
        "the recommended band is one of the four §51 allows",
        ticket["ai_recommended_priority"] in set(TicketPriority),
        repr(ticket["ai_recommended_priority"]),
    )
    for field in ("sentiment_confidence", "ai_classification_confidence"):
        value = ticket[field]
        check(
            f"{field} is a probability",
            isinstance(value, float) and 0.0 <= value <= 1.0,
            repr(value),
        )

    # The claim the phase is built on. `ticket_service.change_priority` is the only writer of
    # `tickets.priority`, and a worker that applied its own suggestion would make §6's
    # comparison between the two columns vacuous.
    check(
        "the priority is the one the customer's ticket was created with",
        ticket["priority"] == CREATED_AT_PRIORITY,
        f"expected {CREATED_AT_PRIORITY!r}, got {ticket['priority']!r}",
    )
    agreed = ticket["priority"] == ticket["ai_recommended_priority"]
    print(
        f"  priority={ticket['priority']!r} vs recommended={ticket['ai_recommended_priority']!r}"
        f" -- {'they happen to agree' if agreed else 'the model recommended something else'}"
    )
    # Stated out loud rather than left for a reader to notice: an agreement is not evidence,
    # and this run cannot tell the two columns apart when it happens. The deterministic proof
    # is `test_the_recommendation_never_becomes_the_priority`, which scripts the provider.
    if agreed:
        print("  (so this run is not evidence for §6 -- the integration test is)")


def the_read_model_reports_the_rows(tenant: Tenant, ticket_id: str, ticket: dict[str, Any]) -> None:
    section("4. §41 and §28: what the API says about the row the model wrote")

    if not ticket:
        check("there are rows to read", False, "section 2 did not complete")
        return

    rows = tenant.analyses(ticket_id)
    for row in rows:
        print(
            f"  {row['operation']:<10}{row['status']:<11}"
            f"in={row['prompt_tokens']} out={row['completion_tokens']}"
            f" {row['latency_ms']}ms  conf={row['confidence']}"
        )

    by_operation = {row["operation"]: row for row in rows}
    classified = by_operation["classify"]["result"]
    analyzed = by_operation["sentiment"]["result"]

    # The payload on the row and the columns on the ticket are written from the same validated
    # model, so a disagreement here is a mapping that drifted.
    check(
        "the row's payload is the ticket's own classification",
        classified
        == {
            "category": ticket["category"],
            "subcategory": ticket["subcategory"],
            "priority": ticket["ai_recommended_priority"],
            "confidence": ticket["ai_classification_confidence"],
        },
        f"{classified} vs the ticket",
    )
    check(
        "the sentiment row agrees with the ticket too",
        analyzed
        == {
            "sentiment": ticket["sentiment"],
            "confidence": ticket["sentiment_confidence"],
        },
        f"{analyzed} vs the ticket",
    )
    check(
        "every successful call was billed for its tokens",
        all(row["prompt_tokens"] > 0 and row["completion_tokens"] > 0 for row in rows),
        str([(row["prompt_tokens"], row["completion_tokens"]) for row in rows]),
    )
    check("every row carries a latency", all(row["latency_ms"] >= 0 for row in rows))
    check(
        "every row says when it finished",
        all(row["completed_at"] is not None for row in rows),
        str([row["completed_at"] for row in rows]),
    )
    # The one field on the response a customer must never see, and it is `None` because
    # nothing failed -- which is the state §18 describes rather than a redaction.
    check("no row carries an error", {row["error_message"] for row in rows} == {None})


def the_dashboard_moved(tenant: Tenant, ticket_id: str) -> None:
    section("5. §28: the ledger grew, read in a third process")

    body = tenant.overview()
    usage = body["ai_usage"]
    print(f"  {usage}")

    operations = {entry["operation"] for entry in usage["by_operation"]}
    check("the dashboard counts the ticket's calls", usage["calls"] >= 2, str(usage["calls"]))
    check("and none of them failed", usage["failed_calls"] == 0, str(usage["failed_calls"]))
    check("it reports the prompt tokens", usage["prompt_tokens"] > 0, str(usage["prompt_tokens"]))
    check(
        "it reports the completion tokens",
        usage["completion_tokens"] > 0,
        str(usage["completion_tokens"]),
    )
    check("it reports a spend", float(usage["cost_usd"]) > 0, str(usage["cost_usd"]))
    check(
        "the breakdown names the two operations that ran",
        operations == {"classify", "sentiment"},
        str(sorted(operations)),
    )
    check(
        "the call count is the breakdown's sum",
        sum(entry["calls"] for entry in usage["by_operation"]) == usage["calls"],
    )
    check(
        "the ticket is the one that spent it",
        any(row["ticket_id"] == ticket_id for row in tenant.analyses(ticket_id)),
    )


def a_stranger_sees_nothing(tenant: Tenant, ticket_id: str) -> None:
    section("6. §53: the same ticket id, asked by another tenant")

    stranger = Tenant("Bystander")
    read = stranger.get(f"/tickets/{ticket_id}/ai/analyses")
    asked = stranger.post(f"/tickets/{ticket_id}/ai/analyze")

    # A 404 and not a 403, for the reason ADR-009 gives: a tenant must not be able to size
    # another's desk by watching which ids are refused. A foreign ticket and a nonexistent
    # one are the same answer, on both verbs.
    for verb, response in (("read", read), ("ask", asked)):
        check(
            f"the {verb} is a 404",
            response.status_code == 404,
            f"{response.status_code} {response.text}",
        )
        check(
            f"and the {verb} says the ticket was not found",
            response.json()["error"]["code"] == "TICKET_NOT_FOUND",
            response.text,
        )
    # The owner still sees their rows, which is what makes this a statement about isolation
    # rather than merely about a 404.
    check("the owner still reads their own analysis", len(tenant.analyses(ticket_id)) == 2)


def asking_again_regenerates(tenant: Tenant, ticket_id: str) -> None:
    section("7. §6: a second analysis adds rows rather than overwriting them")

    before = {row["id"] for row in tenant.analyses(ticket_id)}
    asked = tenant.post(f"/tickets/{ticket_id}/ai/analyze")
    check("the button answers 202", asked.status_code == 202, asked.text)

    rows = wait_until_settled(tenant, ticket_id)
    after = {row["id"] for row in rows}

    check(
        "the rows served now are new ones",
        not (before & after),
        str(sorted(before & after)),
    )
    check(
        "and they finished too",
        {row["status"] for row in rows} == {"completed"},
        str(sorted({row["status"] for row in rows})),
    )
    print(
        "  the superseded rows are still in the table -- §6 keeps them for comparison.\n"
        "  The read route serves the newest per operation, which is why they are not listed here."
    )


def main() -> None:
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

    tenant = Tenant("Phase U")
    ticket = raising_a_ticket_queues_without_waiting(tenant)
    ticket_id = str(ticket["id"])

    analyzed = the_worker_runs_it(tenant, ticket_id)
    the_recommendation_is_not_the_priority(analyzed)
    if analyzed:
        the_read_model_reports_the_rows(tenant, ticket_id, analyzed)
        the_dashboard_moved(tenant, ticket_id)
        asking_again_regenerates(tenant, ticket_id)
    a_stranger_sees_nothing(tenant, ticket_id)

    print(f"\n{_passed} passed, {_failed} failed")

    print(
        "\nTwo things this script could not do or show for itself.\n"
        "\n"
        "1. The timeline entry, which §25 announces and no route serves. It is written by\n"
        "   `ai_analysis_service._record_completion` with `actor_user_id` NULL -- the system\n"
        "   acted, not a person -- and the sockets are the only place it surfaces. Read it in\n"
        "   psql instead:\n"
        f"     docker compose exec postgres psql -U supportflow -c \\\n"
        f'       "SELECT event_type, actor_user_id, extra_data FROM ticket_events\\\n'
        f"        WHERE ticket_id = '{ticket_id}'\"\n"
        "\n"
        "2. §6's separation, deterministically. Section 3 asserts `priority` is untouched, and\n"
        "   a run where the model happened to recommend the band the ticket already had cannot\n"
        "   tell the two columns apart. `tests/integration/test_ai_analysis.py` scripts the\n"
        "   provider with a band that differs and pins it there; the deliberate break that\n"
        "   makes the worker write `tickets.priority` fails exactly that test and no other.\n"
        "\n"
        f"Left behind: two organizations ({tenant.name}, and one named Bystander), a customer,\n"
        "a ticket, four ai_analyses rows, and the ledger rows their calls wrote. Nothing here\n"
        "deletes anything."
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
