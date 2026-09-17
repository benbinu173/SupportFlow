"""The real-time socket: one route, `/ws`, and the auth frame that opens it.

**A WebSocket route is not an HTTP route, and almost nothing above this module applies to
it.** There is no response body, so `app/main.py`'s §42 error envelope has no surface here
and the four exception handlers registered there are unreachable from a socket scope — a
`raise` inside this handler escapes into middleware that expects a response and produces a
500 nobody can read. There is no dependency tree to speak of either: `Depends` works, but
the one dependency worth having — "who is calling?" — cannot be answered at handshake time,
because a browser is not allowed to set an `Authorization` header on a `WebSocket`
handshake. So the credential arrives in a frame *after* the socket is open, this module
authenticates it by calling into `app/api/deps.py` rather than restating its rules, and every
refusal is expressed as a close code.

**Why not a query string.** `?token=...` is the common answer and it is the wrong one here.
uvicorn logs the full request line of a handshake, so a live fifteen-minute access token
would be written to stdout on every connect and every reconnect — and
`tests/security/test_log_hygiene.py` **would not catch it**, because that suite records
structlog calls and uvicorn does not log through structlog. A credential that leaks into a
log the project's own leakage test cannot see is worse than one that leaks into a log it
can. The subprotocol header was the third option and loses for a smaller reason: it abuses a
negotiation mechanism to carry a secret, and would make `token` part of what a proxy sees in
a header it is entitled to log.

**The connection is unauthenticated for at most `WS_AUTH_TIMEOUT_SECONDS`,** and it is not
in the registry for any part of that window. See ADR-025.
"""

import asyncio
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import authenticate_token, tenant_context_for
from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import AppError
from app.core.permissions import Permission
from app.core.tenancy import TenantContext
from app.websocket import manager
from app.websocket.events import CloseCode

logger = structlog.get_logger(__name__)

router = APIRouter()


class AuthFrame(BaseModel):
    """The first message a client must send.

    A model rather than `payload["token"]`, because §4's "validate all input" applies to a
    socket frame exactly as it applies to a request body — and because `receive_json` raises
    on bytes that are not JSON at all, which is a refusal path and not a crash.
    """

    type: str = "auth"
    token: str


# The capabilities this channel's contents require. Every role holds both — §3 gives
# `TICKET_VIEW` to all four and `NOTIFICATION_LIST` to all four — so this is a statement of
# what the socket *carries*, not a narrowing of who may hold one.
#
# It is checked anyway, and the check is the point: a role added in a later phase, or an
# existing role edited to drop one of these, would otherwise keep a socket that delivers
# content its holder is no longer allowed to read, and nothing else in the codebase would
# notice. `tests/unit/test_realtime_events.py` asserts every role holds both, so the day
# that stops being true a test says so rather than a socket quietly leaking.
REQUIRED_CAPABILITIES = (Permission.TICKET_VIEW, Permission.NOTIFICATION_LIST)


async def _refuse(websocket: WebSocket, code: CloseCode) -> None:
    """Close an unauthenticated connection with a reason.

    Deliberately sends nothing first. A client's `onclose` is where this is handled, and
    adding a JSON body would create a second refusal format that only exists on the socket.
    """
    logger.info("realtime_refused", close_code=int(code))
    await websocket.close(code=int(code))


async def _authenticate(websocket: WebSocket, db: AsyncSession) -> TenantContext | None:
    """Read the auth frame and turn it into a context, or refuse and return `None`.

    The order is the security property, not an implementation detail: the timeout is
    enforced by `wait_for` around the *receive*, so an idle socket that never sends anything
    is closed rather than held open indefinitely. Every failure below — no frame, unreadable
    frame, wrong frame, bad token — closes the connection, and none of them distinguishes
    itself to the caller beyond the code. A socket that said "that token is expired" and a
    socket that said "that token is for another tenant" would be an oracle.
    """
    timeout = get_settings().WS_AUTH_TIMEOUT_SECONDS
    try:
        raw = await asyncio.wait_for(websocket.receive_json(), timeout=timeout)
        frame = AuthFrame.model_validate(raw)
    except TimeoutError:
        await _refuse(websocket, CloseCode.AUTH_TIMEOUT)
        return None
    except (WebSocketDisconnect, ValidationError, ValueError):
        # `ValidationError` covers a well-formed JSON body that is not an auth frame;
        # `ValueError` covers bytes that are not JSON at all, which Starlette surfaces from
        # `receive_json`. Both are the same answer to the client.
        await _refuse(websocket, CloseCode.UNAUTHENTICATED)
        return None

    try:
        user = await authenticate_token(db, frame.token)
    except AppError as exc:
        # `AppError` and not a bare `Exception`: the five checks in `deps.py` raise their own
        # domain errors (`AuthenticationRequiredError`, `InactiveUserError`,
        # `TenantAccessDeniedError`), and anything else escaping here is a bug that should
        # reach the unhandled-exception logger rather than be reported as a bad credential.
        logger.info("realtime_auth_failed", error_code=str(exc.code))
        await _refuse(websocket, CloseCode.UNAUTHENTICATED)
        return None

    context = tenant_context_for(user)
    if not context.has(*REQUIRED_CAPABILITIES):
        logger.info(
            "realtime_forbidden",
            user_id=str(context.user_id),
            organization_id=str(context.organization_id),
            role=context.role.value,
        )
        await _refuse(websocket, CloseCode.FORBIDDEN)
        return None

    return context


@router.websocket("/ws")
async def realtime_socket(
    websocket: WebSocket,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Hold one client's real-time connection.

    `accept()` before authentication is a deliberate trade and the only ordering available:
    the alternative is refusing the handshake, which a browser reports as an opaque failure
    with no close code, so the client could not tell "log in again" from "the server is
    down". Accepting first buys a code the client can act on, and costs a window of at most
    `WS_AUTH_TIMEOUT_SECONDS` during which the socket is open, absent from the registry, and
    subscribed to nothing — there is no code path below that reaches a queue before
    `context` exists.

    `ws.protocol` on the uvicorn side already answers ping/pong, so there is no application
    heartbeat here: a dead peer is detected one layer down and this handler observes the
    disconnect through `receive`. Adding a second liveness mechanism above a working one
    would give a connection two ways to be declared dead and two sets of timing to tune.
    """
    await websocket.accept()

    context = await _authenticate(websocket, db)
    if context is None:
        return

    # The client's cue that it may start trusting the socket. Sent before registration so a
    # client cannot act on an acknowledgement for a connection the server has not yet
    # finished setting up.
    await websocket.send_json({"type": "authenticated"})
    await manager.serve(websocket, context)
