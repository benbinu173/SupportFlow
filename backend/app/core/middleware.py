"""HTTP middleware: request correlation, and the response headers §54 asks for.

**Both are pure ASGI middleware rather than `BaseHTTPMiddleware` subclasses.** Starlette's
`BaseHTTPMiddleware` runs the application in a child task and buffers the response body to do
it, which is why it famously breaks `StreamingResponse` and why the contextvars it sets take a
detour through a task boundary. This project downloads attachments and holds a WebSocket open;
neither needs that class of bug introduced for two dozen lines of saved code. A raw ASGI
callable wraps `send` and costs nothing.

**Order in `create_app` is deliberate.** Starlette applies middleware in reverse registration
order, so the *last* one added is the *outermost*. The request id is registered last, which
puts it outside the security headers — so the id is bound before any other middleware can log,
and a line written while the headers are being applied already carries it.

**Neither middleware touches WebSocket connections.** `scope["type"] != "http"` is passed
straight through. A socket has no headers to set and no request to correlate; its identity is
the authenticated user, which `app/websocket/manager.py` already binds where it matters.
"""

import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The header a client may send to correlate its own trace with this server's log lines, and the
# header this server echoes it back on. One name, because a proxy that renames it between the
# edge and here is a proxy that has broken the correlation it was added to provide.
REQUEST_ID_HEADER = "X-Request-ID"

# §54's "secure headers where appropriate", each with the thing it actually stops.
#
# This API answers with JSON, a private file download, and a WebSocket upgrade. That shapes
# which headers are worth sending and which are cargo cult:
#
#   * `X-Content-Type-Options: nosniff` — a browser must not sniff an attachment's bytes into a
#     type it was not served as. The attachment route sets this itself, with its own comment;
#     repeating it here is what makes it true of *every* response rather than of the one route
#     somebody remembered. Both setting it is not a conflict — same value.
#   * `X-Frame-Options: DENY` — no page may frame this API. An API in an iframe is either a
#     mistake or a clickjacking attempt aimed at a browser that is already authenticated.
#   * `Referrer-Policy: no-referrer` — this API's URLs contain ticket and document ids, and a
#     leaked `Referer` on an outbound link is how a resource id leaves the building.
#   * `Cross-Origin-Opener-Policy: same-origin` — severs the `window.opener` handle a page that
#     opened this origin would otherwise hold.
#
# No `Content-Security-Policy` here, deliberately: a CSP governs what a *document* may load, and
# none of these responses is a document. The frontend's nginx sets one, where it means something.
_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}

# One year, the value every browser's HSTS preload list requires, and `includeSubDomains` so a
# forgotten `api.` hostname cannot be downgraded to plaintext.
_HSTS = "max-age=31536000; includeSubDomains"


class RequestIDMiddleware:
    """Give every request an id, bind it to the logs, and hand it back in the response.

    **An inbound id is honoured, and that is a considered risk rather than an oversight.** A
    client (or an upstream proxy) may send `X-Request-ID`, and this server adopts it. The
    alternative — always minting — breaks the one thing correlation is for: a load balancer or
    a gateway that already assigned an id would have its trace severed at this hop, and the two
    halves could never be joined. The value is not trusted for anything but grouping log lines,
    so a hostile or absurd one is a cosmetic concern, not a security one.

    What *is* bounded is its length: "not trusted for anything but grouping" stops being true if
    a client can put a megabyte into every log line. Anything longer than 64 characters is
    replaced rather than truncated, because a truncated id is a wrong id that looks right.

    The id is written to `scope["state"]`, which Starlette exposes as `request.state` — so a
    handler that wants to correlate something it writes (an audit record, say) can read
    `request.state.request_id` without this middleware having to guess what it needs. The
    client-facing channel is the `X-Request-ID` response header, and deliberately **not** the §42
    error envelope: see `app/core/exceptions.py` for why a per-request value in that body broke
    the isolation tests' requirement that two refusals be indistinguishable.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _inbound_request_id(scope) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        # `bound_contextvars` rather than `bind_contextvars`/`clear_contextvars`: it restores
        # whatever was bound before, so this middleware cannot wipe context owned by whoever
        # wrapped it. It binds through the same `contextvars` module the `merge_contextvars`
        # processor in `app/core/logging.py` reads, which is what puts the id on every line a
        # request logs without a single call site passing it.
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            await self.app(scope, receive, send_with_request_id)


class SecurityHeadersMiddleware:
    """Attach §54's secure headers to every HTTP response.

    Set through `MutableHeaders` at `http.response.start` rather than rebuilt into a new
    `Response` object, so a streamed body, a `FileResponse`, and an error rendered by an
    exception handler are all covered identically. A middleware that reconstructed responses
    would have to know about all three, and would be wrong about the fourth.

    `Strict-Transport-Security` is **production only**, and that asymmetry is the point. HSTS
    tells a browser never to speak plaintext to this origin again — on a localhost development
    server, which *is* plaintext, it pins the browser to a scheme the server cannot serve and
    the developer's next request fails with an error that names neither HSTS nor the setting
    that caused it. It is also the one header here that is a commitment rather than a
    restriction, and an unconditional one would be a commitment this code could not take back.
    """

    def __init__(self, app: ASGIApp, *, include_hsts: bool) -> None:
        self.app = app
        self._headers: dict[str, str] = dict(_SECURITY_HEADERS)
        if include_hsts:
            self._headers["Strict-Transport-Security"] = _HSTS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self._headers.items():
                    # `setdefault`-style: a route that already set the header keeps its value.
                    # The attachment route sets `nosniff` with a comment explaining why, and a
                    # middleware overwriting it would make that comment describe a line that no
                    # longer decides anything.
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _inbound_request_id(scope: Scope) -> str | None:
    """The client's request id, if it sent a usable one.

    Bounded at 64 characters — a UUID is 36 and a hex uuid is 32, so the ceiling is generous for
    every honest producer and refuses the one case where an id becomes a payload.
    """
    value: str | None = Headers(scope=scope).get(REQUEST_ID_HEADER)
    if value is None:
        return None
    value = value.strip()
    return value if 0 < len(value) <= 64 else None


__all__ = ["REQUEST_ID_HEADER", "RequestIDMiddleware", "SecurityHeadersMiddleware"]
