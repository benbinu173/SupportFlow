"""Outbound email — SMTP, and nothing above it.

The same role `app/core/storage.py` plays for S3: a thin boundary around a library that
is deliberately the only place the library is imported. Everything above this module
composes a subject and a body and gets a domain error back; nothing above it knows that
`smtplib` exists, so swapping the transport is a change to one file.

**Synchronous on purpose.** Celery tasks are synchronous, and so is `smtplib` — which is
why a worker can send mail without the async machinery the API runs on. The coroutine
bridge exists only for the database read, and it lives in the task, not here.

**The two exception types are the whole interface.** A caller has exactly one question
to answer about a failed send: is this worth retrying? So `smtplib`'s errors are
translated into two answers rather than passed through as a dozen shapes:

* `TransientEmailError` — the server was unreachable, refused the connection, or timed
  out. Retrying is the correct response and the task does it with backoff.
* `PermanentEmailError` — the message itself was rejected: a malformed recipient, a
  refused relay, a bad credential. Retrying re-sends the same rejected message, so the
  task does not.

`SMTPException` is the base of `SMTPRecipientsRefused`, `SMTPSenderRefused`,
`SMTPAuthenticationError`, and the rest — all of which describe a message or a
credential the server would refuse again. `OSError` covers connection refused, DNS
failure, and the socket timeout, all of which may well succeed in a minute. The split
follows that line rather than chasing individual exception names.
"""

import smtplib
from email.message import EmailMessage

import structlog

from app.core.config import get_settings

logger = structlog.get_logger(__name__)


class EmailError(Exception):
    """Base for a failed send. Never escapes this module's callers un-caught."""


class TransientEmailError(EmailError):
    """The send may succeed if tried again: unreachable, refused, or timed out."""


class PermanentEmailError(EmailError):
    """The send will fail identically every time: the message or credentials are wrong."""


def send_email(*, to: str, subject: str, body: str) -> None:
    """Deliver one plain-text message.

    Plain text deliberately. An HTML template is a rendering concern with its own
    testing story, and a notification body is a sentence and a link — markup would add a
    second representation of content that already exists in the database.

    No `Cc`, `Bcc`, or `Reply-To`: this sends a notification, and a notification with
    headers nobody sets is a notification whose reply-to is an unmonitored mailbox.

    The connection is opened and closed per message rather than pooled. A worker sends
    few messages and reconnects rarely, and a pooled SMTP connection is a long-lived
    authenticated socket that goes stale silently between long idle periods — the
    failure mode being a batch of notifications that quietly fail after hours of quiet.
    """
    settings = get_settings()

    message = EmailMessage()
    message["From"] = settings.SMTP_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(
            settings.SMTP_HOST,
            settings.SMTP_PORT,
            timeout=settings.SMTP_TIMEOUT_SECONDS,
        ) as client:
            # `ehlo` is explicit so the capability exchange happens before STARTTLS is
            # attempted; `starttls` needs the server's advertised extensions to know
            # whether it is supported, and letting the library guess is how a misconfigured
            # server turns into an unclear failure.
            client.ehlo()
            if settings.SMTP_STARTTLS:
                client.starttls()
                client.ehlo()
            if settings.SMTP_USERNAME is not None:
                # Credentials are read from settings and passed to the library. They are
                # never logged, never included in an error raised from here, and never
                # echoed back to the caller — §4's "no secrets in logs" applies to the
                # worker exactly as it applies to the API.
                client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
            client.send_message(message)
    except smtplib.SMTPException as exc:
        # The server spoke and refused. `type(exc).__name__` and not `str(exc)`: an
        # authentication failure's message can quote the username, and a recipient
        # refusal quotes the address.
        raise PermanentEmailError(type(exc).__name__) from exc
    except OSError as exc:
        # Never connected, or connected and stalled. `errno` rather than `str(exc)`,
        # which embeds the host and port.
        raise TransientEmailError(f"{type(exc).__name__}:{exc.errno}") from exc
