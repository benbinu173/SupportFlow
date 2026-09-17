"""Phase R verification: what a Redis outage costs, on a live socket.

The claim is that an outage degrades real-time delivery and nothing else. Two things follow
from it and neither is obvious:

* **An authenticated write still succeeds.** `realtime.publish` fails open, and so does
  `notification_service.enqueue_delivery` — the transaction has already committed, and
  returning a 500 for an action that succeeded is strictly worse than losing the announcement.
  Both log the error *type* and not the message, because a connection error's message embeds
  the broker URL and in production that URL carries a password.
* **Delivery resumes on the socket the client already holds.** The subscriber reconnects with
  a bounded backoff, so the recovery must not require the API to restart. That is what makes
  this script hold one connection across the whole thing: two sockets would prove only that a
  new one works.

It stops and starts Redis itself, and leaves it running whichever way the run ends. The API
and the worker must already be up.

    .venv/Scripts/python.exe scripts/redis_bounce_check.py

Afterwards, read the API's own stdout: there should be a subscriber error line naming the
failure, and **no second "Application startup complete"** — the process never restarted.
"""

# ruff: noqa: T201

import contextlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from websockets.sync.client import connect as socket_connect

#: Resolved from this file rather than from the working directory, so the check runs the same
#: way from `backend/` as it does from the repository root.
COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

BASE = "http://localhost:8000/api/v1"
WS = "ws://localhost:8000/ws"
PASSWORD = "correct-horse-battery-staple"

#: How long to wait for one announced ticket. The publish path is a local Redis round trip, so
#: a few seconds is generous rather than tight.
ANNOUNCE_SECONDS = 5.0

#: How long to wait for the subscriber to come back after Redis restarts. The backoff is
#: bounded, so this is a ceiling rather than an expectation.
RECOVERY_SECONDS = 90.0

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ok    {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}{f' -- {detail}' if detail else ''}")


def compose(*args: str) -> None:
    # The noqa is for a call this file has no untrusted input for: every argument is a literal
    # from this file, and `docker` comes off PATH because requiring an absolute path to it
    # would make the check unrunnable on the machine it is written for.
    command = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    subprocess.run(command, check=True, capture_output=True)  # noqa: S603


class Session:
    """A registered tenant, its socket, and the two calls this check makes."""

    def __init__(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        response = httpx.post(
            f"{BASE}/auth/register",
            json={
                "organization_name": f"Bounce {suffix}",
                "name": "Admin",
                "email": f"bounce-{suffix}@walkthrough.example",
                "password": PASSWORD,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        self.token = str(response.json()["access_token"])
        self.headers = {"Authorization": f"Bearer {self.token}"}

        created = httpx.post(
            f"{BASE}/customers",
            headers=self.headers,
            json={"name": "Bounce Customer", "email": f"bounce-{suffix}@customer.example"},
            timeout=30.0,
        )
        created.raise_for_status()
        self.customer_id = str(created.json()["id"])

        # `legacy=True`: this connection is held for the whole of the run and closed in
        # `close()`, rather than scoped to a block.
        self.socket = socket_connect(WS, open_timeout=10, close_timeout=5, legacy=True)
        self.socket.send(json.dumps({"type": "auth", "token": self.token}))
        self.acknowledged = json.loads(self.socket.recv(timeout=10.0)).get("type")

    def create(self, subject: str) -> httpx.Response:
        return httpx.post(
            f"{BASE}/tickets",
            headers=self.headers,
            json={
                "subject": subject,
                "description": "Bounce.",
                "customer_id": self.customer_id,
                "priority": "medium",
            },
            timeout=30.0,
        )

    def announced(self, *, within: float = ANNOUNCE_SECONDS) -> int | None:
        """The ticket number of the next announcement, or `None` if none arrived."""
        deadline = time.monotonic() + within
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                envelope: dict[str, Any] = json.loads(self.socket.recv(timeout=remaining))
            except TimeoutError:
                return None
            if envelope.get("type") == "ticket.created":
                return int(envelope["ticket_number"])

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.socket.close()


def main() -> None:
    session = Session()
    try:
        run(session)
    finally:
        session.close()
        # Left running whatever happened, so a failure here does not hand back a broken stack.
        compose("start", "redis")


def run(session: Session) -> None:
    check(
        "a socket opened before the outage is acknowledged",
        session.acknowledged == "authenticated",
    )

    print("\n1. Delivery works, so the arrival below means something\n" + "-" * 54)
    first = session.create("Before the outage")
    check("the ticket was created", first.status_code == 201, first.text[:120])
    check(
        "and announced on the socket",
        session.announced() == first.json()["number"],
    )

    print("\n2. Redis stops\n" + "-" * 14)
    compose("stop", "redis")
    print("  redis stopped")

    second = session.create("Created while Redis is down")
    check(
        "the write still succeeds, because the publish fails open",
        second.status_code == 201,
        second.text[:200],
    )
    check("and nothing is announced, because the subscriber is down", session.announced() is None)

    print("\n3. Redis comes back\n" + "-" * 19)
    compose("start", "redis")

    # The subscriber reconnects on its own schedule, so the arrival is polled for rather than
    # slept on: retrying the write is how the test observes recovery without knowing the
    # backoff's interval. Each retry is a real ticket, which is harmless on a development
    # database and is the point — the alternative is guessing the reconnect delay.
    deadline = time.monotonic() + RECOVERY_SECONDS
    recovered: int | None = None
    attempts = 0
    while recovered is None and time.monotonic() < deadline:
        attempts += 1
        third = session.create(f"After the outage, attempt {attempts}")
        if third.status_code == 201:
            recovered = session.announced()

    check(
        "delivery resumes on the socket that was already open",
        recovered is not None,
        f"nothing arrived in {RECOVERY_SECONDS:.0f}s",
    )
    # Reported rather than asserted: the backoff's interval is the subscriber's business, and
    # an assertion here would be a test of how long `connect()` happens to wait today.
    print(f"  ..    recovered after {attempts} attempt(s)")

    print(f"\n{_passed} passed, {_failed} failed")
    print(
        "\nNow read the API's own stdout. Two things should be there:\n"
        "  * a subscriber error line, naming the failure and not the broker URL;\n"
        '  * exactly one "Application startup complete" -- the process never restarted.'
    )
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
