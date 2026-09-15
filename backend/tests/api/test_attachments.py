"""Attachments — validation, storage, and who may read a file back.

Spec §33's flow is `Frontend -> FastAPI -> validated upload -> object storage ->
database metadata`, and this file exercises every arrow of it against the real stack:
real MinIO, real Postgres, real HTTP. Nothing here mocks storage, because the claim
being tested is that a file which goes up comes back down byte-identical only for
someone entitled to it — and a mocked store would make that claim about the mock.

The three rules that would be cheapest to break, named so a regression is legible:

* **The stored key contains no client text.** Traversal is prevented by construction,
  not by sanitizing, and this is where that is checked rather than asserted in a comment.
* **A file on an internal note is internal.** The customer who owns the ticket holds
  `ATTACHMENT_DOWNLOAD` and reaches the ticket, so the ticket's row scope is not enough
  to stop them. That is the one rule an attachment adds over a ticket.
* **An unreachable attachment is 404, never 403.** The same body bytes as a genuinely
  absent id, because a distinguishable refusal confirms a file the caller was never
  meant to know about (ADR-009).
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.conftest import API, PASSWORD, TICKETS, OrgSession, login

pytestmark = pytest.mark.integration

ATTACHMENTS = f"{API}/tickets"


def upload_path(ticket_id: str) -> str:
    """The upload and list route for one ticket."""
    return f"{ATTACHMENTS}/{ticket_id}/attachments"


def download_path(attachment_id: str) -> str:
    """The download route. Under `/attachments`, because a download has only an id."""
    return f"{API}/attachments/{attachment_id}"


# The real leading bytes of each accepted type, plus filler. Nothing past the signature
# is inspected, so a valid file needs no binary fixture on disk.
PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"payload bytes" * 4
PDF_BODY = b"%PDF-1.4\n" + b"synthetic" * 4
TEXT_BODY = b"2026-09-15 ERROR the observatory is offline\n"


def png(name: str = "screenshot.png") -> dict[str, Any]:
    """A `files=` payload for a valid PNG."""
    return {"file": (name, PNG_BODY, "image/png")}


def upload(
    session: OrgSession,
    ticket_id: str,
    *,
    files: dict[str, Any] | None = None,
    message_id: str | None = None,
) -> Any:
    """POST an attachment, with the optional form field wired the way the route reads it."""
    data = {"message_id": message_id} if message_id is not None else None
    return session.post(upload_path(ticket_id), files=files or png(), data=data)


# Derived from the body rather than written as a number, so the "over the limit" case
# stays over the limit if `PNG_BODY` ever changes size.
OVERSIZED_LIMIT = len(PNG_BODY) - 1


def stored_key(engine: Engine, attachment_id: str) -> str:
    """Read an attachment's `storage_key` straight from the row.

    The one place these tests look at the database rather than at a response. The key is
    *deliberately* absent from every response — that is the design — so the only way to
    assert what was stored is to ask the table. `sync_engine` is the established
    mechanism for HTTP-test bookkeeping; see its docstring.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT storage_key FROM attachments WHERE id = :id"), {"id": attachment_id}
        ).one_or_none()

    assert row is not None, "the attachment row was not written"
    return str(row[0])


def events(session: OrgSession, ticket_id: str) -> list[dict[str, Any]]:
    """A ticket's timeline."""
    response = session.get(f"{TICKETS}/{ticket_id}/events")
    assert response.status_code == 200, response.text
    return list(response.json())


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def test_an_uploaded_file_is_attributed_to_its_ticket_and_uploader(
    client: TestClient, register_org: Any
) -> None:
    """The metadata row §33 asks for, and the three fields it deliberately omits."""
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    response = upload(org, ticket["id"])

    assert response.status_code == 201, response.text
    body = response.json()

    assert body["ticket_id"] == ticket["id"]
    assert body["uploaded_by_id"] == org.user_id
    assert body["message_id"] is None, "a ticket-level upload names no message"
    assert body["filename"] == "screenshot.png"
    assert body["content_type"] == "image/png"
    assert body["size_bytes"] == len(PNG_BODY)


def test_the_response_never_carries_the_storage_key(client: TestClient, register_org: Any) -> None:
    """The key is an internal name for a private object.

    A client that never sees it cannot ask storage for it directly, which is what makes
    this API the only path to a file rather than one of two.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    created = upload(org, ticket["id"]).json()
    listed = org.get(upload_path(ticket["id"])).json()
    downloaded = org.get(download_path(created["id"]))

    assert "storage_key" not in created
    assert "storage_key" not in listed[0]
    assert "storage_key" not in downloaded.headers
    assert b"storage_key" not in downloaded.content


def test_the_stored_key_is_composed_from_server_side_values_only(
    client: TestClient, register_org: Any, sync_engine: Engine
) -> None:
    """§33's "prevent path traversal", satisfied by construction rather than by a filter.

    The key is `{organization_id}/{ticket_id}/{uuid4().hex}`. The client's filename never
    reaches it, so there is no traversal to sanitize — which is why this asserts the
    *shape* of the key rather than searching it for `..`. A key that cannot contain a
    client string cannot contain a client's `..`.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    attachment_id = upload(org, ticket["id"]).json()["id"]
    key = stored_key(sync_engine, attachment_id)

    organization_id, ticket_id, suffix = key.split("/")

    assert uuid.UUID(organization_id)
    assert ticket_id == ticket["id"]
    assert len(suffix) == 32, "uuid4().hex, so no characters need escaping in a path"
    assert ".." not in key
    assert "screenshot" not in key, "the client's filename is nowhere in the key"


def test_two_uploads_of_the_same_file_get_distinct_keys(
    client: TestClient, register_org: Any, sync_engine: Engine
) -> None:
    """Uniqueness without a round trip to check for a collision."""
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    first = upload(org, ticket["id"]).json()["id"]
    second = upload(org, ticket["id"]).json()["id"]

    assert stored_key(sync_engine, first) != stored_key(sync_engine, second)


def test_a_traversal_shaped_name_is_displayed_without_its_directory(
    client: TestClient, register_org: Any, sync_engine: Engine
) -> None:
    """`../../shot.png` is **accepted**, and that is the correct answer.

    Every signal the validator can check is consistent: the extension is allowed, the
    declared type agrees, and the bytes are a real PNG. Refusing it would refuse a
    legitimate file that happens to be named oddly by a client SDK.

    What makes it safe is elsewhere, and this asserts both halves: the stored key has no
    directory part because it was never derived from the name, and the *display* name is
    reduced to its final component so a browser saving the download cannot act on a
    traversal it was handed in a header.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    response = upload(org, ticket["id"], files=png("../../shot.png"))

    assert response.status_code == 201, response.text
    attachment_id = response.json()["id"]

    assert response.json()["filename"] == "shot.png"
    assert ".." not in stored_key(sync_engine, attachment_id)


@pytest.mark.parametrize(
    ("files", "expected_code"),
    [
        # An executable renamed to .png and announced as image/png. The name and the
        # declared type both pass; only reading the bytes tells the truth.
        ({"file": ("shot.png", b"MZ\x90\x00", "image/png")}, "UNSUPPORTED_FILE_TYPE"),
        # An extension outside the allowlist.
        (
            {"file": ("payload.exe", b"MZ\x90\x00", "application/octet-stream")},
            "UNSUPPORTED_FILE_TYPE",
        ),
        # A type that disagrees with its own extension.
        ({"file": ("shot.png", PNG_BODY, "application/pdf")}, "UNSUPPORTED_FILE_TYPE"),
        # A PDF that does not begin with the PDF signature.
        ({"file": ("report.pdf", b"not a pdf", "application/pdf")}, "UNSUPPORTED_FILE_TYPE"),
        # Nothing at all. No signature to read, and the column requires a positive size.
        ({"file": ("empty.png", b"", "image/png")}, "UNSUPPORTED_FILE_TYPE"),
    ],
)
def test_an_upload_that_fails_validation_is_refused_with_one_opaque_answer(
    client: TestClient, register_org: Any, files: dict[str, Any], expected_code: str
) -> None:
    """One error code for every failure, and that is the point.

    Which of the three signals disagreed is a map of what the validator accepts, and a
    legitimate caller has no use for it. The specific reason goes to the log; the client
    gets the same 422 whatever it did wrong.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    response = upload(org, ticket["id"], files=files)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == expected_code


def test_a_refused_upload_stores_nothing(client: TestClient, register_org: Any) -> None:
    """Validation runs before the object is written, so a refusal leaves no trace.

    Asserted on the listing rather than on the bucket: the row is what a caller could
    ever observe, and an orphaned object is invisible by design (see the module's note
    on ordering in `create_attachment`).
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    upload(org, ticket["id"], files={"file": ("shot.png", b"MZ\x90\x00", "image/png")})

    assert org.get(upload_path(ticket["id"])).json() == []


def test_an_oversized_upload_is_refused(
    client: TestClient, register_org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit is enforced by counting the stream, not by trusting `Content-Length`.

    `Content-Length` is a header, and a header is a claim — a client that understates it
    would otherwise get an unbounded write, and the one place the size genuinely matters
    is the one place a client has an incentive to lie. `_measure`'s signature takes only
    the upload, so there is no header for it to consult even by accident; this test
    proves the wiring reaches it by driving a real request past a lowered limit.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    monkeypatch.setattr(
        "app.services.attachment_service.get_settings",
        lambda: SimpleNamespace(MAX_ATTACHMENT_BYTES=OVERSIZED_LIMIT),
    )

    response = upload(org, ticket["id"])

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_the_size_limit_is_not_read_from_a_header(
    client: TestClient, register_org: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same claim from the other side: a small declared length changes nothing.

    The body is what decides. A `Content-Length` far below what is actually sent buys
    the client nothing, because the server counted the bytes it received.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    monkeypatch.setattr(
        "app.services.attachment_service.get_settings",
        lambda: SimpleNamespace(MAX_ATTACHMENT_BYTES=OVERSIZED_LIMIT),
    )

    response = org.post(
        upload_path(ticket["id"]),
        files=png(),
        headers={"Content-Length": "12"},
    )

    assert response.status_code == 413, response.text


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_an_unauthenticated_upload_is_refused(client: TestClient, register_org: Any) -> None:
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    response = client.post(upload_path(ticket["id"]), files=png())

    assert response.status_code == 401, response.text


def test_an_unauthenticated_download_is_refused(client: TestClient, register_org: Any) -> None:
    """Every byte is served by this process, so the credential is checked on every read.

    This is the property a presigned URL would have given away: a URL grants access to
    whoever holds it, and authorization is a decision made per request against an
    authenticated identity.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])
    attachment_id = upload(org, ticket["id"]).json()["id"]

    response = client.get(download_path(attachment_id))

    assert response.status_code == 401, response.text


def test_an_agent_cannot_attach_to_a_colleagues_ticket(
    client: TestClient, register_org: Any
) -> None:
    """A 404, not a 403 — and the capability is not what denies it.

    Every role holds `ATTACHMENT_UPLOAD`; what stops this is the ticket's row scope. An
    agent should not be able to size the desk's workload by watching which ids are
    refused and which are not.
    """
    org = register_org()
    agent = org.add_user("agent")
    colleague = org.add_user("agent")
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])
    # Assign to the colleague, so the ticket is real and reachable by *someone*.
    org.post(f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": colleague.user_id})

    response = upload(agent, ticket["id"])

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "TICKET_NOT_FOUND"


def test_a_customer_can_attach_to_their_own_ticket(client: TestClient, register_org: Any) -> None:
    """`RowScope.OWN`, resolved through the user's customer link."""
    org = register_org()
    customer = org.add_customer()
    portal = org.add_portal_user(customer["id"])
    ticket = org.add_ticket(customer["id"])

    response = upload(portal, ticket["id"])

    assert response.status_code == 201, response.text


def test_a_customer_cannot_attach_to_another_customers_ticket(
    client: TestClient, register_org: Any
) -> None:
    org = register_org()
    mine = org.add_customer()
    theirs = org.add_customer(name="Someone Else", email="else@example.com")
    portal = org.add_portal_user(mine["id"])
    other_ticket = org.add_ticket(theirs["id"])

    response = upload(portal, other_ticket["id"])

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_a_ticket_lists_its_attachments_oldest_first(client: TestClient, register_org: Any) -> None:
    """Ascending, matching the conversation the files sit under."""
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    names = ["first.png", "second.png", "third.png"]
    for name in names:
        assert upload(org, ticket["id"], files=png(name)).status_code == 201

    listed = org.get(upload_path(ticket["id"])).json()

    assert [row["filename"] for row in listed] == names


def test_listing_requires_the_ticket_to_be_reachable(client: TestClient, register_org: Any) -> None:
    """404 rather than an empty page: "you cannot see this ticket" and "this ticket has
    no files" are different facts, and an empty list would report the second."""
    org = register_org()
    agent = org.add_user("agent")
    colleague = org.add_user("agent")
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])
    org.post(f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": colleague.user_id})

    response = agent.get(upload_path(ticket["id"]))

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def test_a_download_returns_the_stored_bytes(client: TestClient, register_org: Any) -> None:
    """The round trip, byte for byte. This is the claim the whole phase exists for."""
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])
    attachment_id = upload(org, ticket["id"]).json()["id"]

    response = org.get(download_path(attachment_id))

    assert response.status_code == 200, response.text
    assert response.content == PNG_BODY


@pytest.mark.parametrize(
    ("filename", "body", "declared", "expected_type"),
    [
        ("screenshot.png", PNG_BODY, "image/png", "image/png"),
        ("report.pdf", PDF_BODY, "application/pdf", "application/pdf"),
        ("server.log", TEXT_BODY, "text/plain", "text/plain"),
    ],
)
def test_a_download_carries_the_headers_that_make_it_safe(
    client: TestClient,
    register_org: Any,
    filename: str,
    body: bytes,
    declared: str,
    expected_type: str,
) -> None:
    """Three headers carry the security of this response, and all three are asserted.

    * `Content-Disposition: attachment` — the browser downloads rather than renders. An
      inline response would ask it to interpret a file a customer supplied, in the same
      origin the refresh cookie is scoped to.
    * `X-Content-Type-Options: nosniff` — the browser must not override the type with its
      own guess, which is exactly the case a client-chosen `Content-Type` creates.
    * `Content-Type` is the **detected** type, established from the bytes at upload time.

    The media type is compared separately from any parameters: Starlette appends
    `charset=utf-8` to a `text/*` media type on its own, which is standard and harmless
    on a response the browser is being told to download rather than render. What matters
    is that the type is the detected one, and that is the half asserted.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])
    created = upload(org, ticket["id"], files={"file": (filename, body, declared)}).json()

    response = org.get(download_path(created["id"]))
    media_type = response.headers["content-type"].split(";")[0].strip()

    assert response.status_code == 200, response.text
    assert media_type == expected_type
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["content-length"] == str(len(body))


def test_a_download_uses_the_sanitized_display_name(client: TestClient, register_org: Any) -> None:
    """The name in the header is the cleaned one, not the one the client sent."""
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])
    created = upload(org, ticket["id"], files=png("C:\\Users\\me\\../../shot.png")).json()

    response = org.get(download_path(created["id"]))

    assert 'filename="shot.png"' in response.headers["content-disposition"]
    assert ".." not in response.headers["content-disposition"]


def test_a_cross_tenant_download_is_the_same_404_as_a_missing_one(
    client: TestClient, register_org: Any
) -> None:
    """Byte-identical, not merely the same status code.

    A refusal that differs in any observable way — a different message, a different
    code, a different length — confirms that the row exists. Comparing the whole body
    is what makes "indistinguishable" a fact rather than an intention (ADR-009).
    """
    org = register_org()
    outsider = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])
    attachment_id = upload(org, ticket["id"]).json()["id"]

    theirs = outsider.get(download_path(attachment_id))
    missing = outsider.get(download_path(str(uuid.uuid4())))

    assert theirs.status_code == 404, theirs.text
    assert theirs.content == missing.content


def test_an_agent_cannot_download_a_colleagues_tickets_file(
    client: TestClient, register_org: Any
) -> None:
    """The row is theirs to see in the tenant; the file is not.

    The attachment row carries no `assigned_agent_id` of its own — its owner is its
    ticket — so this is the case that proves the ticket is resolved before the bytes
    are, and that the difference is not observable.
    """
    org = register_org()
    agent = org.add_user("agent")
    colleague = org.add_user("agent")
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])
    attachment_id = upload(org, ticket["id"]).json()["id"]
    org.post(f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": colleague.user_id})

    response = agent.get(download_path(attachment_id))

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Internal notes — the one rule an attachment adds over a ticket
# ---------------------------------------------------------------------------


def _ticket_with_internal_note(org: OrgSession) -> tuple[dict[str, Any], str]:
    """A ticket plus an internal note on it. Returns the ticket and the note's id.

    Authored by the admin, so this can run before any agent is involved — an agent's
    scope is `ASSIGNED`, and a note written before the assignment would 404. Tests that
    need an agent attach afterwards, once the ticket is theirs.
    """
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    response = org.post(f"{TICKETS}/{ticket['id']}/notes", json={"body": "Skinner knows"})
    assert response.status_code == 201, response.text

    return ticket, response.json()["id"]


def test_a_file_on_an_internal_note_is_hidden_from_the_customer(
    client: TestClient, register_org: Any
) -> None:
    """The divergence ADR-013 predicted, reached as a row rule rather than a role rule.

    A customer holds `ATTACHMENT_DOWNLOAD` and their own ticket is in scope, so nothing
    in §3's matrix stops them at a file attached to a note they cannot read. The rule is
    that a file is reachable exactly when the message it arrived with is.
    """
    org = register_org()
    customer = org.add_customer()
    portal = org.add_portal_user(customer["id"])
    ticket = org.add_ticket(customer["id"])
    note = org.post(f"{TICKETS}/{ticket['id']}/notes", json={"body": "Skinner knows"}).json()
    attachment_id = upload(org, ticket["id"], message_id=note["id"]).json()["id"]

    # Absent from the list, rather than listed and refused on download: a metadata list
    # naming a file the caller cannot open is itself a hint about a private conversation.
    assert org.get(upload_path(ticket["id"])).json() != []
    assert portal.get(upload_path(ticket["id"])).json() == []

    refused = portal.get(download_path(attachment_id))
    missing = portal.get(download_path(str(uuid.uuid4())))

    assert refused.status_code == 404, refused.text
    assert refused.content == missing.content, "byte-identical to an id that never existed"


def test_a_file_on_an_internal_note_is_visible_to_staff(
    client: TestClient, register_org: Any
) -> None:
    """The other half: hiding it from the customer must not hide it from the desk.

    An agent attaches to the admin's note, which is the case the rule most needs to
    allow — the note is internal, the agent may read internal notes, and the file is
    part of the work being discussed. The admin sees it too, from the other side of the
    scope.
    """
    org = register_org()
    agent = org.add_user("agent")
    ticket, note_id = _ticket_with_internal_note(org)
    org.post(f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": agent.user_id})

    created = upload(agent, ticket["id"], message_id=note_id).json()

    assert [row["id"] for row in agent.get(upload_path(ticket["id"])).json()] == [created["id"]]
    assert agent.get(download_path(created["id"])).status_code == 200
    # And the admin, whose scope is the whole organization.
    assert org.get(download_path(created["id"])).status_code == 200
    assert upload(org, ticket["id"], message_id=note_id).status_code == 201


def test_a_customer_cannot_attach_to_a_note_they_cannot_read(
    client: TestClient, register_org: Any
) -> None:
    """Attaching is only meaningful on a note you can see.

    Refused as a 404 for the note, which is the same answer a note on another ticket
    gets — the caller may not learn that the note exists by trying to attach to it.
    """
    org = register_org()
    customer = org.add_customer()
    portal = org.add_portal_user(customer["id"])
    ticket = org.add_ticket(customer["id"])
    note = org.post(f"{TICKETS}/{ticket['id']}/notes", json={"body": "Skinner knows"}).json()

    response = upload(portal, ticket["id"], message_id=note["id"])

    assert response.status_code == 404, response.text


def test_an_upload_cannot_name_a_message_on_another_ticket(
    client: TestClient, register_org: Any
) -> None:
    """Scoped to the ticket in the path, so a message id from elsewhere is not found."""
    org = register_org()
    customer = org.add_customer()
    first = org.add_ticket(customer["id"])
    second = org.add_ticket(customer["id"], subject="A second problem")
    reply = org.post(f"{TICKETS}/{first['id']}/messages", json={"body": "Mulder"}).json()

    response = upload(org, second["id"], message_id=reply["id"])

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# The timeline
# ---------------------------------------------------------------------------


def test_a_ticket_level_upload_appears_on_the_timeline(
    client: TestClient, register_org: Any
) -> None:
    """§33's fourth arrow: a file on a ticket is part of what happened to it."""
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    upload(org, ticket["id"], files=png("diagram.png"))

    added = [
        event for event in events(org, ticket["id"]) if event["event_type"] == "attachment_added"
    ]
    assert len(added) == 1
    # The filename lives in `extra_data`, not in `to_value` — that column is String(100)
    # and its renderer reads it as "the value this changed to".
    assert added[0]["extra_data"]["filename"] == "diagram.png"
    assert added[0]["actor_user_id"] == org.user_id


def test_an_internal_note_upload_writes_no_timeline_entry(
    client: TestClient, register_org: Any
) -> None:
    """The note's own `INTERNAL_NOTE_ADDED` entry already covers it.

    And the customer reads the timeline of their own ticket. An `ATTACHMENT_ADDED` entry
    there would put a client-supplied filename on a page they can see, describing a file
    they cannot download — a hint about a conversation they are not part of.
    """
    org = register_org()
    customer = org.add_customer()
    portal = org.add_portal_user(customer["id"])
    ticket = org.add_ticket(customer["id"])
    note = org.post(f"{TICKETS}/{ticket['id']}/notes", json={"body": "Skinner knows"}).json()

    upload(org, ticket["id"], message_id=note["id"], files=png("evidence.png"))

    types = [event["event_type"] for event in events(org, ticket["id"])]
    assert "attachment_added" not in types
    assert "internal_note_added" in types

    # And nothing about the file reaches the customer's view of the timeline.
    customer_events = portal.get(f"{TICKETS}/{ticket['id']}/events").json()
    assert "evidence.png" not in str(customer_events)


def test_the_customer_sees_a_ticket_level_attachment_on_their_timeline(
    client: TestClient, register_org: Any
) -> None:
    """The contrast that makes the previous test meaningful: a ticket-level file is
    visible to the ticket's audience, because that is who it was sent to."""
    org = register_org()
    customer = org.add_customer()
    portal = org.add_portal_user(customer["id"])
    ticket = org.add_ticket(customer["id"])

    upload(org, ticket["id"], files=png("diagram.png"))

    customer_events = portal.get(f"{TICKETS}/{ticket['id']}/events").json()
    assert "diagram.png" in str(customer_events)


# ---------------------------------------------------------------------------
# Isolation between organizations
# ---------------------------------------------------------------------------


def test_an_organization_cannot_attach_to_another_organizations_ticket(
    client: TestClient, register_org: Any
) -> None:
    """The ticket id is real; the tenant filter is what makes it absent.

    Byte-identical to a random id, so the response does not confirm that the ticket
    exists in some other organization.
    """
    org = register_org()
    outsider = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    theirs = upload(outsider, ticket["id"])
    missing = upload(outsider, str(uuid.uuid4()))

    assert theirs.status_code == 404, theirs.text
    assert theirs.content == missing.content


def test_an_organization_lists_nothing_for_another_organizations_ticket(
    client: TestClient, register_org: Any
) -> None:
    org = register_org()
    outsider = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])
    upload(org, ticket["id"])

    assert outsider.get(upload_path(ticket["id"])).status_code == 404


def test_login_is_needed_to_reach_any_of_it(client: TestClient, register_org: Any) -> None:
    """A guard against the whole file passing for the wrong reason.

    If every request above were being refused for an unrelated reason — a broken token,
    a misconfigured client — the 404s would still look correct. This asserts the same
    session can do the same thing successfully.
    """
    org = register_org()
    customer = org.add_customer()
    ticket = org.add_ticket(customer["id"])

    fresh = login(client, org.email, PASSWORD)
    assert upload(fresh, ticket["id"]).status_code == 201
    assert fresh.get(upload_path(ticket["id"])).status_code == 200
