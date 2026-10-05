"""Phase X end to end: a document nobody wrote for a model, and the answer that cites it.

§22, §23, and §24 are a pipeline, a question, and a refusal — and each is proven separately in
the suite against a scripted embedding provider, so that the ranking is a decision rather than an
accident. What no test in the phase can show is the thing the phase is *for*:

* **That real vectors made from real documents rank a real question, today, through a real
  worker.** Every other test in Phase X scripts its vectors, which is exactly right and exactly
  why something has to prove the live path is not well-tested fiction. This script uploads a real
  PDF and a real markdown file, waits for a worker process to extract, chunk, and embed them, and
  then asks a question whose answer exists in one document and not the others.
* **That §24's refusal is the absence of a call, not the absence of a good answer.** Section 3
  asks something the documents do not cover, and the response is §24's own sentence with nothing
  cited. The ledger shows why: no model was asked.
* **That the citations are the server's.** Every id in `sources` resolves to a stored chunk, and
  the first one is the passage retrieval considered nearest — not the first one a model happened
  to name. That distinction is invisible when the model is honest and load-bearing when it is not.
* **That §21's suggested replies read the same retrieval.** A draft for a ticket about refunds is
  written from the refund policy, and the evidence that the retrieval *ran* is structural: the
  embedding call it makes is attributed to the ticket, so it appears in `/analytics/overview`
  where the ingestion calls — which have no ticket — do not.

**Everything here is HTTP.** Like Phase W's, this script never touches the database and never
imports a service: Phase X is the phase that gave §22 its routes, so a walkthrough that reached
around the route would be bypassing the thing it is walking through. The one import is
`get_settings`, for the model names and the similarity threshold the assertions compare against.

Five steps, from `backend/`:

    # 1. the datastores (Docker Desktop must already be running)
    docker compose up -d postgres redis

    # 2. the API
    .venv/Scripts/python.exe -m uvicorn app.main:app \\
        --loop app.core.event_loop:loop_factory --port 8000

    # 3. the worker, in a third terminal — and `-Q` matters
    .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker \\
        --loglevel=info --pool=solo -Q notifications,sla,ai,knowledge

    # then
    .venv/Scripts/python.exe scripts/phase_x_walkthrough.py

**Two keys, from two vendors.** `EMBEDDING_API_KEY` is required for sections 1-3 and 5, and
`AI_API_KEY` for section 4's draft. Neither Anthropic nor Groq publishes an embedding model, so
the embedding key is an OpenAI one; the generation key may be either vendor, or a free Groq one.

**A missing `EMBEDDING_API_KEY` ends the run early, and that is not a false alarm.**
`EMBEDDING_PROVIDER=fake` would keep the pipeline, the storage, the worker, the ledger, and every
route real — but a hash-derived vector has no relationship to meaning, so the *ranking* this
script exists to show would be demonstration-free, and an answer that came out right would have
come out right by luck. It exits `0` with that sentence rather than calling a hash a demo. A
missing `AI_API_KEY` is different: it skips section 4 and runs the rest, because §22 and §23 do
not need a generation provider to be true.

**The `-Q` is not decoration.** `ingest_document` is routed to the `knowledge` queue, and a worker
started without `-Q knowledge` consumes only the default one. The symptom is distinctive here: a
document is created and readable at `201`, and then sits `pending` forever while section 1 times
out — with no error anywhere, because from the application's side a task was queued successfully.

It **registers two organizations**, and §45 limits registration to five an hour per address, so a
third run inside the hour reports `429` rather than a failed assertion:

    docker compose exec redis redis-cli -n 0 --scan --pattern 'ratelimit:register:*'

And it leaves everything behind — the organizations, the documents and their chunks, the vectors,
the ticket, the draft, the audit rows, and the ledger rows. A spend record that can be tidied away
is not a spend record.
"""

# ruff: noqa: T201

import io
import sys
import time
import uuid
from collections.abc import Sequence
from typing import Any

import httpx
from pypdf import PdfReader

from app.core.config import get_settings

BASE = "http://localhost:8000/api/v1"
PASSWORD = "correct-horse-battery-staple"

#: How long a section will wait for the worker. Longer than Phase W's, because this waits on two
#: queues and two vendors: the ingestion that embeds three documents, and the draft. A timeout
#: means the worker is not running or is not listening on `knowledge`, and the closing note says
#: so.
WORKER_TIMEOUT = 180.0

#: The two statuses that mean a document is finished, either way.
TERMINAL = {"completed", "failed"}

#: The three documents, and each is a different source kind: a markdown file the server stores,
#: a PDF the server stores, and text the client already had. **The subject matter is chosen so
#: that one question can be answered from exactly one of them** — a question about a refund must
#: retrieve the refund policy and not the shipping one, which is what makes the citation check
#: mean something rather than merely pass.
MARKDOWN_TITLE = "Shipping policy"
MARKDOWN = (
    "# Shipping policy\n\n"
    "Standard shipping takes three to five business days. "
    "Express shipping arrives on the next working day, for a flat fee.\n"
)

PDF_TITLE = "Returns and refunds policy"
PDF_LINES = (
    "Returns and Refunds Policy",
    "Customers may return an item within 30 days of delivery.",
    "Refunds are processed within five working days",
    "of approval, to the original payment method.",
)

MANUAL_TITLE = "Support hours"
MANUAL = (
    "The support desk answers messages from 08:00 to 18:00 UTC, Monday to Friday. "
    "Messages received outside those hours are answered on the next working day."
)

#: §23's question. The answer is in the PDF and nowhere else.
ANSWERED_QUESTION = "How long does a refund take to arrive?"

#: §24's question. Nothing in the three documents discusses discounts, and this is the case the
#: refusal exists for: a plausible question with no source, where a model answering from its
#: priors would produce something confidently wrong.
REFUSED_QUESTION = "Do you offer a student discount for part-time students?"

#: The words §24's grounding has to produce, and the reason there are four of them: the model is
#: asked to answer in its own words, so "five working days" and "5 business days" are the same
#: correct answer. Checking for the number and the unit — rather than for an exact sentence —
#: is the difference between asserting that the draft is grounded and asserting that it is
#: word-for-word the document.
REFUND_PHRASES = (
    "five working days",
    "5 working days",
    "five business days",
    "5 business days",
)

SUBJECT = "When will my refund arrive?"
DESCRIPTION = (
    "I posted my order back to you last week and I still have not seen the money. I have looked "
    "at my statement twice. How long is a refund supposed to take?"
)

#: §24's sentence, spelled here as the literal a client reads. **Deliberately duplicated from
#: `app/services/knowledge_service.py:NO_ANSWER` rather than imported**, because importing it
#: would make this script agree with the implementation by construction — and the thing worth
#: asserting about a promise a customer reads is that it says those words, not that two
#: references resolve to one object.
NO_ANSWER = "The knowledge base does not contain sufficient information to answer that question."

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record one assertion. The script runs to the end even after a failure.

    A walkthrough that stopped at the first problem would hide the rest of the story, and seeing
    the rest of the story is why one walks through rather than running the suite.
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


def a_pdf(lines: Sequence[str]) -> bytes:
    """A minimal but real PDF containing `lines`, one per text line.

    **Built here rather than checked in as a fixture**, and the reason is that a binary blob in a
    script directory is a thing nobody can read or change. This is forty lines of PDF: a catalog,
    a page tree, one page, one Helvetica text object, and the cross-reference table every reader
    needs to find them. `pypdf` — the same library the ingestion worker extracts with — reads it
    back, and section 1 asserts that it did, so the fixture is proven by the pipeline that
    consumes it rather than by a file that happens to be in the repository.
    """
    body = (
        "BT /F1 12 Tf 72 720 Td "
        + " ".join(f"({_escape(line)}) Tj 0 -16 Td" for line in lines)
        + " ET"
    ).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"

    # The cross-reference table, with a byte offset for every object. A reader that cannot find
    # this cannot open the file, which is why "minimal PDF" is not the same as "a text file with
    # a %PDF- header".
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _escape(text: str) -> str:
    """Escape the three characters a PDF string literal gives meaning to.

    A parenthesis in a document's text would otherwise close the string early and take the rest
    of the page with it — which is the sort of thing that produces a PDF every reader opens and
    no two readers read the same.
    """
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


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

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.delete(f"{BASE}{path}", headers=self.headers, timeout=30.0, **kwargs)


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

    # --- §22: registering a document ------------------------------------------------

    def author(self, title: str, content: str) -> httpx.Response:
        """`POST /knowledge` with text the client already has."""
        return self.post("/knowledge", json={"title": title, "content": content})

    def upload(self, filename: str, content: bytes, media_type: str, title: str) -> httpx.Response:
        """`POST /knowledge/upload` — the multipart route, and the only one that stores bytes."""
        return self.post(
            "/knowledge/upload",
            files={"file": (filename, content, media_type)},
            data={"title": title},
        )

    def document(self, document_id: str) -> dict[str, Any]:
        response = self.get(f"/knowledge/{document_id}")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def documents(self, term: str | None = None) -> list[dict[str, Any]]:
        params = {"q": term} if term is not None else {}
        response = self.get("/knowledge", params=params)
        assert response.status_code == 200, response.text
        return list(response.json())

    def forget(self, document_id: str) -> httpx.Response:
        return self.delete(f"/knowledge/{document_id}")

    # --- §23: asking a question ------------------------------------------------------

    def ask(self, question: str) -> httpx.Response:
        return self.post("/knowledge/search", json={"question": question})

    # --- §21: the draft, and the bill -------------------------------------------------

    def usage(self) -> dict[str, Any]:
        response = self.get("/analytics/overview")
        assert response.status_code == 200, response.text
        return dict(response.json()["ai_usage"])

    def raise_ticket(self) -> tuple[str, Principal]:
        """A customer, a ticket carrying a real question about refunds, and their own login."""
        suffix = uuid.uuid4().hex[:8]
        customer = self.post(
            "/customers", json={"name": "Grace Hopper", "email": f"grace-{suffix}@customer.example"}
        )
        customer.raise_for_status()
        customer_id = str(customer.json()["id"])

        address = f"portal-{suffix}@customer.example"
        created = self.post(
            "/users",
            json={
                "name": "Grace Hopper",
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

        ticket = self.post(
            "/tickets",
            json={"subject": SUBJECT, "description": DESCRIPTION, "customer_id": customer_id},
        )
        ticket.raise_for_status()
        return str(ticket.json()["id"]), Principal(str(login.json()["access_token"]))

    def suggest(self, ticket_id: str) -> httpx.Response:
        return self.post(f"/tickets/{ticket_id}/ai/suggest-response")

    def draft_row(self, ticket_id: str) -> dict[str, Any] | None:
        """The newest `suggest_response` row, or `None`.

        `/ai/analyses` serves the newest row of each kind, one per operation, so this is the
        draft's current state and not a history of it — which is what a staff screen would be
        showing, and the right thing to poll.
        """
        response = self.get(f"/tickets/{ticket_id}/ai/analyses")
        assert response.status_code == 200, response.text
        rows = [row for row in response.json() if row["operation"] == "suggest_response"]
        return dict(rows[0]) if rows else None


def wait_for_document(
    tenant: Tenant, document_id: str, *, timeout: float = WORKER_TIMEOUT
) -> dict[str, Any]:
    """Poll the read route until the document is terminal, or the timeout expires.

    Polling the API rather than watching the worker's log, because that is what a client does:
    the route exists so an admin screen can show a document being processed instead of an empty
    list, and this is that route being used for its purpose. A timeout returns what it last saw,
    so the caller reports the real state rather than a bare "timed out".
    """
    deadline = time.monotonic() + timeout
    document = tenant.document(document_id)
    while document["status"] not in TERMINAL:
        if time.monotonic() >= deadline:
            return document
        time.sleep(2.0)
        document = tenant.document(document_id)
    return document


def wait_for_draft(
    tenant: Tenant, ticket_id: str, *, timeout: float = WORKER_TIMEOUT
) -> dict[str, Any] | None:
    """Poll the analyses route until §21's row is terminal, or the timeout expires."""
    deadline = time.monotonic() + timeout
    while True:
        row = tenant.draft_row(ticket_id)
        if row is not None and row["status"] in TERMINAL:
            return row
        if time.monotonic() >= deadline:
            return row
        time.sleep(2.0)


def wait_for_ticket_analysis(tenant: Tenant, ticket_id: str) -> None:
    """Wait until every row on a fresh ticket is terminal, so a baseline is final.

    **Section 4 compares `/analytics/overview` either side of the draft, and that comparison is
    only meaningful once the ticket's own §18 rows have landed.** §18 runs on ticket creation, so
    a baseline taken while `classify` and `sentiment` are still `pending` would count them
    against the draft — and the section would report that asking for a draft cost two generations
    rather than one embedding and one generation. This is Phase W's reason for waiting, and it is
    the same wait.
    """
    deadline = time.monotonic() + WORKER_TIMEOUT
    while True:
        response = tenant.get(f"/tickets/{ticket_id}/ai/analyses")
        assert response.status_code == 200, response.text
        rows: list[dict[str, Any]] = list(response.json())
        if rows and all(row["status"] in TERMINAL for row in rows):
            print(f"  the ticket's own §18 analysis settled: {[r['operation'] for r in rows]}")
            return
        if time.monotonic() >= deadline:
            states = [(row["operation"], row["status"]) for row in rows]
            print(f"  §18 had not settled before the timeout: {states}")
            return
        time.sleep(2.0)


def calls_by_operation(usage: dict[str, Any]) -> dict[str, int]:
    """`/analytics/overview`'s breakdown, as a mapping this script can look things up in."""
    return {str(row["operation"]): int(row["calls"]) for row in usage["by_operation"]}


def spend(usage: dict[str, Any]) -> str:
    """One `/analytics/overview` reading, in a line."""
    return (
        f"calls={usage['calls']} failed={usage['failed_calls']} "
        f"cost={usage['cost_usd']} by_operation={calls_by_operation(usage)}"
    )


# ---------------------------------------------------------------------------
# The sections
# ---------------------------------------------------------------------------


def three_kinds_of_document(tenant: Tenant) -> dict[str, str]:
    section("1. §22: three source kinds in, and the worker makes each one searchable")

    # The fixture proves itself before it is uploaded, through the same library the ingestion
    # worker extracts with. A hand-built PDF is the kind of file that opens in one viewer and
    # not another, so "section 1 completed" has to mean the text came back and not merely that
    # the bytes were accepted.
    fixture = a_pdf(PDF_LINES)
    extracted = PdfReader(io.BytesIO(fixture)).pages[0].extract_text()
    check(
        "the PDF fixture is a real PDF that pypdf reads back",
        all(line in extracted for line in PDF_LINES),
        extracted,
    )

    # The two uploads first, so the wait below is over the slowest of the three.
    pdf = tenant.upload("handbook.pdf", fixture, "application/pdf", PDF_TITLE)
    check("the PDF upload answered 201", pdf.status_code == 201, pdf.text)
    markdown = tenant.upload("shipping.md", MARKDOWN.encode(), "text/markdown", MARKDOWN_TITLE)
    check("the markdown upload answered 201", markdown.status_code == 201, markdown.text)
    manual = tenant.author(MANUAL_TITLE, MANUAL)
    check("the hand-authored document answered 201", manual.status_code == 201, manual.text)

    if pdf.status_code != 201 or markdown.status_code != 201 or manual.status_code != 201:
        return {}

    for label, response in (("pdf", pdf), ("markdown", markdown), ("manual", manual)):
        body = response.json()
        # **201 and `pending` is the honest pair.** A `202` would say "accepted, result later";
        # this document exists now and is readable while its ingestion runs, so the status is on
        # the row rather than in the code — which is why nothing here waits for `completed`
        # before it can name an id.
        check(
            f"the {label} document is pending, and readable while it is",
            body["status"] == "pending",
            str(body["status"]),
        )

    identifiers = {
        "pdf": str(pdf.json()["id"]),
        "markdown": str(markdown.json()["id"]),
        "manual": str(manual.json()["id"]),
    }
    kinds = {
        "pdf": pdf.json()["source_type"],
        "markdown": markdown.json()["source_type"],
        "manual": manual.json()["source_type"],
    }
    check(
        "the two files are `upload` and the pasted text is `manual`, derived from the transport",
        kinds == {"pdf": "upload", "markdown": "upload", "manual": "manual"},
        str(kinds),
    )

    started = time.monotonic()
    settled = {
        name: wait_for_document(tenant, identifier) for name, identifier in identifiers.items()
    }
    elapsed = time.monotonic() - started
    print(f"  all three settled after {elapsed:.1f}s")

    for name, document in settled.items():
        if document["status"] != "completed":
            check(f"the {name} document completed", False, str(document))
            print(f"        {document['status']}: {document['error_message']}")
            continue
        check(f"the {name} document completed", True)
        # §22's pipeline ends "document becomes searchable", and published-and-completed is the
        # predicate the partial index and the retrieval query both use. The worker sets both; no
        # route does, and there is no publish endpoint.
        check(
            "and it is published, with chunks",
            document["is_published"] is True and document["chunk_count"] >= 1,
            f"published={document['is_published']} chunks={document['chunk_count']}",
        )
        check(
            "and it carries no failure reason",
            document["error_message"] is None,
            str(document["error_message"]),
        )

    # The PDF's text survived the trip through `pypdf`, asserted through the pipeline that
    # consumes it rather than by opening the fixture in a viewer. This is the one assertion that
    # makes the hand-built PDF in `a_pdf` a document rather than a file with a header.
    refunded = settled["pdf"]
    check(
        "the PDF extracted to text the chunker could see",
        refunded["chunk_count"] >= 1 and refunded["error_message"] is None,
        str(refunded),
    )

    # `?q=` is the title filter, served by the trigram index Phase D declared for this view.
    found = tenant.documents("refund")
    check(
        "the title filter finds the refund policy and only it",
        [document["title"] for document in found] == [PDF_TITLE],
        str([document["title"] for document in found]),
    )
    check("and an unfiltered list holds all three", len(tenant.documents()) == 3)

    # **Ingestion writes no ticket-attributed ledger row**, because a document belongs to the
    # organization and not to a conversation. This tenant has no tickets at all yet, and
    # `/analytics/overview` counts only calls attributed to a ticket in the caller's row scope —
    # so the embedding calls that just happened are invisible here by design and not by failure.
    usage = tenant.usage()
    check(
        "the three ingestions wrote no ticket-attributed ledger row",
        usage["calls"] == 0 and usage["by_operation"] == [],
        spend(usage),
    )
    print("  (they are in `ai_usage` with a NULL ticket_id — the closing note shows the query.)")
    return identifiers


def the_question_the_documents_answer(tenant: Tenant) -> None:
    section("2. §23: a question one document answers, and the passage the answer came from")

    response = tenant.ask(ANSWERED_QUESTION)
    check("the question answered 200", response.status_code == 200, response.text)
    if response.status_code != 200:
        return
    body = response.json()
    answer = str(body["answer"])
    sources = list(body["sources"])
    print(f"  answer: {answer!r}")

    check("it is not §24's refusal", answer != NO_ANSWER)
    check("and it is a real answer", len(answer) > 20, str(len(answer)))
    check(
        "it mentions the policy's own number of days",
        any(phrase in answer.lower() for phrase in REFUND_PHRASES),
        answer,
    )

    # **The citations are resolved by the server, from the passages it supplied.** Nothing here
    # reads the model's prose for a `[1]`; what the model returned was a list of numbers, and
    # every id below was matched to a stored chunk by `_cite`.
    check("the answer cites at least one passage", sources != [], str(sources))
    if not sources:
        return

    titles = [source["document_title"] for source in sources]
    print(f"  sources: {titles}")
    check(
        "the nearest passage is from the refund policy, not the shipping one",
        titles[0] == PDF_TITLE,
        str(titles),
    )
    check(
        "every citation names a stored chunk and carries its own text",
        all(source["document_id"] and source["excerpt"] for source in sources),
        str(sources),
    )
    check(
        "the citations are in descending similarity, so the first is the best match",
        all(
            sources[index]["similarity"] >= sources[index + 1]["similarity"]
            for index in range(len(sources) - 1)
        ),
        str([source["similarity"] for source in sources]),
    )

    threshold = get_settings().RETRIEVAL_MIN_SIMILARITY
    check(
        f"the best match clears the configured threshold ({threshold})",
        float(sources[0]["similarity"]) > threshold,
        str(sources[0]["similarity"]),
    )
    check(
        "the passage's own words contain what the answer said",
        any(phrase in sources[0]["excerpt"].lower() for phrase in REFUND_PHRASES),
        sources[0]["excerpt"],
    )

    usage = tenant.usage()
    check(
        "the question wrote no ticket-attributed ledger row, because a question has no ticket",
        calls_by_operation(usage).get("embed", 0) == 0,
        spend(usage),
    )
    print("  the call happened and was billed -- it is simply not counted against a ticket.")


def the_question_the_documents_do_not(tenant: Tenant) -> None:
    section("3. §24: a question they do not answer, and the model that was never asked")

    response = tenant.ask(REFUSED_QUESTION)
    check("the refusal is a 200", response.status_code == 200, response.text)
    if response.status_code != 200:
        return
    body = response.json()
    print(f"  answer: {body['answer']!r}")

    # §24's sentence, and it is the *server's* text rather than a model's: a model asked to say
    # "I don't know" can say something else instead, and the refusal is a product promise rather
    # than a generation.
    check("the answer is §24's sentence, verbatim", body["answer"] == NO_ANSWER, body["answer"])
    check("and nothing is cited", body["sources"] == [], str(body["sources"]))
    check(
        "there is no `grounded` flag -- an empty source list is the whole of that fact",
        "grounded" not in body,
    )

    # **The refusal is the absence of a call.** `pypdf` did not run, no vector was computed, no
    # model was asked, and no ledger row exists to commit — which is why there is no `was_cached`
    # row here either: nothing answered from a cache, and nothing was ever going to be asked.
    print("  no embedding call and no model call: there was nothing to ground an answer in,")
    print("  so nothing was asked to write one. §24's last line, served by not calling.")


def the_draft_is_grounded_in_the_same_documents(tenant: Tenant) -> None:
    section("4. §21: a ticket about refunds, and the draft the policy grounds")

    if not get_settings().AI_API_KEY:
        print("  AI_API_KEY is not set, so there is no live draft to walk through -- skipped.")
        print("  §22 and §23 above do not need a generation provider to be true.")
        return

    ticket_id, portal = tenant.raise_ticket()
    print(f"  ticket: {ticket_id}")

    # The baseline has to be final, or §18's own rows get counted against the draft.
    wait_for_ticket_analysis(tenant, ticket_id)
    before = tenant.usage()
    print(f"  before: {spend(before)}")

    queued = tenant.suggest(ticket_id)
    check("the draft route accepted the request", queued.status_code in {200, 202}, queued.text)
    if queued.status_code not in {200, 202}:
        return

    started = time.monotonic()
    row = wait_for_draft(tenant, ticket_id)
    elapsed = time.monotonic() - started
    print(f"  settled after {elapsed:.1f}s: status={row and row['status']}")
    if row is None or row["status"] != "completed":
        check("the draft completed", False, str(row))
        if row and row["status"] == "failed":
            print(f"        failed: {row['error_message']}")
        return
    check("the draft completed", True)

    body = str((row["result"] or {}).get("body", ""))
    print(f"  draft: {body!r}")
    # **This is the claim §21's third prompt block exists to make true.** The prompt now carries
    # the retrieved passages; whether the model used them is visible in the text, and the phrase
    # is deliberately loose because the instruction asks for the model's own words.
    check(
        "the draft uses the policy's own turnaround time",
        any(phrase in body.lower() for phrase in REFUND_PHRASES),
        body,
    )

    after = tenant.usage()
    print(f"  after:  {spend(after)}")
    operations = calls_by_operation(after)

    # **The structural half of the grounding claim, and the reason section 1 checked for an empty
    # breakdown.** §21's retrieval embeds the ticket's own words to search with, and that call
    # *is* attributed to the ticket — so it appears here where the ingestion calls did not. This
    # is not an inference from the draft's wording; it is a row in the ledger that could only have
    # been written by the retrieval path.
    check(
        "the draft's retrieval made an embedding call, and it is in the ledger",
        operations.get("embed", 0) >= 1,
        str(operations),
    )
    check(
        "so asking for a draft costs one embedding as well as one generation",
        after["calls"] - before["calls"] >= 2,
        f"{before['calls']} -> {after['calls']}",
    )
    check(
        "the generation itself is recorded as a suggest_response call",
        operations.get("suggest_response", 0) >= 1,
        str(operations),
    )

    # §3's matrix, read live: a customer holds none of the four knowledge capabilities, so the
    # portal login cannot list the documents its own draft was grounded in. The knowledge base is
    # the organization's, and a customer is not in the organization.
    refused = portal.get("/knowledge")
    check(
        "the customer's own login cannot list the knowledge base",
        refused.status_code == 403,
        f"{refused.status_code}",
    )


def another_tenant_and_a_bystander(tenant: Tenant, identifiers: dict[str, str]) -> None:
    section("5. §53 and §3: another tenant's knowledge base is empty, and its ids are not readable")

    stranger = Tenant("Bystander")
    document_id = identifiers.get("pdf", "")

    # A 404 and not a 403, for ADR-009's reason: a tenant must not be able to size another's
    # knowledge base by watching which ids are refused. A foreign document and a nonexistent one
    # are the same answer, on both the read and the write.
    read = stranger.get(f"/knowledge/{document_id}")
    check("a foreign document is a 404 on read", read.status_code == 404, f"{read.status_code}")
    check(
        "and it names no document",
        read.json()["error"]["code"] == "KNOWLEDGE_DOCUMENT_NOT_FOUND",
        read.text,
    )
    removed = stranger.forget(document_id)
    check("and a 404 on delete", removed.status_code == 404, f"{removed.status_code}")
    check(
        "and the owner's document is untouched",
        tenant.document(document_id)["status"] == "completed",
    )

    # **The isolation claim, from the outside.** The stranger holds `KB_LIST` and
    # `AI_QUERY_KNOWLEDGE` — this is not a permission refusal — and its own knowledge base is
    # empty, so the very question the owner's documents answer is §24's refusal here. The
    # assertion is on the *result*, which is the only form of it worth making: a query-level test
    # would pass while the ranking leaked.
    check("the stranger's knowledge base lists nothing", stranger.documents() == [])
    answer = stranger.ask(ANSWERED_QUESTION)
    check(
        "and the question the owner's policy answers is refused for the stranger",
        answer.status_code == 200,
        answer.text,
    )
    if answer.status_code == 200:
        check(
            "with §24's sentence and nothing cited",
            answer.json()["answer"] == NO_ANSWER and answer.json()["sources"] == [],
            answer.text,
        )

    # §41's other direction: withdrawing a document is a delete, not an unpublish, and it takes
    # the chunks with it by the foreign key. The read that follows is the same 404 a stranger gets.
    shipping = identifiers.get("markdown", "")
    forgotten = tenant.forget(shipping)
    check("the owner can delete a document", forgotten.status_code == 204, forgotten.text)
    check("and it is gone", tenant.get(f"/knowledge/{shipping}").status_code == 404)
    check(
        "and the other two are still there",
        len(tenant.documents()) == 2,
        str([document["title"] for document in tenant.documents()]),
    )


def main() -> None:
    # The section headings carry §, and a redirected stdout on Windows defaults to the locale
    # encoding — cp1252, which has no §. It round-trips as a replacement character, so the
    # transcript this script exists to produce arrives unreadable. Asking for UTF-8 explicitly is
    # one line and is the difference between a record and a mess of question marks.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = get_settings()
    if not settings.EMBEDDING_API_KEY:
        print(
            "EMBEDDING_API_KEY is not set, so there is no live retrieval to walk through.\n\n"
            "Add an OpenAI key to .env and run again:\n"
            "  EMBEDDING_PROVIDER=openai  EMBEDDING_MODEL=text-embedding-3-small\n"
            "  EMBEDDING_API_KEY=sk-...\n\n"
            "`EMBEDDING_PROVIDER=fake` is not a substitute here. The pipeline, the storage, the\n"
            "worker, the ledger, and every route would still be real -- but a hash-derived vector\n"
            "has no relationship to meaning, so the *ranking* this script exists to demonstrate\n"
            "would be demonstration-free, and a right answer would have come out right by luck.\n"
            "The suite covers that configuration, where the vectors are scripted on purpose.\n\n"
            "This is a legal configuration, not a failure of this phase: the provider module\n"
            "refuses at the point of use, and the rest of the suite runs without a key."
        )
        sys.exit(0)

    print(
        f"embedding={settings.EMBEDDING_PROVIDER}/{settings.EMBEDDING_MODEL} "
        f"generation={settings.AI_PROVIDER}/{settings.AI_MODEL}"
    )

    tenant = Tenant("Phase X")
    identifiers = three_kinds_of_document(tenant)
    if not identifiers:
        print("\nthe documents did not all arrive; the rest of the script has nothing to ask.")
        sys.exit(1)

    the_question_the_documents_answer(tenant)
    the_question_the_documents_do_not(tenant)
    the_draft_is_grounded_in_the_same_documents(tenant)
    another_tenant_and_a_bystander(tenant, identifiers)

    print(f"\n{_passed} passed, {_failed} failed")

    print(
        "\nFour things this script could not do or show for itself.\n"
        "\n"
        "1. The ledger rows with no ticket. `/analytics/overview` counts only calls attributed\n"
        "   to a ticket in the caller's row scope, and a document belongs to the organization --\n"
        "   so every ingestion call, and the embedding and answering calls a question makes, are\n"
        "   in `ai_usage` with `ticket_id IS NULL` and are invisible to that endpoint by design.\n"
        "   Read them where they are, with the tenant's own organization id:\n"
        "\n"
        "     docker compose exec postgres psql -U supportflow -c \\\n"
        '       "SELECT operation, model, prompt_tokens, cost_usd, ticket_id FROM ai_usage \\\n'
        "        WHERE organization_id = '<the tenant id>' ORDER BY created_at\"\n"
        "\n"
        "   The contrast is the interesting part: the draft's embedding call *is* attributed to\n"
        "   the ticket, which is why section 4 could assert it and section 2 could not.\n"
        "\n"
        "2. The refusal's silence in the ledger. Section 3 shows §24's sentence and an empty\n"
        "   source list, but the stronger fact -- that no row was written at all -- is an\n"
        "   absence, and an absence has no response surface. Counting `ai_usage` rows for the\n"
        "   tenant before and after the refused question is the check, and it belongs in the\n"
        "   suite, where it is `tests/integration/test_knowledge_draft_grounding.py`'s assertion\n"
        "   rather than a sentence here.\n"
        "\n"
        "3. The `url` source kind. §22 lists three, and two of them are files. Fetching a URL\n"
        "   in a walkthrough means depending on a stranger's server being up and serving the\n"
        "   text this script expects -- which is a flaky test wearing a demonstration's clothes.\n"
        "   The guard is covered where it can be made deterministic, in\n"
        "   `tests/unit/test_url_fetch.py`, against an injected resolver.\n"
        "\n"
        "4. A document that fails. A scanned PDF extracts to nothing and lands `failed` with a\n"
        "   reason a person can read, and that path is asserted in the ingestion suite with a\n"
        "   provider that refuses. Producing one here would mean checking a broken binary blob\n"
        "   into the repository.\n"
        "\n"
        f"Left behind: two organizations ({tenant.name}, and one named Bystander), and in the\n"
        "first one three documents with their chunks and vectors -- minus the shipping policy,\n"
        "which section 5 deleted -- a customer, a portal login, a ticket, a draft message, and\n"
        "the ledger rows every call wrote. Nothing else here deletes anything."
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
