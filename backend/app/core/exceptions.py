"""Domain exceptions and the error vocabulary (§42).

One exception type per failure mode, each carrying a stable machine-readable `code`
and the HTTP status it should produce. `app/main.py` registers handlers that render
them all in one shape, so no route ever hand-builds an error response:

    {"error": {"code": "USER_NOT_FOUND", "message": "User not found."}}

Reading the HTTP status off the exception is a deliberate coupling. Splitting it into
a domain code plus a separate code→status table in the API layer would buy
framework-independence that nothing here needs, at the cost of a mapping every new
error has to be added to twice.

**A note on `message`.** It is written for the client and must never carry internals —
no SQL, no stack traces, no "user with email x@y.com exists". Anything an attacker
could use to enumerate accounts belongs in the log, not the response.
"""

from enum import StrEnum
from typing import Any

from starlette import status


class ErrorCode(StrEnum):
    """Every code the API can return.

    The specification lists eleven (§42); all eleven are here verbatim. Three are
    added:

    `INVALID_CREDENTIALS` — a failed login is not the same condition as a missing
    token, and a client needs to tell them apart to know whether to retry the request
    or send the user back to the login form. Both remain 401, so the distinction is
    invisible to anyone probing.

    `USER_ALREADY_EXISTS` — the spec's list has no code for a unique-constraint
    violation, which an admin creating a user can genuinely trigger.

    `CUSTOMER_ALREADY_EXISTS` — the same gap on the customers table, whose unique
    constraint is `(organization_id, email)`. Reported distinctly from the user case
    because the two are different forms in a UI and a client should not have to guess
    which record it collided with.

    Codes for phases not yet built (`AI_SERVICE_ERROR` and the rest) are defined now so
    the vocabulary is complete in one place and the frontend can map against it without
    chasing additions.
    """

    # --- Authentication and authorization ---------------------------------
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    FORBIDDEN = "FORBIDDEN"
    TENANT_ACCESS_DENIED = "TENANT_ACCESS_DENIED"

    # --- Validation and conflict -------------------------------------------
    VALIDATION_ERROR = "VALIDATION_ERROR"
    USER_ALREADY_EXISTS = "USER_ALREADY_EXISTS"
    CUSTOMER_ALREADY_EXISTS = "CUSTOMER_ALREADY_EXISTS"

    # --- Not found ---------------------------------------------------------
    USER_NOT_FOUND = "USER_NOT_FOUND"
    CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND"
    TICKET_NOT_FOUND = "TICKET_NOT_FOUND"
    ATTACHMENT_NOT_FOUND = "ATTACHMENT_NOT_FOUND"
    # Carries a second meaning beyond "absent": a notification addressed to a colleague
    # is as unreachable as one that was never written, and this is the code both return.
    NOTIFICATION_NOT_FOUND = "NOTIFICATION_NOT_FOUND"

    # --- Transport-level ---------------------------------------------------
    # Raised by the framework, not by domain code: the *route* does not exist, or the
    # method is wrong. Distinct from the `*_NOT_FOUND` codes above, which mean a real
    # resource is absent. Reporting "no such endpoint" as `USER_NOT_FOUND` would be a
    # lie a client could act on.
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"

    # --- Domain rules ------------------------------------------------------
    INVALID_TICKET_TRANSITION = "INVALID_TICKET_TRANSITION"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"

    # --- Infrastructure ----------------------------------------------------
    RATE_LIMITED = "RATE_LIMITED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    AI_SERVICE_ERROR = "AI_SERVICE_ERROR"
    INTERNAL_SERVER_ERROR = "INTERNAL_SERVER_ERROR"


class AppError(Exception):
    """Base for every error this application raises deliberately.

    Only subclasses of this are rendered as structured errors. Anything else reaching
    the handler is a bug, and becomes an opaque 500.
    """

    code: ErrorCode = ErrorCode.INTERNAL_SERVER_ERROR
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message if message is not None else type(self).message
        # e.g. WWW-Authenticate on 401, Retry-After on 429. Passed through to the
        # response rather than being swallowed.
        self.headers = headers
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.code} {self.status_code}>"


# ---------------------------------------------------------------------------
# 401 — no valid identity
# ---------------------------------------------------------------------------


class AuthenticationRequiredError(AppError):
    """No token, a malformed one, or one that failed verification.

    One error for all three: distinguishing "expired" from "forged" tells an attacker
    which half of the problem to work on. The reason is logged instead.
    """

    code = ErrorCode.AUTHENTICATION_REQUIRED
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "Authentication required."

    def __init__(
        self, message: str | None = None, *, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message, headers=headers or {"WWW-Authenticate": "Bearer"})


class InvalidCredentialsError(AppError):
    """Login failed. Used for both a wrong password and an unknown email address.

    The single message is the point: if an unknown email produced a different error,
    the login form would become an account-enumeration oracle.
    """

    code = ErrorCode.INVALID_CREDENTIALS
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "Incorrect email or password."


class InactiveUserError(AppError):
    """A correctly authenticated user whose account has been deactivated.

    403 rather than 401: the credentials are valid, so the client should stop
    retrying and tell the user to contact their administrator. Architecture §4.
    """

    code = ErrorCode.FORBIDDEN
    status_code = status.HTTP_403_FORBIDDEN
    message = "This account is inactive."


# ---------------------------------------------------------------------------
# 403 — valid identity, insufficient rights
# ---------------------------------------------------------------------------


class PermissionDeniedError(AppError):
    """The role does not grant the capability the route requires."""

    code = ErrorCode.FORBIDDEN
    status_code = status.HTTP_403_FORBIDDEN
    message = "You do not have permission to perform this action."


class TenantAccessDeniedError(AppError):
    """A tenant mismatch that is itself the reportable condition.

    Not raised for ordinary cross-tenant reads. Those return 404 via `NotFoundError`,
    because a 403 would confirm the record exists somewhere else and turn the API into
    an ID-enumeration oracle (ADR-009). This exists for the cases where the mismatch
    is the whole problem — for example a token whose organization claim disagrees with
    the user it names — and where the event deserves its own log line.
    """

    code = ErrorCode.TENANT_ACCESS_DENIED
    status_code = status.HTTP_403_FORBIDDEN
    message = "You do not have access to this resource."


# ---------------------------------------------------------------------------
# 404 — absent, or present in another tenant
# ---------------------------------------------------------------------------

_NOT_FOUND_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.USER_NOT_FOUND: "User not found.",
    ErrorCode.CUSTOMER_NOT_FOUND: "Customer not found.",
    ErrorCode.TICKET_NOT_FOUND: "Ticket not found.",
    ErrorCode.ATTACHMENT_NOT_FOUND: "Attachment not found.",
    ErrorCode.NOTIFICATION_NOT_FOUND: "Notification not found.",
}


class NotFoundError(AppError):
    """A resource that does not exist *for this tenant*.

    Cross-tenant reads land here rather than on `TenantAccessDeniedError`, so that
    "another organization's record" and "no such record" are indistinguishable to the
    caller. Repositories produce this naturally: a query scoped to the caller's
    organization simply matches nothing.
    """

    status_code = status.HTTP_404_NOT_FOUND

    def __init__(self, code: ErrorCode, message: str | None = None) -> None:
        super().__init__(message or _NOT_FOUND_MESSAGES.get(code, "Not found."))
        self.code = code


# ---------------------------------------------------------------------------
# 409 / 422 — the request was understood and rejected
# ---------------------------------------------------------------------------


class ConflictError(AppError):
    """A uniqueness or state conflict. Base for the specific cases."""

    status_code = status.HTTP_409_CONFLICT
    message = "That resource already exists."


class UserAlreadyExistsError(ConflictError):
    code = ErrorCode.USER_ALREADY_EXISTS
    message = "A user with that email already exists."


class CustomerAlreadyExistsError(ConflictError):
    code = ErrorCode.CUSTOMER_ALREADY_EXISTS
    message = "A customer with that email already exists."


class InvalidTicketTransitionError(ConflictError):
    """A status change that `TICKET_TRANSITIONS` does not permit.

    409 rather than 422: the request is well-formed and the target status is a real
    status — what is wrong is the ticket's current state, which is exactly the kind of
    conflict a retry after the right transition would resolve. Spec §5 names this error
    code; the message names both ends of the refused edge, because "invalid transition"
    alone leaves a client guessing which of its two states was the surprise.
    """

    code = ErrorCode.INVALID_TICKET_TRANSITION

    def __init__(self, current: str, target: str, hint: str | None = None) -> None:
        message = f"A ticket cannot move from {current} to {target}."
        if hint:
            message = f"{message} {hint}"
        super().__init__(message)


class ValidationError(AppError):
    """A domain rule rejected otherwise well-formed input.

    Pydantic handles shape; this is for rules it cannot express, such as an
    organization trying to deactivate its last administrator.
    """

    code = ErrorCode.VALIDATION_ERROR
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "The request could not be processed."


class UnsupportedFileTypeError(AppError):
    """An upload whose type could not be established, or could not be trusted.

    Spec §33: *"Never trust filename or MIME type alone."* All three of the inputs the
    client controls — the extension, the declared `Content-Type`, and the bytes — must
    agree, and the bytes get the final word. This covers every way they can fail to:
    an extension outside the allowlist, a declared type outside it, or a file whose
    leading bytes are not the signature of the type it claims to be.

    A zero-byte file lands here too. It has no signature, so there is nothing to agree
    with, and a dedicated rule for it would be a special case for the same answer.

    The message deliberately does not say *which* check failed. That distinction is of
    no use to a legitimate client — which either uploaded the right file or did not —
    and is of considerable use to someone probing what the validator accepts.
    """

    code = ErrorCode.UNSUPPORTED_FILE_TYPE
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "That file type is not supported."


class FileTooLargeError(AppError):
    """An upload past the configured ceiling.

    413 rather than 422: the request was well-formed and the type may be perfectly
    acceptable — the body is simply more than this service will accept, which is what
    413 means and what a client's retry logic keys on.
    """

    code = ErrorCode.FILE_TOO_LARGE
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    message = "That file is too large."


class StorageUnavailableError(AppError):
    """Object storage could not be reached, or refused the operation.

    503 rather than 500: this is a dependency being down, not a bug, and it is
    recoverable — the client can retry the same upload. Reporting it as a 500 would
    tell the client to give up on something that will work in a minute, and would bury
    it among the errors that genuinely need a person.

    The storage layer's own error is logged with its cause and never returned: an S3
    error message carries the bucket and key, which is infrastructure detail a client
    has no business seeing.
    """

    code = ErrorCode.STORAGE_UNAVAILABLE
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "File storage is temporarily unavailable."


# ---------------------------------------------------------------------------
# 429 / 500
# ---------------------------------------------------------------------------


class RateLimitedError(AppError):
    """Too many requests. `retry_after` becomes the Retry-After header."""

    code = ErrorCode.RATE_LIMITED
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    message = "Too many requests. Please try again later."

    def __init__(self, retry_after: int | None = None, message: str | None = None) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        super().__init__(message, headers=headers)
        self.retry_after = retry_after


class InternalServerError(AppError):
    """A bug, or an unhandled dependency failure.

    The message is fixed: whatever actually went wrong is logged with its traceback
    and never returned.
    """

    code = ErrorCode.INTERNAL_SERVER_ERROR
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    message = "An unexpected error occurred."


def error_body(code: ErrorCode, message: str) -> dict[str, Any]:
    """Build the §42 error envelope.

    Shared by every handler in `app/main.py` so the shape is defined once. A separate
    Pydantic response model per error would be ceremony for a two-field object.
    """
    return {"error": {"code": str(code), "message": message}}
