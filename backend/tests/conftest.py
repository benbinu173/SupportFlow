"""Shared pytest fixtures.

Environment variables are set before importing application code so that
`Settings` validation succeeds without a real .env file present.

Two families of fixture live here:

* **Database** (`db_engine`, `db`, `truncate_tables`) — for tests that want the ORM
  directly.
* **HTTP** (`client`, `register_org`) — for tests that drive the API. These seed and
  act exclusively through real requests, which is both a stronger test than inserting
  rows and the only way to exercise the auth and tenancy layers end to end.
  `tests/api/conftest.py` explains how those tests stay isolated from each other.
"""

import asyncio
import itertools
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault(
    "DATABASE_URL",
    # Matches the credentials in docker/postgres/init: the dev and test databases
    # share a role, and only the database name differs.
    "postgresql+psycopg://supportflow:supportflow@localhost:5432/supportflow_test",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("JWT_SECRET", "test-secret-value-that-is-long-enough-32")

# Object storage. `S3_ACCESS_KEY` and `S3_SECRET_KEY` have no defaults in `Settings` —
# a missing secret must fail at startup rather than fall back — so without these the
# whole suite would refuse to import on a machine with no `.env`, which is every CI
# checkout. The values match the `minio` service in `docker-compose.yml`, so the
# attachment tests talk to the same container the app does in development.
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
os.environ.setdefault("S3_BUCKET", "supportflow-attachments")
os.environ.setdefault("S3_REGION", "us-east-1")

# The rate limiter is real and hits real Redis, but the whole suite logs in far more
# often from the same address than any human would. Raising the ceilings here keeps
# every other test off the limiter's path; `tests/unit/test_rate_limit.py` exercises
# the limit logic directly against a stand-in client, and `tests/api/test_auth.py`
# has one test that lowers the value back down to prove the 429 wiring works.
os.environ.setdefault("RATE_LIMIT_LOGIN_PER_MINUTE", "100000")
os.environ.setdefault("RATE_LIMIT_REGISTER_PER_HOUR", "100000")

# Imported after the environment is populated, which is why this block sits below
# the statements above. Ruff's E402 allows `os.environ` setup before imports
# precisely because this pattern is unavoidable for test configuration.
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.event_loop import loop_factory
from app.core.permissions import PORTAL_ROLES
from app.main import create_app
from app.models import Base

API = "/api/v1"
AUTH = f"{API}/auth"
USERS = f"{API}/users"
CUSTOMERS = f"{API}/customers"
TICKETS = f"{API}/tickets"

# Comfortably past the configured 12-character minimum, and not a credential anyone
# would mistake for a real one.
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(scope="session")
def fresh_schema(sync_engine: Engine) -> None:
    """Drop and recreate every table, once, before the first client exists.

    This used to live in `db_engine`, which was the wrong place for it. PostgreSQL caches
    a prepared statement's result type against the relation it was planned against, and
    dropping then recreating a table gives it a new identity — so every plan the
    application's connection pool is already holding becomes unusable, and the next
    request fails with `cached plan must not change result type`, naming neither the
    reset nor the table that changed.

    That only bites if the reset happens *after* the app has served a request, which is
    exactly what a lazily-set-up `db_engine` does: `tests/security/test_row_scopes.py`
    builds rows through the ORM before it exercises anything over HTTP, so it invites
    `db_engine` in while `tests/security/test_log_hygiene.py` has already been issuing
    requests against the same pool.

    So the reset is attached to `client` instead. `client` is the one thing in the suite
    that cannot be created later than the first request, which makes "the schema is reset
    before anyone can talk to the app" a property of the fixture graph rather than of
    collection order — and collection order is the kind of thing that changes the day
    somebody renames a directory.

    Synchronous on purpose: `sync_engine`'s docstring explains why schema work in this
    suite avoids pytest-asyncio's default loop on Windows.
    """
    Base.metadata.drop_all(sync_engine)
    Base.metadata.create_all(sync_engine)


@pytest.fixture(scope="session")
def client(fresh_schema: None) -> Iterator[TestClient]:
    """Synchronous test client against the real ASGI app.

    `backend_options` carries the same loop factory the app and the async tests
    use. TestClient runs the app on its own loop in a worker thread, which would
    otherwise be a ProactorEventLoop on Windows — enough for routing tests to pass
    while every endpoint touching Postgres silently reported itself unavailable.

    **Session-scoped, and that is load-bearing.** `TestClient` owns an event loop and
    shuts it down on exit, but the application's engine in `app/core/database.py` is a
    process-global whose pool would then hold connections bound to a loop that no
    longer exists — the next client would fail with "attached to a different loop" or
    "Event loop is closed". One client for the session mirrors production, where there
    is also exactly one loop, and it is why `tests/api/conftest.py` truncates between
    tests instead of starting a fresh application each time.
    """
    with TestClient(
        create_app(),
        backend_options={"loop_factory": loop_factory},
    ) as test_client:
        yield test_client


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Give every async test a loop the database driver can use.

    psycopg cannot drive Windows' default ProactorEventLoop, so without this every
    database test fails at connect with an InterfaceError — see
    app/core/event_loop.py for the full reasoning.

    This hook rather than the `event_loop_policy` fixture: policies are deprecated
    in Python 3.14 and pytest-asyncio is removing the fixture that wraps them. The
    factory reused here is the same one uvicorn is given, so tests and the running
    app share a loop implementation instead of drifting apart.
    """
    return {"asyncio": loop_factory}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# These fixtures need a running Postgres with the vector and pg_trgm extensions.
# `make up` provides it; tests using them are marked `integration`.


@pytest.fixture(scope="session")
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Session-scoped engine with the schema present.

    Built from metadata rather than by running migrations: this asserts the models
    are self-consistent, independently of whether a migration has been written yet.
    Phase E adds a separate check that migrations produce the same schema.

    `checkfirst`, never a drop. The reset lives in `fresh_schema`, which `client`
    depends on — see its docstring for why running it here broke HTTP tests that ran
    after this fixture was first requested.

    **The schema is deliberately not dropped on teardown.** It used to be, and that
    broke every test that ran afterwards: the HTTP suite's `sync_engine` is also
    session-scoped, and this fixture's setup — which necessarily runs *after* it, since
    pytest collects `tests/api/` before `tests/integration/` — would leave the tables
    gone when `tests/security/` started truncating. Two session-scoped fixtures cannot
    both own the schema's lifetime, and an ordering dependency between them is exactly
    the kind of thing that works until someone renames a directory. Leaving an empty
    schema behind in a database whose name ends in `_test` costs nothing.
    """
    engine = create_async_engine(str(get_settings().DATABASE_URL))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose writes are rolled back when the test ends.

    The session joins an outer transaction that is never committed, so tests share
    one schema without leaking rows into each other. `session.commit()` inside a
    test commits to a savepoint only, which keeps commit-dependent behaviour
    testable while staying isolated.
    """
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        factory = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with factory() as session:
            yield session
        await transaction.rollback()


@pytest.fixture(scope="session")
def sync_engine() -> Iterator[Engine]:
    """A synchronous engine, for HTTP-test bookkeeping.

    Sync on purpose. The HTTP tests are synchronous — `TestClient` runs the ASGI app
    on its own loop in a worker thread — and pytest-asyncio runs an async fixture
    requested by a *sync* test on its default loop rather than the one
    `pytest_asyncio_loop_factories` supplies. That default is a ProactorEventLoop on
    Windows, which psycopg refuses to drive. Truncating a table needs no concurrency,
    so the loop question is best removed rather than configured.

    `checkfirst=True`, never a drop: `fresh_schema` owns the one reset in the suite.
    """
    engine = create_engine(get_settings().sqlalchemy_dsn, poolclass=NullPool)
    Base.metadata.create_all(engine, checkfirst=True)
    yield engine
    engine.dispose()


@pytest.fixture
def truncate_tables(sync_engine: Engine) -> Iterator[None]:
    """Empty every table after the test that requested this.

    Deliberately **not** autouse here. Requesting it forces a real database, and the
    unit tests must stay runnable with nothing running — `tests/api/conftest.py` and
    `tests/security/conftest.py` opt their packages in.

    Runs on teardown, so a failing test leaves nothing behind for the next one. The
    table names come from our own metadata, so they are identifiers by construction
    rather than strings from anywhere a client could reach.
    """
    yield
    names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with sync_engine.begin() as conn:
        # CASCADE because the tables are full of foreign keys. RESTART IDENTITY is
        # unnecessary — every primary key is a UUID with no sequence behind it.
        conn.execute(text(f"TRUNCATE TABLE {names} CASCADE"))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


@dataclass
class OrgSession:
    """A registered organization, plus the credentials of one user in it.

    Every field comes from a real HTTP response — nothing here reaches into the
    database. That is what makes these tests evidence that the API works rather than
    evidence that the ORM does.

    `auth` is a property returning a fresh dict each time, so a test that mutates the
    headers it passes cannot corrupt the session for the next request.
    """

    client: TestClient
    user_id: str
    email: str
    password: str
    role: str
    access_token: str
    refresh_token: str | None = None
    _users_created: int = field(default=0, repr=False)
    _customers_created: int = field(default=0, repr=False)

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        """The bearer header, plus anything the caller added.

        Merged rather than replaced, so a test can attach a `Cookie` header without
        having to restate the authorization — and cannot accidentally drop it and
        turn a test into an unauthenticated request that passes for the wrong reason.
        """
        return {**self.auth, **(extra or {})}

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.client.get(path, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.client.post(path, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.client.patch(path, headers=self._headers(kwargs.pop("headers", None)), **kwargs)

    def add_user(
        self,
        role: str,
        *,
        name: str | None = None,
        email: str | None = None,
        password: str = PASSWORD,
        customer_id: str | None = None,
    ) -> "OrgSession":
        """Create a user in this organization through the API, then log them in.

        Exercises the real path — an admin calling `POST /users`, then the new account
        calling `POST /auth/login` — so a role that cannot actually authenticate is
        discovered here rather than assumed working.

        The caller's own `_users_created` counter makes the generated emails unique
        within the organization without a global sequence.

        **A portal role gets a customer created for it** unless one is passed in. A
        `customer` account must be linked to a `Customer` row — that link is what makes
        `RowScope.OWN` resolvable — so the API refuses an unlinked one. Doing it here
        keeps every existing `org.add_user("customer")` call site working and meaning
        what it always meant: "a customer-role principal in this organization". Tests
        that care *which* customer a portal user acts as pass `customer_id` explicitly,
        which is what `as_portal_user` below is for.
        """
        self._users_created += 1
        suffix = self._users_created
        address = email or f"user{suffix}-{self.email}"

        if customer_id is None and role in PORTAL_ROLES:
            customer_id = self.add_customer(
                name=name or f"Customer {suffix}",
                email=f"customer{suffix}-{self.email}",
            )["id"]

        body: dict[str, str] = {
            "name": name or f"User {suffix}",
            "email": address,
            "password": password,
            "role": role,
        }
        if customer_id is not None:
            body["customer_id"] = customer_id

        response = self.post(USERS, json=body)
        assert response.status_code == 201, response.text

        return login(self.client, address, password)

    def add_customer(
        self,
        *,
        name: str = "Test Customer",
        email: str | None = None,
        phone: str | None = None,
        external_reference: str | None = None,
    ) -> dict[str, Any]:
        """Create a customer through the API and return the response body.

        Returns the body rather than a wrapper object: a customer has no session of its
        own until a portal login is created for it, so there is nothing else to carry.
        """
        self._customers_created += 1
        suffix = self._customers_created

        response = self.post(
            CUSTOMERS,
            json={
                "name": name,
                "email": email or f"customer{suffix}@{self.email.split('@')[-1]}",
                "phone": phone,
                "external_reference": external_reference,
            },
        )
        assert response.status_code == 201, response.text
        return cast("dict[str, Any]", response.json())

    def add_portal_user(self, customer_id: str, *, email: str | None = None) -> "OrgSession":
        """Create a login for a specific customer and return their session.

        The `own` scope resolves through this link, so tests about "a customer sees only
        their own tickets" need the link to name a customer they chose.
        """
        return self.add_user("customer", customer_id=customer_id, email=email)

    def add_ticket(
        self,
        customer_id: str,
        *,
        subject: str = "Something is broken",
        description: str = "It does not work.",
        priority: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Create a ticket through the API and return the response body."""
        payload: dict[str, Any] = {
            "subject": subject,
            "description": description,
            "customer_id": customer_id,
        }
        if priority is not None:
            payload["priority"] = priority
        if category is not None:
            payload["category"] = category

        response = self.post(TICKETS, json=payload)
        assert response.status_code == 201, response.text
        return cast("dict[str, Any]", response.json())


def cookie_header(token: str) -> dict[str, str]:
    """A `Cookie` header carrying a refresh token.

    Sent as an explicit header rather than through httpx's `cookies=` argument for two
    reasons: that argument is deprecated at the per-request level with ambiguous
    persistence semantics, and — more importantly — the `client` fixture is
    session-scoped, so a cookie written into its jar would leak into every later test.
    An explicit header is scoped to exactly one request and cannot.
    """
    return {"Cookie": f"{get_settings().REFRESH_COOKIE_NAME}={token}"}


def login(client: TestClient, email: str, password: str = PASSWORD) -> OrgSession:
    """Authenticate over HTTP and return the resulting session."""
    response = client.post(f"{AUTH}/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text

    body = response.json()
    # The cookie is read off the response rather than left in the client's jar: the
    # jar is shared by every caller of the fixture, so two organizations in one test
    # would silently overwrite each other's refresh token.
    client.cookies.clear()

    me = client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200, me.text

    return OrgSession(
        client=client,
        user_id=me.json()["id"],
        email=email,
        password=password,
        role=me.json()["role"],
        access_token=body["access_token"],
        refresh_token=response.cookies.get(get_settings().REFRESH_COOKIE_NAME),
    )


@pytest.fixture
def register_org(client: TestClient) -> Callable[..., OrgSession]:
    """Factory for registered organizations, each with its own admin session.

    A counter rather than a fixture parameter so a single test can register two
    organizations and assert one cannot see the other — which is the whole point of
    the isolation suite.
    """
    counter = itertools.count(1)

    def _register(
        *,
        organization_name: str | None = None,
        email: str | None = None,
        password: str = PASSWORD,
    ) -> OrgSession:
        number = next(counter)
        response = client.post(
            f"{AUTH}/register",
            json={
                "organization_name": organization_name or f"Acme {number}",
                "name": f"Admin {number}",
                "email": email or f"admin{number}@example.com",
                "password": password,
            },
        )
        assert response.status_code == 201, response.text

        body = response.json()
        client.cookies.clear()
        refresh_token = response.cookies.get(get_settings().REFRESH_COOKIE_NAME)

        me = client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert me.status_code == 200, me.text

        return OrgSession(
            client=client,
            user_id=me.json()["id"],
            email=email or f"admin{number}@example.com",
            password=password,
            role=me.json()["role"],
            access_token=body["access_token"],
            refresh_token=refresh_token,
        )

    return _register
