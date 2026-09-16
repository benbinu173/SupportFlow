"""Email delivery — the message that leaves the process, and what happens when it cannot.

Two modules, one contract. `app/core/mail.py` decides whether a failed send is worth
retrying; `app/workers/email_tasks.py` acts on that answer. Tested together because the
second is only correct if the first classifies honestly, and a test of either alone would
pass with the pair broken.

**What this file deliberately does not cover.** The task's database read is replaced by a
stand-in session, so nothing here proves the `select` is right — that is a query, and a
query is better tested by running it. `tests/api/test_notifications.py` and
`scripts/phase_p_walkthrough.py` exercise the whole path against real Postgres including
that read, and this file is then free to be about the parts above the session: the subject
line, the body, the retry policy, and the `emailed_at` guard.

The stand-in is also the honest answer to a real constraint. The task reaches the
database through `app/core/event_loop.run`, which starts a loop psycopg can drive
(ADR-011) — and a test running inside `pytest-asyncio`'s loop could not start a second
one. Every test here is therefore **synchronous**, and calls the task the way a worker
does: the task owns its loop, and the suite does not own it for it. That is why
`tests/conftest.py`'s `queued_emails` fixture records the enqueue rather than running the
task inline, and why this file can run the task for real.

**The retry tests drive Celery's own machinery.** They do not inspect `autoretry_for` and
call it verified — they call `.apply()`, let Celery's eager path run the task, and assert
on how many attempts actually happened. Eager mode is used here and nowhere else in the
suite, because this is the one place it is safe: no request loop is running, so the task's
loop is the only one.
"""

import smtplib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar, NoReturn

import pytest

from app.core import mail
from app.core.mail import PermanentEmailError, TransientEmailError
from app.models.enums import NotificationType, UserRole
from app.models.notification import Notification
from app.models.user import User
from app.workers import email_tasks
from app.workers.email_tasks import send_notification_email

pytestmark = pytest.mark.unit

_ORGANIZATION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Settings, fixed rather than read
# ---------------------------------------------------------------------------


def _settings(**overrides: object) -> SimpleNamespace:
    """The subset of settings the mail layer reads, with the real defaults.

    Replaced rather than read from the environment so the assertions can name literal
    strings. Reading `get_settings().SMTP_FROM` into an expected value would make the test
    agree with whatever happens to be configured, which is the opposite of what a
    composition test is for.
    """
    values: dict[str, object] = {
        "SMTP_HOST": "mailpit",
        "SMTP_PORT": 1025,
        "SMTP_FROM": "support@supportflow.local",
        "SMTP_TIMEOUT_SECONDS": 10,
        "SMTP_USERNAME": None,
        "SMTP_PASSWORD": None,
        "SMTP_STARTTLS": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def mail_settings(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Point `app.core.mail` at the fixed settings.

    Returns the same callable again so a test can reconfigure *within* itself — the
    STARTTLS and credential paths are configuration, and testing them needs a second
    configuration rather than a second test that repeats the first one's setup.
    """

    def configure(**overrides: object) -> None:
        monkeypatch.setattr(mail, "get_settings", lambda: _settings(**overrides))

    configure()
    return configure


# ---------------------------------------------------------------------------
# A recording stand-in for the SMTP client
# ---------------------------------------------------------------------------


def _raise(exc: BaseException) -> NoReturn:
    raise exc


class _FakeSMTP:
    """Records the conversation with the server, and can be made to fail on cue.

    Records the *calls* and not only the message, because half of `send_email`'s job is
    the conversation around the send: `ehlo` before `starttls`, and a login only when
    credentials are configured. A test that checked only the delivered bytes would pass
    with the ordering wrong.

    The recording is class-level, because `send_email` constructs the client itself and
    the test has no handle on the instance. The `smtp` fixture clears it between tests,
    and `on_enter` / `on_send` are the seams that let a test make the connection or the
    send fail the way a real server would.
    """

    instances: ClassVar[list["_FakeSMTP"]] = []
    calls: ClassVar[list[str]] = []
    messages: ClassVar[list[Any]] = []
    on_enter: ClassVar[Callable[[], None] | None] = None
    on_send: ClassVar[Callable[[Any], None] | None] = None

    def __init__(self, host: str, port: int, timeout: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        type(self).instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        if type(self).on_enter is not None:
            type(self).on_enter()
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def ehlo(self) -> None:
        type(self).calls.append("ehlo")

    def starttls(self) -> None:
        type(self).calls.append("starttls")

    def login(self, username: str, password: str) -> None:
        type(self).calls.append(f"login:{username}:{password}")

    def send_message(self, message: Any) -> None:
        if type(self).on_send is not None:
            type(self).on_send(message)
        type(self).calls.append("send")
        type(self).messages.append(message)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSMTP]:
    """Replace the SMTP client, so no test in this file opens a socket."""
    _FakeSMTP.instances = []
    _FakeSMTP.calls = []
    _FakeSMTP.messages = []
    _FakeSMTP.on_enter = None
    _FakeSMTP.on_send = None
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------


def test_a_message_is_sent_from_the_configured_sender_to_the_recipient(
    smtp: type[_FakeSMTP], mail_settings: Callable[..., None]
) -> None:
    """The four things an email needs, and the body as plain text.

    `set_content` is what makes it plain text — an `EmailMessage` given its body through
    `set_content` gets `Content-Type: text/plain`, while one assembled by hand can end up
    with no content type at all and render as nothing in some clients.
    """
    mail.send_email(
        to="ada@example.com", subject="[SupportFlow] Ticket assigned to you", body="body"
    )

    sent = smtp.messages[0]
    assert sent["From"] == "support@supportflow.local"
    assert sent["To"] == "ada@example.com"
    assert sent["Subject"] == "[SupportFlow] Ticket assigned to you"
    assert sent.get_content().strip() == "body"
    assert sent.get_content_type() == "text/plain"


def test_the_connection_is_opened_with_the_configured_host_and_timeout(
    smtp: type[_FakeSMTP], mail_settings: Callable[..., None]
) -> None:
    """A timeout that is configured but not passed is a timeout that does not exist.

    Worth asserting because the failure it prevents is a worker slot held open
    indefinitely by a mail server that accepted the connection and then said nothing.
    """
    mail.send_email(to="ada@example.com", subject="s", body="b")

    assert (smtp.instances[0].host, smtp.instances[0].port, smtp.instances[0].timeout) == (
        "mailpit",
        1025,
        10,
    )


def test_starttls_is_negotiated_after_the_capability_exchange(
    smtp: type[_FakeSMTP], mail_settings: Callable[..., None]
) -> None:
    """`ehlo` before `starttls`, and again after — the ordering is the point.

    STARTTLS is only offered if the server advertised it, and the advertisement arrives in
    the EHLO response. Repeating EHLO afterwards is what RFC 3207 asks for: the
    capabilities after the handshake belong to the encrypted session, not the plaintext one.
    """
    mail_settings(SMTP_STARTTLS=True)

    mail.send_email(to="ada@example.com", subject="s", body="b")

    assert smtp.calls == ["ehlo", "starttls", "ehlo", "send"]


def test_credentials_are_only_used_when_configured(
    smtp: type[_FakeSMTP], mail_settings: Callable[..., None]
) -> None:
    """Mailpit takes no credentials, and an unauthenticated `login` is a failed send.

    The default is `None`, so the unconfigured path has to be the one that skips the call
    — not the one that passes empty strings and hopes.
    """
    mail.send_email(to="ada@example.com", subject="s", body="b")

    assert [call for call in smtp.calls if call.startswith("login")] == []

    mail_settings(SMTP_USERNAME="apikey", SMTP_PASSWORD="hunter2")
    mail.send_email(to="ada@example.com", subject="s", body="b")

    assert smtp.calls[-2:] == ["login:apikey:hunter2", "send"]


# ---------------------------------------------------------------------------
# The classification the retry policy depends on
# ---------------------------------------------------------------------------


def test_a_refused_message_is_permanent_and_names_no_recipient(
    smtp: type[_FakeSMTP], mail_settings: Callable[..., None]
) -> None:
    """The server spoke and said no. Retrying re-sends the same rejected message.

    The error's message is the exception *type* and nothing else. `SMTPRecipientsRefused`'s
    own message quotes the address it refused, and that address would then be in
    `PermanentEmailError`'s message, in the task's traceback, and in the worker's log —
    §4's "no secrets in logs" reads the same for personal data as it does for tokens.
    """
    smtp.on_send = lambda message: _raise(
        smtplib.SMTPRecipientsRefused({"ada@example.com": (550, b"no such user")})
    )

    with pytest.raises(PermanentEmailError) as caught:
        mail.send_email(to="ada@example.com", subject="s", body="b")

    assert str(caught.value) == "SMTPRecipientsRefused"
    assert "ada@example.com" not in str(caught.value)


def test_an_unreachable_server_is_transient_and_names_no_host(
    smtp: type[_FakeSMTP], mail_settings: Callable[..., None]
) -> None:
    """Nothing answered. The same message may well go through in a minute.

    `ConnectionRefusedError` is an `OSError`, which is also what a DNS failure and a
    socket timeout arrive as — the whole reason `OSError` rather than a list of concrete
    types is the line between the two classifications.
    """
    smtp.on_enter = lambda: _raise(ConnectionRefusedError(111, "Connection refused"))

    with pytest.raises(TransientEmailError) as caught:
        mail.send_email(to="ada@example.com", subject="s", body="b")

    # The errno and not the sentence: `str(OSError)` for a socket error embeds the host and
    # port, and this exception's message travels to a log.
    assert str(caught.value) == "ConnectionRefusedError:111"
    assert "mailpit" not in str(caught.value)


def test_a_stalled_server_is_transient(
    smtp: type[_FakeSMTP], mail_settings: Callable[..., None]
) -> None:
    """A timeout is the case a retry exists for: the host is up, the conversation is not.

    Separate from the test above although they share a line of `mail.py`, because
    `TimeoutError` is an `OSError` subclass whose `errno` is `None` — a different path
    through the same formatting, and the one that would go unnoticed.
    """
    smtp.on_enter = lambda: _raise(TimeoutError("timed out"))

    with pytest.raises(TransientEmailError) as caught:
        mail.send_email(to="ada@example.com", subject="s", body="b")

    assert str(caught.value) == "TimeoutError:None"


# ---------------------------------------------------------------------------
# The task, against a session that is not a database
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, notification: Notification | None) -> None:
        self._notification = notification

    def scalar_one_or_none(self) -> Notification | None:
        return self._notification


class _FakeSession:
    """A session that answers one query and records its commits.

    Deliberately not a `Mock`. A mock would accept the wrong query and the wrong method
    names, so a test could pass against a task that had drifted; this accepts exactly what
    `_deliver` uses — `execute`, `commit`, and the async context manager — and anything
    else is an `AttributeError` naming the method that was wanted.
    """

    def __init__(self, notification: Notification | None) -> None:
        self._notification = notification
        self.commits = 0

    async def execute(self, statement: Any) -> _FakeResult:
        return _FakeResult(self._notification)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _user(*, is_active: bool = True, email: str = "ada@example.com") -> User:
    return User(
        id=uuid.uuid4(),
        organization_id=_ORGANIZATION_ID,
        name="Ada Lovelace",
        email=email,
        password_hash="not-a-real-hash",
        role=UserRole.AGENT,
        is_active=is_active,
    )


def _notification(*, user: User | None, emailed_at: datetime | None = None) -> Notification:
    notification = Notification(
        id=uuid.uuid4(),
        organization_id=_ORGANIZATION_ID,
        user_id=user.id if user is not None else uuid.uuid4(),
        notification_type=NotificationType.TICKET_ASSIGNED,
        title="Ticket assigned to you",
        body="#1042 - The printer is on fire",
        ticket_id=uuid.uuid4(),
        emailed_at=emailed_at,
    )
    # The relationship, set directly: this object is never added to a session, so there is
    # no lazy load to avoid — which is the only reason `_deliver` joins in the first place.
    notification.user = user
    return notification


@pytest.fixture
def session_for(monkeypatch: pytest.MonkeyPatch) -> Callable[[Notification | None], _FakeSession]:
    """Point the task at a fake session and hand back the object it will use.

    The project name is replaced too, so the subject and body assertions can be literal
    strings rather than a format string rebuilt from the same settings the task read.
    """
    monkeypatch.setattr(email_tasks, "_settings", SimpleNamespace(PROJECT_NAME="SupportFlow"))

    def build(notification: Notification | None) -> _FakeSession:
        session = _FakeSession(notification)
        monkeypatch.setattr(email_tasks, "_SessionFactory", lambda: session)
        return session

    return build


@pytest.fixture
def sends(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Record every send the task performs, and perform none."""
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(email_tasks, "send_email", lambda **kwargs: calls.append(kwargs))
    return calls


def test_the_email_is_composed_from_the_stored_row(
    session_for: Callable[[Any], _FakeSession], sends: list[dict[str, str]]
) -> None:
    """The subject is bracketed with the product name; the body is the notification plus a nudge.

    Composed here rather than in the service because a subject line and a bracketed prefix
    are email's concerns, while `notification.title` and `.body` are what the in-app bell
    shows and have to read correctly there. So the assertion is on the *difference* between
    the two representations, which is the whole reason the composition lives in this module.
    """
    notification = _notification(user=_user())
    session_for(notification)

    result = send_notification_email(str(notification.id))

    assert result == "delivered"
    assert sends == [
        {
            "to": "ada@example.com",
            "subject": "[SupportFlow] Ticket assigned to you",
            "body": "#1042 - The printer is on fire\n\nOpen SupportFlow to view it.",
        }
    ]


def test_the_send_happens_before_the_flag_is_written(
    session_for: Callable[[Any], _FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`emailed_at` is stamped only after the send returned, and this is the direct check.

    Asserting the flag afterwards would also pass if it had been set *before* the send —
    which is wrong in exactly the case that matters: a send that then failed would leave a
    row claiming an email that never left. Reading the row from inside the send is what
    makes the ordering observable.
    """
    notification = _notification(user=_user())
    session_for(notification)
    flag_at_send_time: list[datetime | None] = []
    monkeypatch.setattr(
        email_tasks,
        "send_email",
        lambda **kwargs: flag_at_send_time.append(notification.emailed_at),
    )

    send_notification_email(str(notification.id))

    assert flag_at_send_time == [None]
    assert notification.emailed_at is not None


def test_a_delivered_notification_is_stamped_and_committed(
    session_for: Callable[[Any], _FakeSession], sends: list[dict[str, str]]
) -> None:
    """The stamp is what a redelivery checks, so it has to be committed to survive it."""
    notification = _notification(user=_user())
    session = session_for(notification)

    send_notification_email(str(notification.id))

    assert notification.emailed_at is not None
    assert notification.emailed_at.tzinfo is not None
    assert session.commits == 1


def test_an_already_emailed_notification_is_not_sent_again(
    session_for: Callable[[Any], _FakeSession], sends: list[dict[str, str]]
) -> None:
    """The at-least-once guard, and what makes `task_acks_late` safe to turn on.

    A worker killed after sending but before acknowledging is handed the task again.
    `acks_late` is what stops an email being silently dropped when a worker dies; this
    check is what stops the same setting from turning that into a duplicate.
    """
    notification = _notification(user=_user(), emailed_at=datetime.now(UTC))
    session = session_for(notification)

    result = send_notification_email(str(notification.id))

    assert result == "already-sent"
    assert sends == []
    # Not re-committed either: a no-op write on every redelivery is a lock taken for nothing.
    assert session.commits == 0


def test_a_notification_that_no_longer_exists_sends_nothing(
    session_for: Callable[[Any], _FakeSession], sends: list[dict[str, str]]
) -> None:
    """A row deleted between being queued and being delivered is not an error.

    `notifications.user_id` cascades, so removing a user removes their notifications.
    Failing the task instead would retry five times against a row that will never come
    back, and the alert it described belonged to an account that no longer exists.
    """
    session = session_for(None)

    result = send_notification_email(str(uuid.uuid4()))

    assert result == "missing"
    assert sends == []
    assert session.commits == 0


@pytest.mark.parametrize("deactivated", [False, True], ids=["no-user", "deactivated"])
def test_a_recipient_who_cannot_read_it_is_not_emailed(
    deactivated: bool,
    session_for: Callable[[Any], _FakeSession],
    sends: list[dict[str, str]],
) -> None:
    """Two ways to have nobody to mail, and the same answer for both.

    A deactivated account is treated as absent rather than merely as unsubscribed: mailing
    somebody the organization has cut off is worse than not mailing them, and an in-app
    notification they cannot sign in to read is not a notification either.
    """
    user = _user(is_active=False) if deactivated else None
    session = session_for(_notification(user=user))

    result = send_notification_email(str(uuid.uuid4()))

    assert result == "no-recipient"
    assert sends == []
    assert session.commits == 0


# ---------------------------------------------------------------------------
# Retry, driven through Celery rather than asserted about
# ---------------------------------------------------------------------------


def test_a_transient_failure_is_retried_and_leaves_the_row_unstamped(
    session_for: Callable[[Any], _FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six attempts — the first plus `max_retries=5` — and the flag still unset.

    The count is the claim. `autoretry_for` names a class, and a class in a tuple proves
    nothing about whether a retry attempt happens, which is why this runs the task instead
    of inspecting its options.

    The unstamped row is the other half: a failed delivery must leave the notification
    looking exactly like one that was never queued, because that is what it is — and
    `emailed_at IS NULL` is the query a later sweep will use to find what still needs
    sending.
    """
    notification = _notification(user=_user())
    session = session_for(notification)
    attempts: list[int] = []

    def _fail(**kwargs: str) -> None:
        attempts.append(1)
        raise TransientEmailError("ConnectionRefusedError:111")

    monkeypatch.setattr(email_tasks, "send_email", _fail)

    result = send_notification_email.apply(args=[str(notification.id)])

    assert len(attempts) == 6
    assert notification.emailed_at is None
    assert session.commits == 0
    assert result.state == "FAILURE"
    # The original error, not `MaxRetriesExceededError`: the traceback in the worker's log
    # should name the mail server's problem, which is the thing an operator can act on.
    assert isinstance(result.get(propagate=False, disable_sync_subtasks=False), TransientEmailError)


def test_a_permanent_failure_is_not_retried(
    session_for: Callable[[Any], _FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One attempt. Five retries of a rejected message is five minutes spent re-sending it.

    This is the test that fails if `PermanentEmailError` is ever added to `autoretry_for` —
    by a well-meaning edit reasoning that more retries is more reliable. The row is left
    unstamped either way, so a sweep can still find it; what changes is whether the worker
    spends five minutes learning the same answer.
    """
    notification = _notification(user=_user())
    session = session_for(notification)
    attempts: list[int] = []

    def _fail(**kwargs: str) -> None:
        attempts.append(1)
        raise PermanentEmailError("SMTPRecipientsRefused")

    monkeypatch.setattr(email_tasks, "send_email", _fail)

    result = send_notification_email.apply(args=[str(notification.id)])

    assert len(attempts) == 1
    assert notification.emailed_at is None
    assert session.commits == 0
    assert result.state == "FAILURE"
    assert isinstance(result.get(propagate=False, disable_sync_subtasks=False), PermanentEmailError)


def test_the_task_is_registered_under_its_qualified_name() -> None:
    """The name the broker stores, and the one `task_routes` matches on.

    `task_routes` in `celery_app.py` routes `app.workers.email_tasks.*`, which is a prefix
    match on this string. A task left with Celery's generated module-path name is routed
    correctly by accident; one named explicitly is routed by intent. This pins the two
    together, so a rename cannot silently drop the routing and send the task to the
    default queue instead of `notifications`.
    """
    assert send_notification_email.name == "app.workers.email_tasks.send_notification_email"
