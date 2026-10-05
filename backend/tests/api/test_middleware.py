"""Request-correlation and security-header tests.

Both middlewares are pure ASGI and registered on the real app, so these drive them through
`TestClient` rather than calling them directly — a test that invoked `RequestIDMiddleware` by
hand would prove the class works and say nothing about whether `create_app` wires it.
"""

import pytest
import structlog
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from app.core.middleware import (
    REQUEST_ID_HEADER,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)

# Every header `SecurityHeadersMiddleware` sets in a non-production environment. Named here
# rather than imported, so a header silently dropped from the middleware fails this test
# instead of shrinking the assertion along with it.
_EXPECTED_HEADERS = (
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Cross-Origin-Opener-Policy",
)


@pytest.mark.unit
def test_a_request_without_an_id_gets_one(client: TestClient) -> None:
    """Every response carries an id, generated when the client did not supply one."""
    response = client.get("/health")

    assert response.headers[REQUEST_ID_HEADER]


@pytest.mark.unit
def test_a_client_supplied_id_is_honoured(client: TestClient) -> None:
    """An upstream proxy's id survives this hop.

    Minting a fresh id here would sever the trace at exactly the boundary correlation exists to
    cross: a gateway that assigned an id and a service that ignored it are two half-traces of
    one request, joinable by nothing.
    """
    response = client.get("/health", headers={REQUEST_ID_HEADER: "trace-from-the-gateway"})

    assert response.headers[REQUEST_ID_HEADER] == "trace-from-the-gateway"


@pytest.mark.unit
def test_an_absurd_id_is_replaced_rather_than_truncated(client: TestClient) -> None:
    """A 4 KB id becomes a fresh one, not its first 64 characters.

    Truncating would produce a *different* id that looks like a real one, and two clients whose
    ids share a prefix would then be indistinguishable in the log. The bound exists because the
    id is written onto every line a request logs.
    """
    response = client.get("/health", headers={REQUEST_ID_HEADER: "x" * 4096})

    returned = response.headers[REQUEST_ID_HEADER]
    assert returned != "x" * 4096
    assert returned != "x" * 64
    assert len(returned) == 32  # uuid4().hex


@pytest.mark.unit
def test_every_response_carries_the_security_headers(client: TestClient) -> None:
    """Including error responses, which is why this is middleware and not a response model.

    A 404, a 401, and a 500 all leave through different handlers. Wrapping `send` covers all of
    them; decorating routes would cover the ones somebody remembered.
    """
    for path in ("/health", "/api/v1/nope"):
        response = client.get(path)

        for header in _EXPECTED_HEADERS:
            assert header in response.headers, f"{header} missing on {path}"

    assert client.get("/health").headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/health").headers["X-Frame-Options"] == "DENY"


@pytest.mark.unit
def test_hsts_is_absent_outside_production(client: TestClient) -> None:
    """The suite runs as `ENVIRONMENT=test`, which is served over plain HTTP.

    Pinning a browser to HTTPS for an origin that cannot serve HTTPS breaks the next request
    with an error naming neither HSTS nor the setting responsible. `Strict-Transport-Security`
    is the one header here that is a commitment rather than a restriction, so it is the one
    that must not be sent speculatively.
    """
    assert "Strict-Transport-Security" not in client.get("/health").headers


@pytest.mark.unit
def test_hsts_is_sent_when_the_middleware_is_told_to(client: TestClient) -> None:
    """The production branch, tested directly rather than by rebuilding the whole app.

    `client` is assembled with this suite's `ENVIRONMENT=test`, and constructing a second FastAPI
    app under a production environment would drag settings, database, and Redis into a test about
    one header. Wrapping a two-line ASGI app in the middleware is the same code path with none of
    that, and it is the half the test above cannot reach.
    """
    app = SecurityHeadersMiddleware(_plain_app, include_hsts=True)

    response = TestClient(app).get("/")

    assert response.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"


@pytest.mark.unit
def test_a_route_that_set_a_header_keeps_its_value(client: TestClient) -> None:
    """The middleware fills gaps and does not overrule a route.

    The attachment download sets `X-Content-Type-Options` itself, with a comment explaining why.
    A middleware that overwrote it would make that comment describe a line that no longer decides
    anything — and the next person to change the download's headers would edit the wrong file.
    """
    app = SecurityHeadersMiddleware(_nosniff_app, include_hsts=False)

    response = TestClient(app).get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff; route-wins"


async def _plain_app(scope: Scope, receive: Receive, send: Send) -> None:
    """A minimal ASGI app: one 200 with no headers of its own."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _nosniff_app(scope: Scope, receive: Receive, send: Send) -> None:
    """A route that has already decided its own `nosniff` value."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"x-content-type-options", b"nosniff; route-wins")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.unit
def test_a_refusal_carries_the_id_in_the_header_and_not_in_the_body(client: TestClient) -> None:
    """The correlation id is a response header; the error body stays a function of the error.

    **This test previously asserted the opposite, and the full suite is what changed it.** Phase Y
    put `request_id` in the §42 envelope so a user could quote it from a rendered error. Eighteen
    isolation tests then failed: they assert that a cross-tenant refusal is indistinguishable from
    one for a record that never existed, by comparing the two bodies, and a per-request random
    value makes that comparison false. It proves nothing — both responses carry a random string —
    but it does mean the property is held by eighteen test authors remembering to strip a field
    rather than by the code.

    So the id is only where a correlation id belongs, on `X-Request-ID`, which every response
    already carries and which the smoke test checks as deployed. This test pins both halves: the
    header present, and the body free of it.
    """
    response = client.get("/api/v1/nope")

    assert response.status_code == 404
    assert response.headers[REQUEST_ID_HEADER]
    assert "request_id" not in response.json()["error"]


@pytest.mark.unit
def test_the_id_is_bound_to_the_logs_the_request_writes() -> None:
    """The id reaches the log lines, and that is a separate mechanism from the response header.

    **This test exists because its absence was found by breaking the code.** Deleting the
    `bound_contextvars` call from `RequestIDMiddleware` — removing the whole reason the middleware
    exists, since an id in a response header correlates nothing — left every other test in this
    file passing. The header is written by `send_with_request_id`, separately from the binding, so
    nothing here noticed the binding was gone.

    Asserted against `get_contextvars` from inside the app rather than by capturing rendered log
    output, because the log path is a chain and this is one link of it: `merge_contextvars` is the
    processor that copies whatever is bound onto the event dict, so what this middleware owes is
    that the *binding* is present for the duration of the request. Capturing output would also mean
    reconfiguring structlog, and `structlog.testing.capture_logs` replaces the processor list —
    which would remove `merge_contextvars` and make the assertion pass without testing anything.

    The direct ASGI app is deliberate: this is about what the middleware binds, not about whether
    `create_app` registers it, which the tests above already establish.
    """
    seen: list[dict[str, object]] = []

    async def _app_that_records_the_context(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(dict(structlog.contextvars.get_contextvars()))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = RequestIDMiddleware(_app_that_records_the_context)
    TestClient(app).get("/", headers={REQUEST_ID_HEADER: "trace-from-the-gateway"})

    assert seen == [{"request_id": "trace-from-the-gateway"}]
