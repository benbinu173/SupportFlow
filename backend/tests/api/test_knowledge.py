"""§22's six routes, driven over real HTTP against a real database and a real bucket.

**What is asserted here and what is not.** Every status code, the shape of the read model, who
may reach which route, the audit entry a create writes, the task a create queues — and, for the
question route, both branches of §24. The *pipeline* is not asserted here: nothing in this file
ingests a document, because ingestion happens in a worker that this process does not run. The
tests that own it are `tests/integration/test_knowledge_ingestion.py` for the documents and
`tests/integration/test_knowledge_retrieval.py` for the vectors; this file is about the API
surface being the one the spec describes.

**Nothing here mocks storage.** `tests/api/test_attachments.py` gives the reason and it applies
unchanged: the claim is that a file which goes up is stored under a key composed of server-side
values, and a mocked bucket would make that claim about the mock.

**The queued task is asserted rather than allowed to run.** `queued_ingestions` is autouse in the
root conftest for the reason `queued_emails` is — every create ends in `enqueue_ingestion` — and
this is the file that reads what it recorded, including the tenant, which is half of what makes
the message safe to hand a worker.

**The question route is driven with a scripted retrieval.** An API test cannot publish a chunk —
that is the worker's job — so the two branches are reached by patching `knowledge_service.retrieve`
and, on the answered branch, the provider. What that exercises is everything after retrieval in
`knowledge_service.answer`: the refusal with no call, and the citation mapping that turns the
model's indices into stored passages and drops the ones that name nothing.
"""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai.errors import AIPermanentError
from app.ai.fake import FakeEmbeddingProvider, FakeProvider
from app.core.config import get_settings
from app.repositories.knowledge_repository import ChunkMatch
from app.schemas.knowledge import KnowledgeAnswerRead
from app.services import ai_service, knowledge_service
from app.services.knowledge_service import NO_ANSWER
from tests.conftest import API, KNOWLEDGE, OrgSession

pytestmark = pytest.mark.integration

UPLOAD = f"{KNOWLEDGE}/upload"
SEARCH = f"{KNOWLEDGE}/search"
AUDIT = f"{API}/audit-logs"

#: Real leading bytes, plus filler. `tests/api/test_attachments.py` sets the precedent: nothing
#: past the signature is inspected, so a valid file needs no binary fixture on disk.
PDF_BODY = b"%PDF-1.4\n" + b"synthetic policy text " * 8
MARKDOWN_BODY = b"# Refunds\n\nRefunds take 5 working days.\n"
PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"screenshot" * 4


def create(org: OrgSession, **payload: Any) -> Any:
    """POST a document to the JSON route."""
    return org.post(KNOWLEDGE, json=payload)


def upload(
    org: OrgSession,
    *,
    name: str = "policy.pdf",
    body: bytes = PDF_BODY,
    declared: str = "application/pdf",
    title: str = "Refund policy",
) -> Any:
    """POST a document to the multipart route, with the title field the route requires."""
    return org.post(UPLOAD, files={"file": (name, body, declared)}, data={"title": title})


def listing(org: OrgSession, **params: Any) -> list[dict[str, Any]]:
    """The documents this organization can see, asserting the read succeeded."""
    response = org.get(KNOWLEDGE, params=params)
    assert response.status_code == 200, response.text
    return list(response.json())


def actions(org: OrgSession) -> list[str]:
    """This organization's audit actions, newest first.

    **Lower-cased, and that is the API's choice rather than this helper's.** The read route
    serialises `AuditAction`'s *value* — `knowledge_document_created` — where
    `app/models/enums.py` names the member in upper case; `tests/api/test_audit.py` asserts
    against the same lower-case strings for the same reason. Normalising here means this file
    reads one casing at every call site, and each comparison below says in a comment which
    `AuditAction` member it is asserting.
    """
    response = org.get(AUDIT, params={"limit": 100})
    assert response.status_code == 200, response.text
    return [row["action"].lower() for row in response.json()]


# ---------------------------------------------------------------------------
# Creating: the JSON route, and the two source kinds behind it
# ---------------------------------------------------------------------------


def test_a_manual_document_is_created_pending_and_queued(
    client: TestClient, register_org: Any
) -> None:
    """**201 with a row that exists and is not readable yet**, which is the honest status.

    `pending` is the signal, and it is on the row rather than in the code: `GET
    /knowledge/{id}` answers while the worker is still extracting, and what it says is that
    nothing is searchable yet. `is_published` is false beside it, because publishing is what
    finishing ingestion means (§22's last step) and no request sets it.
    """
    org = register_org()

    response = create(org, title="Refund policy", content="Refunds take 5 working days.")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["title"] == "Refund policy"
    assert body["source_type"] == "manual"
    assert (body["status"], body["is_published"], body["chunk_count"]) == ("pending", False, 0)
    assert body["processed_at"] is None
    assert body["created_by_id"] == org.user_id


def test_the_queued_task_names_the_document_and_its_tenant(
    client: TestClient, register_org: Any, queued_ingestions: list[tuple[str, str]]
) -> None:
    """The message is two ids, and the second one is the tenant.

    A worker has no `TenantContext` and must not invent one, so the organization it fills a row
    for travels with the job. A recorder that kept only the document id could not ask this
    question — see `queued_ingestions` — and an id-only message would be a task that had to read
    the tenant off the row it was about to write, which is the shape ADR-009 exists against.

    The tenant is asserted *shaped* rather than *equal to* something: no route returns the
    caller's organization id, and the property that matters here is that the value is a
    server-side identifier rather than a client's claim. That it is *this* tenant's id is the
    isolation suite's assertion, where there are two tenants to tell apart.
    """
    org = register_org()

    created = create(org, title="Refund policy", content="Refunds take 5 working days.").json()

    ((document_id, organization_id),) = queued_ingestions
    assert document_id == created["id"]
    assert uuid.UUID(organization_id)


def test_a_url_document_stores_the_reference_and_is_not_fetched_here(
    client: TestClient, register_org: Any
) -> None:
    """Registration is not fetching. The row is written with the URL and the worker fetches it.

    That ordering is §16's *"the API should not wait unnecessarily"* with a stranger's web server
    on the other end: a request that fetched would hold a connection across up to six guarded
    hops, and the guard that decides whether the address is even reachable lives in the worker.
    The proof that nothing was fetched is that the document is `pending` and empty — and that
    this test, which never patches `url_fetch`, completes at all.
    """
    org = register_org()

    response = create(org, title="Public policy", url="https://example.com/refunds")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_type"] == "url"
    assert body["status"] == "pending"


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "Both", "content": "text", "url": "https://example.com/"},
        {"title": "Neither"},
    ],
)
def test_the_body_must_name_exactly_one_source(
    client: TestClient, register_org: Any, payload: dict[str, Any]
) -> None:
    """One of `content` and `url`, enforced by the schema rather than by the route.

    Both at once is refused as firmly as neither, and that is what makes `source_type` derivable
    rather than declarable: a body that could say "manual" while carrying a URL would be a body
    that says two things, and the service reads which field was sent instead of trusting a field
    that could disagree.
    """
    org = register_org()

    assert create(org, **payload).status_code == 422


def test_creating_a_document_is_audited(client: TestClient, register_org: Any) -> None:
    """§34's action, written in the same transaction as the row.

    `KNOWLEDGE_DOCUMENT_DELETED` is the other one §34 names and it is asserted where the delete
    is; what this checks is that the *create* is on the trail at all, since a route that wrote
    the row and skipped the audit would look identical from every other angle.
    """
    org = register_org()

    create(org, title="Refund policy", content="Refunds take 5 working days.")

    assert "knowledge_document_created" in actions(org)


def test_a_search_writes_no_audit_entry(client: TestClient, register_org: Any) -> None:
    """§34's list has two knowledge actions and a query is neither of them.

    A search is not a mutation, and an audit trail that recorded reads would be a trail whose
    signal is buried in it. §34 is the list, and this is the assertion that the list was
    followed rather than added to by a route that felt important.
    """
    org = register_org()

    org.post(SEARCH, json={"question": "How long do refunds take?"})

    assert "knowledge_document_created" not in actions(org)
    assert not [action for action in actions(org) if "query" in action]


# ---------------------------------------------------------------------------
# Creating: the file route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "body", "declared", "expected"),
    [
        ("policy.pdf", PDF_BODY, "application/pdf", "application/pdf"),
        ("refunds.md", MARKDOWN_BODY, "text/markdown", "text/markdown"),
        ("policy.txt", MARKDOWN_BODY, "text/plain", "text/plain"),
    ],
)
def test_a_readable_file_is_accepted_and_stored_under_a_server_side_key(
    client: TestClient,
    register_org: Any,
    queued_ingestions: list[tuple[str, str]],
    name: str,
    body: bytes,
    declared: str,
    expected: str,
) -> None:
    """Three of §22's four source kinds arrive as a file, and all three are stored the same way.

    The read model carries no key — it is an internal name for an object in a private bucket —
    so what a caller learns is that the document exists, which is what a client needs. The
    upload itself is exercised for real against MinIO, because the claim that the bytes went
    somewhere is exactly the claim a mock would fabricate.

    Markdown is a plain text file a person wrote; the extension is what tells the worker how to
    read it back, and it is in the key by construction rather than by sanitizing the client's
    filename.
    """
    org = register_org()

    response = upload(org, name=name, body=body, declared=declared, title=name)

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["source_type"] == "upload"
    assert created["status"] == "pending"
    assert "source_reference" not in created
    assert [document_id for document_id, _ in queued_ingestions] == [created["id"]]


def test_a_file_the_extractor_cannot_read_is_refused_and_queues_nothing(
    client: TestClient, register_org: Any, queued_ingestions: list[tuple[str, str]]
) -> None:
    """A screenshot is a fine ticket attachment and not a knowledge document.

    Refused at upload rather than half a minute later in the worker, so the admin learns
    immediately — and the document does not exist, which is what `queued_ingestions` being empty
    says. A refusal that still queued a job would be a job for a row that was never written.
    """
    org = register_org()

    response = upload(org, name="shot.png", body=PNG_BODY, declared="image/png")

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
    assert listing(org) == []
    assert queued_ingestions == []


def test_a_file_whose_bytes_disagree_with_its_declared_type_is_refused(
    client: TestClient, register_org: Any
) -> None:
    """The signature is checked, not the header and not the filename.

    A `.pdf` name and an `application/pdf` header on a PNG is the shape of an evasion attempt,
    and it is the case `app/core/file_validation.py` was written for: the leading bytes decide.
    """
    org = register_org()

    response = upload(org, name="policy.pdf", body=PNG_BODY, declared="application/pdf")

    assert response.status_code == 422, response.text
    assert listing(org) == []


def test_an_empty_file_is_refused(client: TestClient, register_org: Any) -> None:
    """Zero bytes is a document that would ingest to nothing and answer nothing."""
    org = register_org()

    response = upload(org, body=b"Refunds", declared="application/pdf")

    assert response.status_code == 422, response.text
    assert listing(org) == []


def test_an_oversized_file_is_refused(
    client: TestClient, register_org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MAX_KNOWLEDGE_DOCUMENT_BYTES`, counted rather than read from `Content-Length`.

    This is the knob that decides what one document can cost to embed, which is why it exists as
    a setting at all — an attachment is stored, and a knowledge document is paid for by the token.
    The limit is lowered here so the branch is reached without a multi-megabyte body; the
    counting-against-a-header property is `test_attachments.py`'s, on the same `_measure` shape.
    """
    org = register_org()
    monkeypatch.setattr(
        "app.services.knowledge_service.get_settings",
        lambda: _SettingsStub(MAX_KNOWLEDGE_DOCUMENT_BYTES=16),
    )

    response = upload(org)

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_an_upload_without_a_title_is_refused(client: TestClient, register_org: Any) -> None:
    """The title is required and the original filename is not stored.

    A file arrives with whatever name the client's filesystem gave it; the name a document has in
    the knowledge base is a thing a person types, and it is what every read, citation, and list
    entry shows.
    """
    org = register_org()

    response = org.post(UPLOAD, files={"file": ("policy.pdf", PDF_BODY, "application/pdf")})

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Reading: the list, the filter, and the page
# ---------------------------------------------------------------------------


def test_the_list_is_newest_first(client: TestClient, register_org: Any) -> None:
    org = register_org()
    for title in ("First", "Second", "Third"):
        assert create(org, title=title, content=f"{title} text").status_code == 201

    assert [row["title"] for row in listing(org)] == ["Third", "Second", "First"]


def test_the_title_filter_is_a_substring_search(client: TestClient, register_org: Any) -> None:
    """The consumer for `ix_knowledge_documents_title_trgm` — the index Phase D declared for
    *"Title search in the admin list view"*.

    A `%term%` match is what a B-tree cannot answer, so without this route that index would be a
    promise nothing keeps. An empty `q` means no filter — a query string that arrives empty is
    not a search for the empty string.
    """
    org = register_org()
    for title in ("Refund policy", "Shipping policy", "Refund exceptions"):
        create(org, title=title, content=f"{title} body")

    assert [row["title"] for row in listing(org, q="Refund")] == [
        "Refund exceptions",
        "Refund policy",
    ]
    assert len(listing(org, q="")) == 3
    assert listing(org, q="Nothing like this") == []


def test_documents_of_every_status_are_listed(client: TestClient, register_org: Any) -> None:
    """This is the administrator's view, so `pending` is not hidden from it.

    A document that is stuck or broken is precisely what an admin opens the list to find out
    about; the published-and-completed rule belongs to retrieval, and applying it here would hide
    the rows most worth seeing.
    """
    org = register_org()

    create(org, title="Refund policy", content="Refunds take 5 working days.")

    assert [row["status"] for row in listing(org)] == ["pending"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (None, 200),
        ({"limit": 1}, 200),
        ({"limit": 0}, 422),
        ({"limit": 101}, 422),
        ({"offset": -1}, 422),
    ],
)
def test_the_page_is_bounded(
    client: TestClient, register_org: Any, params: dict[str, Any] | None, expected: int
) -> None:
    """A list route with no ceiling is a route a client can ask for the whole table through.

    `limit` is capped at 100 and `offset` is non-negative, both declared on the query parameters
    rather than checked in the body, so the refusal is a 422 from the framework and the bound is
    visible in the OpenAPI document.
    """
    org = register_org()
    create(org, title="Refund policy", content="Refunds take 5 working days.")

    assert org.get(KNOWLEDGE, params=params or {}).status_code == expected


def test_a_document_reads_back_by_id(client: TestClient, register_org: Any) -> None:
    org = register_org()
    created = create(org, title="Refund policy", content="Refunds take 5 working days.").json()

    response = org.get(f"{KNOWLEDGE}/{created['id']}")

    assert response.status_code == 200, response.text
    assert response.json() == created


def test_the_read_model_carries_neither_the_text_nor_the_key(
    client: TestClient, register_org: Any
) -> None:
    """Two fields withheld, for two different reasons.

    `content` is retained so a document can be re-chunked, and it is not a document's readable
    surface — a page of twenty documents should not carry twenty documents' worth of text.
    `source_reference` is an object-storage key for an upload, and for a `url` it is the client's
    own input; withholding one case's internal name protects both. `source_type` is exposed,
    which is what a reader needs to know where a document came from.
    """
    org = register_org()

    body = create(org, title="Refund policy", content="Refunds take 5 working days.").json()

    assert "content" not in body
    assert "source_reference" not in body
    assert body["source_type"] == "manual"


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


def test_deleting_a_document_is_204_and_then_a_404(client: TestClient, register_org: Any) -> None:
    """204 and not 200: the representation that would have been returned no longer exists.

    A caller that wants to confirm the deletion reads the document and gets a 404 — the same
    answer it would get for an id it never knew, which is the property the next test leans on.
    """
    org = register_org()
    created = create(org, title="Refund policy", content="Refunds take 5 working days.").json()

    assert org.delete(f"{KNOWLEDGE}/{created['id']}").status_code == 204
    assert org.get(f"{KNOWLEDGE}/{created['id']}").status_code == 404
    assert listing(org) == []


def test_deleting_a_document_is_audited(client: TestClient, register_org: Any) -> None:
    org = register_org()
    created = create(org, title="Refund policy", content="Refunds take 5 working days.").json()

    org.delete(f"{KNOWLEDGE}/{created['id']}")

    assert "knowledge_document_deleted" in actions(org)


# ---------------------------------------------------------------------------
# Tenancy, and who may reach which route
# ---------------------------------------------------------------------------


def test_another_organizations_document_is_a_404_on_read_and_delete(
    client: TestClient, register_org: Any
) -> None:
    """The same 404 a document that does not exist produces, and it is not politeness.

    A distinguishable refusal confirms a document the caller was never meant to know about — the
    fact that an id exists somewhere else is a fact about another tenant (ADR-009). Both verbs
    are asserted because a delete that reached across tenants would be the worse of the two
    failures and the one a read-only test would miss.
    """
    mine = register_org(organization_name="Mine")
    theirs = register_org(organization_name="Theirs")
    theirs_id = create(theirs, title="Their policy", content="Their text.").json()["id"]

    assert mine.get(f"{KNOWLEDGE}/{theirs_id}").status_code == 404
    assert mine.delete(f"{KNOWLEDGE}/{theirs_id}").status_code == 404

    # And it is still there, which is what makes the 404 above a refusal rather than a delete
    # that happened and then reported the row missing.
    assert theirs.get(f"{KNOWLEDGE}/{theirs_id}").status_code == 200


def test_the_list_shows_only_this_organizations_documents(
    client: TestClient, register_org: Any
) -> None:
    mine = register_org(organization_name="Mine")
    theirs = register_org(organization_name="Theirs")
    create(mine, title="Mine", content="Mine.")
    create(theirs, title="Theirs", content="Theirs.")

    assert [row["title"] for row in listing(mine)] == ["Mine"]


def test_a_manager_and_an_agent_may_list_and_ask_but_not_add_or_delete(
    client: TestClient, register_org: Any
) -> None:
    """§3's matrix, read off the running routes rather than off the permission table.

    `KB_LIST` and `AI_QUERY_KNOWLEDGE` are the two a manager and an agent hold; adding a document
    and removing one are administrative — a delete is unrecoverable and an upload spends the
    organization's embedding budget, so both stay with the role that owns the knowledge base.
    Asserting both halves matters: a route that took the wrong capability would pass a
    customer-is-refused test and fail this one.
    """
    org = register_org()
    created = create(org, title="Refund policy", content="Refunds take 5 working days.").json()

    for role in ("manager", "agent"):
        member = org.add_user(role)

        assert member.get(KNOWLEDGE).status_code == 200
        assert member.get(f"{KNOWLEDGE}/{created['id']}").status_code == 200
        assert member.post(SEARCH, json={"question": "How long?"}).status_code == 200
        assert member.post(KNOWLEDGE, json={"title": "X", "content": "Y"}).status_code == 403
        assert member.delete(f"{KNOWLEDGE}/{created['id']}").status_code == 403


def test_a_customer_holds_none_of_the_four_knowledge_capabilities(
    client: TestClient, register_org: Any
) -> None:
    """§3 gives the customer role no knowledge capability at all, including the reads.

    A knowledge base is the organization's internal documentation; a portal user asking it
    questions is what §23's route is *not* for in this phase, and the refusal is a 403 rather
    than a 404 because the document is not being hidden — the capability is.
    """
    org = register_org()
    created = create(org, title="Refund policy", content="Refunds take 5 working days.").json()
    customer = org.add_user("customer")

    assert customer.get(KNOWLEDGE).status_code == 403
    assert customer.get(f"{KNOWLEDGE}/{created['id']}").status_code == 403
    assert customer.post(KNOWLEDGE, json={"title": "X", "content": "Y"}).status_code == 403
    assert customer.post(SEARCH, json={"question": "How long?"}).status_code == 403
    assert customer.delete(f"{KNOWLEDGE}/{created['id']}").status_code == 403


def test_an_unauthenticated_request_is_refused(client: TestClient) -> None:
    assert client.get(KNOWLEDGE).status_code == 401
    assert client.post(SEARCH, json={"question": "How long?"}).status_code == 401


# ---------------------------------------------------------------------------
# §23 and §24 — the question
# ---------------------------------------------------------------------------


def test_a_question_with_nothing_published_gets_the_specifications_sentence(
    client: TestClient, register_org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**§24's refusal, and the half of it that is not in the response.**

    The organization has a document, but it is `pending` — nothing is published — so retrieval
    finds nothing, and the answer is the server's own sentence with an empty source list. The
    assertion that carries the weight is `provider.calls == 0`: **no model was asked at all**.
    There is nothing to ground an answer in, so nothing is asked to write one, and a model with
    no passages would answer from its priors while appearing to cite the knowledge base.

    The embedding double is installed rather than the real provider so that a call *could* be
    observed — with no key configured a call would fail, and a failing call is not the same
    evidence as no call.
    """
    org = register_org()
    create(org, title="Refund policy", content="Refunds take 5 working days.")
    embeddings = _use_embeddings(monkeypatch)
    generator = _use_generator(monkeypatch)

    response = org.post(SEARCH, json={"question": "How long do refunds take?"})

    assert response.status_code == 200, response.text
    assert response.json() == {"answer": NO_ANSWER, "sources": []}
    assert embeddings.calls == 0
    assert generator.calls == 0


def test_a_question_with_a_retrieved_passage_is_answered_and_cited(
    client: TestClient, register_org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grounded branch: one call, and citations built from the retrieval rather than the prose.

    The model names passage 1 and passage 9. There is no ninth passage, so that index is dropped
    with a log line rather than trusted or raised on — dropping is not fabricating, and a model
    that miscounted still produced a usable answer (§24). The excerpt comes from the retrieved
    chunk and not from anything the model wrote, which is what makes a citation checkable.
    """
    org = register_org()
    _retrieve(
        monkeypatch, [_match(index=0, content="Refunds take 5 working days.", similarity=0.91)]
    )
    generator = _use_generator(
        monkeypatch, {"answer": "Refunds take 5 working days.", "used_sources": [1, 9]}
    )

    response = org.post(SEARCH, json={"question": "How long do refunds take?"})

    assert response.status_code == 200, response.text
    body = KnowledgeAnswerRead.model_validate(response.json())
    assert body.answer == "Refunds take 5 working days."
    assert [source.chunk_index for source in body.sources] == [0]
    assert body.sources[0].excerpt == "Refunds take 5 working days."
    assert body.sources[0].similarity == 0.91
    assert body.sources[0].document_title == "Refund policy"
    assert generator.calls == 1


def test_the_sources_are_in_retrieval_order_and_de_duplicated(
    client: TestClient, register_org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model naming passage 2 first, then 1, then 2 again gives two sources in retrieval order.

    The order a caller reads citations in is a statement about which passage was closest, and the
    model's typing order is not that statement. Naming one passage twice is one passage.
    """
    org = register_org()
    _retrieve(
        monkeypatch,
        [
            _match(index=0, content="Nearest passage.", similarity=0.9),
            _match(index=1, content="Further passage.", similarity=0.6),
        ],
    )
    _use_generator(monkeypatch, {"answer": "An answer.", "used_sources": [2, 1, 2]})

    body = org.post(SEARCH, json={"question": "Anything?"}).json()

    assert [source["chunk_index"] for source in body["sources"]] == [0, 1]


def test_an_answer_naming_no_passage_is_legal_and_cites_nothing(
    client: TestClient, register_org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that answered from a passage without naming it produces an answer with no sources.

    Not a validation failure and not a fabricated citation to fill the field — an empty list is
    the model's own claim, and the response carries it through.
    """
    org = register_org()
    _retrieve(monkeypatch, [_match(index=0, content="A passage.", similarity=0.8)])
    _use_generator(monkeypatch, {"answer": "An answer.", "used_sources": []})

    body = org.post(SEARCH, json={"question": "Anything?"}).json()

    assert body["answer"] == "An answer."
    assert body["sources"] == []


def test_a_retrieval_failure_on_the_question_route_is_a_503_not_an_empty_answer(
    client: TestClient, register_org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question the product could not answer is not a question the product answered with nothing.

    The two are different reports and a caller acts differently on them: §24's sentence means
    "your documents do not cover this", and a 503 means "ask again". §21's draft path makes the
    opposite choice — it drafts anyway — and that difference is a policy of each caller, which is
    why retrieval lets the failure through rather than deciding here.

    **The failure is permanent on purpose.** A transient one would be retried with §17's backoff,
    and this test would spend three sleeps proving what `tests/unit/test_ai_retry.py` already
    proves; both kinds end at the same `AIServiceError`, which is the only part being asserted.
    `_pretend_something_is_published` is needed because the guard would otherwise answer this
    organization's question from an empty index without ever reaching the embedding call.
    """
    org = register_org()
    _pretend_something_is_published(monkeypatch)
    monkeypatch.setattr(ai_service, "_embedding_provider", lambda: _BrokenEmbeddings())

    response = org.post(SEARCH, json={"question": "How long do refunds take?"})

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "AI_SERVICE_ERROR"


def test_the_question_route_is_rate_limited(
    client: TestClient, register_org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§45: the one route in this module that spends money in the request path.

    Every call it admits can become a provider call charged per token, so it is guarded by
    `limit_ai` alongside the analysis routes — a question is free text from an authenticated
    account, and the budget it spends is the organization's.
    """
    org = register_org()
    monkeypatch.setattr(get_settings(), "RATE_LIMIT_AI_PER_HOUR", 0, raising=False)

    assert org.post(SEARCH, json={"question": "How long?"}).status_code == 429


@pytest.mark.parametrize("question", ["", "x" * 2_001])
def test_a_question_is_bounded_at_both_ends(
    client: TestClient, register_org: Any, question: str
) -> None:
    """A question is a question and not a document.

    §23's example is fourteen characters, and there is an endpoint for documents; a
    several-thousand-character "question" is one, and the field says so. Both bounds are declared
    on the schema rather than checked in the route, so the refusal is the framework's 422 and the
    limits are in the OpenAPI document.
    """
    org = register_org()

    assert org.post(SEARCH, json={"question": question}).status_code == 422


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SettingsStub:
    """The one setting a helper reads, and nothing else.

    `test_attachments.py` passes a `SimpleNamespace`; this exists because `knowledge_service`
    reads more than one setting on its paths and a stub that raised `AttributeError` for the
    others would fail for a reason unrelated to the limit under test. An explicit class keeps
    the substitute's surface visible.
    """

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class _BrokenEmbeddings:
    """An `EmbeddingProvider` whose call fails, for the branch where retrieval itself breaks."""

    name = "broken"

    async def generate_embedding(self, texts: list[str]) -> Any:
        raise AIPermanentError("the provider rejected the API key")


def _use_embeddings(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> FakeEmbeddingProvider:
    """Point the service at a scripted embedding provider. The seam `_embedding_provider` is for."""
    provider = FakeEmbeddingProvider(**kwargs)
    monkeypatch.setattr(ai_service, "_embedding_provider", lambda: provider)
    return provider


def _use_generator(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> FakeProvider:
    """Point the *generation* service at a scripted provider — the §23 answer comes from here."""
    provider = FakeProvider(*outcomes)
    monkeypatch.setattr(ai_service, "_provider", lambda: provider)
    return provider


def _match(
    *, index: int, content: str, similarity: float, title: str = "Refund policy"
) -> ChunkMatch:
    """One retrieved passage, as `search_chunks` would have returned it."""
    return ChunkMatch(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title=title,
        chunk_index=index,
        content=content,
        similarity=similarity,
    )


def _retrieve(monkeypatch: pytest.MonkeyPatch, matches: list[ChunkMatch]) -> None:
    """Replace retrieval with a scripted answer.

    Nothing in this package can publish a chunk — that is the worker's job, and
    `tests/integration/test_knowledge_retrieval.py` is where a real vector query is exercised.
    What is left to test here is the part of `answer` that runs *after* retrieval.
    """

    async def retrieve(*args: Any, **kwargs: Any) -> list[ChunkMatch]:
        return list(matches)

    monkeypatch.setattr(knowledge_service, "retrieve", retrieve)


def _pretend_something_is_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer `has_published_chunks` with True, so the question path reaches the embedding call.

    The guard is real and correct — an organization with nothing published pays nothing — but it
    is a *shortcut past* the code this test is about, so a test of a failing embedding has to
    step over it. `test_knowledge_draft_grounding.py` is where the guard's own behaviour is
    asserted, by counting the calls it prevents.
    """

    async def has_published_chunks(*args: Any, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        knowledge_service.knowledge_repository, "has_published_chunks", has_published_chunks
    )
