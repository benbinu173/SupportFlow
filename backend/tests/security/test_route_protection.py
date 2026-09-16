"""Every route is protected, asserted mechanically rather than by review.

Spec §54 requires "authorization on every protected resource". The failure this guards
against is not a wrong permission — it is a route that was added in a hurry and has
*none*. That failure is invisible in review (the diff looks like any other endpoint),
invisible at runtime (it returns 200, which looks like success), and severe. So it is
checked by walking the application's routing table, where a new endpoint cannot hide.

Two independent claims are asserted for every route:

* **Authenticated** — the auth dependency is somewhere in its dependency tree, or the
  route is on the explicit public allowlist.
* **Authorized** — it declares the capability it needs via `require_permission`, or it
  is on the (much shorter) list of routes that legitimately need authentication only.

The allowlists are compared for *equality* against what the application actually does,
not merely consulted. That is what makes them a record of deliberate decisions: adding
a public route means editing this file, which is the point at which someone asks
whether it should be public.
"""

from collections.abc import Iterable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api.deps import get_current_user
from app.core.permissions import Permission
from app.main import create_app

pytestmark = pytest.mark.security

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})


@pytest.fixture(scope="module")
def api() -> FastAPI:
    """A fresh application, for its routing table.

    Built here rather than imported from the session `client` fixture: this suite reads
    route metadata and issues no requests, so it needs no database, no Redis, and no
    running server.
    """
    return create_app()


# ---------------------------------------------------------------------------
# The allowlists
# ---------------------------------------------------------------------------
# A route is identified by (method, path) with the mounting prefix already applied.

# Reachable with no credential at all. Every entry is a decision, and each one is
# justified: an attacker who can call these has no session to abuse.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # Liveness probes. A load balancer has no credential to offer, and neither
        # discloses anything beyond "the process is up".
        ("GET", "/health"),
        ("GET", "/health/ready"),
        # Registration must be reachable by definition — an unauthenticated visitor
        # creating their organization is the entry point. Rate limited by IP.
        ("POST", "/api/v1/auth/register"),
        # Login likewise: the credential being established is the one it would need.
        # Rate limited by IP, and the response is identical for a wrong password and an
        # unknown address.
        ("POST", "/api/v1/auth/login"),
        # Refresh authenticates with the HttpOnly cookie rather than the bearer header,
        # because the access token is precisely what has expired. The cookie is
        # SameSite=Lax, which is the CSRF control (ADR-014).
        ("POST", "/api/v1/auth/refresh"),
    }
)

# Authenticated, but requiring no capability beyond being yourself. Kept separate from
# PUBLIC_ROUTES because these routes *do* have an authenticated caller — they simply
# act on that caller's own session, which needs no capability to describe.
AUTHENTICATED_WITHOUT_CAPABILITY: frozenset[tuple[str, str]] = frozenset(
    {
        # Ending the session you are already authenticated as. A capability here would
        # mean a user could be forbidden from logging out.
        ("POST", "/api/v1/auth/logout"),
    }
)


# ---------------------------------------------------------------------------
# Walking the routing table
# ---------------------------------------------------------------------------


def _walk(dependant: object) -> Iterator[object]:
    """Every dependency in a route's tree, including nested ones.

    Recursive because the auth dependency is reached through `Context`, which depends
    on `get_tenant_context`, which depends on `get_current_user` — so checking only the
    route's immediate dependencies would report every protected route as unprotected.
    """
    yield dependant
    for child in getattr(dependant, "dependencies", []):
        yield from _walk(child)


def _calls(route: APIRoute) -> set[object]:
    """The callables in a route's dependency tree, at every depth."""
    return {node.call for node in _walk(route.dependant)}


def _required_permissions(route: APIRoute) -> tuple[Permission, ...]:
    """Every capability the route declares, gathered from all of its guards.

    A guard is recognised by the `requires` attribute `require_permission` attaches to
    it — by contract, not by inspecting the dependency's name or module, which a
    lookalike could imitate. `PermissionGuard` in `app/api/deps.py` is the type-side
    half of the same contract.
    """
    required: list[Permission] = []
    for call in _calls(route):
        required.extend(getattr(call, "requires", ()))
    return tuple(required)


def _walk_routes(routes: Iterable[object], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Every `APIRoute` in a routing table, paired with its mounted path.

    Recursive because routers are not always flattened into `app.routes`: this version
    of FastAPI keeps an included router as a container and resolves its prefix at
    request time, so a walk that looked only at the top level would find three
    documentation routes and conclude the API has no endpoints at all.

    The container is matched *structurally* — something with inner `routes` and an
    optional include prefix — rather than by importing FastAPI's private
    `_IncludedRouter`. Both that class and the flattening behaviour are
    implementation details, and this walk has to survive either. What it must not do is
    silently find nothing, which `test_the_walk_finds_every_documented_route` guarantees
    by checking the result against the OpenAPI schema FastAPI generates itself.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue

        inner = getattr(route, "original_router", None) or getattr(route, "router", None)
        if inner is None:
            # A plain Starlette route — the framework's own docs surface.
            continue

        context = getattr(route, "include_context", None)
        yield from _walk_routes(inner.routes, prefix + (getattr(context, "prefix", "") or ""))


def _routes(api: FastAPI) -> list[tuple[str, APIRoute]]:
    """Every API route, with the path it is served at."""
    return list(_walk_routes(api.routes))


def _entries(api: FastAPI) -> list[tuple[str, str, APIRoute]]:
    """`(method, path, route)` for every method of every route.

    One route yields several entries where it handles several methods, because the
    question "is this endpoint protected?" is per-method.
    """
    return [
        (method, path, route) for path, route in _routes(api) for method in sorted(route.methods)
    ]


def _discovered_public(api: FastAPI) -> frozenset[tuple[str, str]]:
    """The routes that, in fact, do not require authentication."""
    return frozenset(
        (method, path)
        for method, path, route in _entries(api)
        if get_current_user not in _calls(route)
    )


def test_there_are_routes_to_check(api: FastAPI) -> None:
    """A guard that silently inspects nothing passes forever.

    The blunt version of the cross-check below: if the walk stops finding routes, this
    says so before any of the guards do — a failure reading "no routes" is a much
    clearer signal than one listing every route as unprotected.

    The floor is a **lower bound, not a count**: it exists to catch a walk that has
    stopped finding things, so it is set below the real total and only raised when a
    phase adds a whole resource. Phases I-K took the surface from 12 to 28 by mounting
    the customers, tickets, and messages routers, and Phases L-M took it to 32 by
    mounting attachments and audit — and this is what fails if one of those
    `include_router` calls is ever dropped. The capability map below names the routes
    themselves, so a quietly missing router would otherwise only show up as an absent
    line in a dict.

    Phase P added four: the notification collection, the unread count, and the two ways
    of marking read.
    """
    assert len(_entries(api)) >= 28


def test_the_walk_finds_every_documented_route(api: FastAPI) -> None:
    """The walk's own correctness, checked against FastAPI rather than against itself.

    `_walk_routes` reaches into the routing table's structure, and if that structure
    ever changes the walk could start returning an empty list. Every guard in this file
    would then pass while inspecting nothing — the worst possible failure, because it
    would look exactly like success.

    The OpenAPI schema is generated by FastAPI from the same routing table by a
    completely separate code path, so agreement between the two is real evidence. Any
    route the walk misses shows up here.
    """
    documented = {
        (method.upper(), path)
        for path, operations in api.openapi()["paths"].items()
        for method in operations
        if method.lower() in _HTTP_METHODS
    }
    walked = {(method, path) for method, path, _ in _entries(api)}

    assert walked, "the walk found no routes at all"
    assert walked == documented, (
        f"missed: {sorted(documented - walked)}; invented: {sorted(walked - documented)}"
    )


def test_every_route_is_authenticated_or_explicitly_public(api: FastAPI) -> None:
    """The guard itself.

    Every route must carry `get_current_user` in its dependency tree. A route that does
    not is either a deliberate public endpoint — in which case it must be named above —
    or an unprotected hole.
    """
    unprotected = sorted(
        (method, path)
        for method, path, route in _entries(api)
        if get_current_user not in _calls(route) and (method, path) not in PUBLIC_ROUTES
    )

    assert not unprotected, (
        "these routes are reachable without authentication and are not on the public "
        "allowlist:\n" + "\n".join(f"  {method} {path}" for method, path in unprotected)
    )


def test_the_public_allowlist_matches_reality(api: FastAPI) -> None:
    """Compared for equality, so the allowlist cannot drift in either direction.

    A route declared public that is in fact protected is a stale entry that would
    quietly excuse a future hole; a route that became public without an entry fails the
    test above. Together the two directions mean the only way to change what is public
    is to say so here.
    """
    assert _discovered_public(api) == PUBLIC_ROUTES


def test_every_protected_route_declares_a_capability(api: FastAPI) -> None:
    """Authentication is not authorization.

    A route that knows *who* is calling but never checks *what they may do* is
    reachable by every role in the tenant — including a customer, who is an external
    party. Each protected route must therefore name a capability, or be listed as
    needing none.
    """
    undeclared = sorted(
        (method, path)
        for method, path, route in _entries(api)
        if get_current_user in _calls(route)
        and not _required_permissions(route)
        and (method, path) not in AUTHENTICATED_WITHOUT_CAPABILITY
    )

    assert not undeclared, (
        "these routes authenticate the caller but check no capability:\n"
        + "\n".join(f"  {method} {path}" for method, path in undeclared)
    )


def test_the_capability_exemptions_are_all_real_routes(api: FastAPI) -> None:
    """No stale exemption: each names a route that exists, is protected, and declares
    nothing — which is exactly what it is exempted for."""
    present = {(method, path) for method, path, _ in _entries(api)}
    by_path = {(method, path): route for method, path, route in _entries(api)}

    for entry in AUTHENTICATED_WITHOUT_CAPABILITY:
        assert entry in present, f"{entry} is exempted but is not a route"
        route = by_path[entry]
        assert get_current_user in _calls(route), f"{entry} is not protected at all"
        assert not _required_permissions(route), f"{entry} declares a capability now"

    for entry in PUBLIC_ROUTES:
        assert entry in present, f"{entry} is public but is not a route"


def test_no_route_declares_a_capability_it_cannot_have(api: FastAPI) -> None:
    """Every declared capability is a real member of the permission vocabulary.

    Guards are built from the `Permission` enum, so this is close to tautological —
    and it is still worth a line, because it fails loudly if a guard is ever built from
    a bare string.
    """
    for method, path, route in _entries(api):
        for permission in _required_permissions(route):
            assert isinstance(permission, Permission), f"{method} {path} declares {permission!r}"


# ---------------------------------------------------------------------------
# The API's own surface
# ---------------------------------------------------------------------------


def test_the_documentation_routes_are_not_api_routes(api: FastAPI) -> None:
    """`/docs` and `/openapi.json` are the framework's, and are outside this walk.

    Asserted rather than assumed: if they were `APIRoute`s the guard above would flag
    them, and the fix would be to add them to the public allowlist — which would
    misrepresent them as endpoints someone chose to leave open.

    They are also asserted present here, so this cannot pass because the docs were
    disabled rather than because they are a different kind of route.
    """
    paths = {path for path, _ in _routes(api)}
    framework_routes = {getattr(route, "path", "") for route in api.routes}

    assert not paths & {"/docs", "/openapi.json", "/redoc"}
    assert {"/docs", "/openapi.json"} <= framework_routes


def test_the_documentation_surface_is_absent_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interactive docs describe every endpoint and every schema.

    A useful development affordance and an unnecessary map of the API for anyone
    probing it. `create_app` reads the setting at construction time, so a fresh
    application built with the environment overridden is what proves the branch.

    The property is replaced on the class rather than the setting mutated on the
    instance: `Settings` is shared and validated, and a test that wrote to it could
    leave the application misconfigured for every test that followed.
    """
    from app.core.config import Settings, get_settings

    monkeypatch.setattr(Settings, "is_production", property(lambda self: True))
    assert get_settings().is_production is True

    production = create_app()
    paths = {getattr(route, "path", "") for route in production.routes}

    assert "/openapi.json" not in paths
    assert "/docs" not in paths


def test_the_walk_finds_the_auth_dependency_through_nesting(api: FastAPI) -> None:
    """`_walk` must descend, and this is what proves it does.

    Nothing depends on `get_current_user` directly. A route names `Context`, which
    names `get_tenant_context`, which names `get_current_user` — so a walk that
    inspected only a route's immediate dependencies would report every route as
    unprotected.

    Both halves are asserted: the shallow view misses it, the full walk finds it. That
    is the difference between the two views, stated as a test rather than assumed.

    (The guard above fails loudly if this ever changes, so the two cannot disagree
    silently — but a failure there would say "every route is unprotected", which is a
    confusing way to learn that `_walk` broke.)
    """
    users = _route_at(api, "GET", "/api/v1/users")

    assert get_current_user not in _immediate_calls(users)
    assert get_current_user in _calls(users)


def _route_at(api: FastAPI, method: str, path: str) -> APIRoute:
    """The route serving one method of one path.

    A helper rather than a `next(...)` at each call site, so a missing route produces
    "there is no POST /api/v1/users" instead of a bare `StopIteration`.
    """
    for entry_method, entry_path, route in _entries(api):
        if (entry_method, entry_path) == (method, path):
            return route
    raise AssertionError(f"there is no {method} {path}")


def _immediate_calls(route: APIRoute) -> set[object]:
    """Only the route's direct dependencies — the shallow view `_walk` must not settle for."""
    return {child.call for child in route.dependant.dependencies}


def test_the_guard_sees_the_permissions_a_route_declares(api: FastAPI) -> None:
    """A positive control for the introspection itself.

    Every check above is a negative — "nothing is missing". If `_required_permissions`
    were broken and always returned an empty tuple,
    `test_every_protected_route_declares_a_capability` would fail loudly, but the
    *inverse* mistake (a guard read as requiring nothing) would make this file pass
    while proving nothing. So one route's known requirement is asserted concretely.
    """
    route = _route_at(api, "POST", "/api/v1/users")

    assert Permission.USER_CREATE in _required_permissions(route)
    # And a route's *other* capabilities are not attributed to it.
    assert Permission.USER_DEACTIVATE not in _required_permissions(route)


def test_every_route_declares_the_capability_the_matrix_assigns(api: FastAPI) -> None:
    """The route → capability mapping for the whole surface, stated once.

    The API suites prove the behaviour end to end; this proves the *declaration*, and it
    is the one place the entire surface is visible at a glance. A change to any route's
    requirement has to be made here too, which is the point: a capability is a decision,
    and this is where every such decision is legible together.

    Written as equality against the full set of resource routes, so it fails in both
    directions — a route whose guard weakened, and a route that appeared without one.

    Two things it makes visible that are easy to lose in a diff:

    * Every ticket *action* carries its own capability. `/assign`, `/priority`,
      `/status`, `/close`, and `/reopen` are five routes with five different
      requirements, which is ADR-017: the capability a request needs is a property of
      where it was sent, never of its body. A single `PATCH /tickets/{id}` would have
      collapsed all five into one and made this dict four lines shorter and much less
      informative.
    * Messages live under `/tickets/{ticket_id}`. There is no `/messages` root, because
      a message has no independent access rule — it is reachable exactly when its ticket
      is (ADR-015).
    """
    declared = {
        (method, path): set(_required_permissions(route))
        for method, path, route in _entries(api)
        # Authenticated API routes only. The public ones are asserted by
        # `PUBLIC_ROUTES` above, and a route with no caller has no capability to
        # describe — including them here would make this dict a second, weaker copy of
        # that allowlist.
        if path.startswith("/api/v1/") and get_current_user in _calls(route)
    }

    assert declared == {
        # --- Auth ---------------------------------------------------------
        # Login and refresh are unreachable here: they are public, so they have no
        # capability and are not in this dict at all.
        ("GET", "/api/v1/auth/me"): {Permission.PROFILE_VIEW},
        ("POST", "/api/v1/auth/logout"): set(),
        # --- Users --------------------------------------------------------
        ("GET", "/api/v1/users"): {Permission.USER_LIST},
        ("POST", "/api/v1/users"): {Permission.USER_CREATE},
        ("GET", "/api/v1/users/{user_id}"): {Permission.USER_LIST},
        ("PATCH", "/api/v1/users/{user_id}/role"): {Permission.USER_UPDATE_ROLE},
        ("POST", "/api/v1/users/{user_id}/deactivate"): {Permission.USER_DEACTIVATE},
        # --- Customers ----------------------------------------------------
        # Reading one customer needs `CUSTOMER_LIST`: the matrix has no separate "view
        # customer" row, and this mirrors `/users/{user_id}`.
        ("GET", "/api/v1/customers"): {Permission.CUSTOMER_LIST},
        ("POST", "/api/v1/customers"): {Permission.CUSTOMER_CREATE},
        ("GET", "/api/v1/customers/{customer_id}"): {Permission.CUSTOMER_LIST},
        ("PATCH", "/api/v1/customers/{customer_id}"): {Permission.CUSTOMER_UPDATE},
        # --- Tickets ------------------------------------------------------
        ("GET", "/api/v1/tickets"): {Permission.TICKET_LIST},
        ("POST", "/api/v1/tickets"): {Permission.TICKET_CREATE},
        ("GET", "/api/v1/tickets/{ticket_id}"): {Permission.TICKET_VIEW},
        # The timeline is part of seeing the ticket; §3 has no row for it.
        ("GET", "/api/v1/tickets/{ticket_id}/events"): {Permission.TICKET_VIEW},
        ("POST", "/api/v1/tickets/{ticket_id}/assign"): {Permission.TICKET_ASSIGN},
        ("POST", "/api/v1/tickets/{ticket_id}/priority"): {Permission.TICKET_CHANGE_PRIORITY},
        ("POST", "/api/v1/tickets/{ticket_id}/status"): {Permission.TICKET_CHANGE_STATUS},
        ("POST", "/api/v1/tickets/{ticket_id}/close"): {Permission.TICKET_CLOSE},
        ("POST", "/api/v1/tickets/{ticket_id}/reopen"): {Permission.TICKET_REOPEN},
        # --- Messages -----------------------------------------------------
        # Reading is one route serving two audiences; posting is two routes with two
        # capabilities, because the audience is a property of the route and not of the
        # body. `MESSAGE_READ_INTERNAL` is deliberately absent from the read route: it
        # decides what a caller *sees*, not whether they may call it, and it is applied
        # in the service where a route cannot express it.
        ("GET", "/api/v1/tickets/{ticket_id}/messages"): {Permission.MESSAGE_READ_PUBLIC},
        ("POST", "/api/v1/tickets/{ticket_id}/messages"): {Permission.MESSAGE_POST_REPLY},
        ("POST", "/api/v1/tickets/{ticket_id}/notes"): {Permission.MESSAGE_POST_INTERNAL},
        # --- Attachments --------------------------------------------------
        # Listing takes `ATTACHMENT_DOWNLOAD` rather than a capability of its own: §3's
        # matrix has no "list attachments" row, and a metadata list is only useful to
        # someone who may fetch the file. Same reasoning as `/customers/{id}` →
        # `CUSTOMER_LIST`.
        #
        # The download is at `/attachments/{id}` rather than under `/tickets`, because a
        # download has only an id to go on. It is scoped just as the list is — both
        # resolve the ticket underneath before returning anything.
        ("POST", "/api/v1/tickets/{ticket_id}/attachments"): {Permission.ATTACHMENT_UPLOAD},
        ("GET", "/api/v1/tickets/{ticket_id}/attachments"): {Permission.ATTACHMENT_DOWNLOAD},
        ("GET", "/api/v1/attachments/{attachment_id}"): {Permission.ATTACHMENT_DOWNLOAD},
        # --- Audit --------------------------------------------------------
        # One route, admin-only via §3 row 87. There is no write route because an audit
        # trail a client can write is not an audit trail; every row is written from
        # inside the transaction of the action it describes.
        ("GET", "/api/v1/audit-logs"): {Permission.AUDIT_VIEW},
        # --- Notifications ------------------------------------------------
        # All four carry the same capability, because §3's matrix gives notifications to
        # every role: a notification is addressed to a person rather than to a job, so
        # there is nothing for a role to widen or narrow. The restriction is per-row and
        # lives in the repository — every query is filtered to `user_id == the caller`,
        # which is why four routes can share one capability without sharing one audience.
        #
        # Two of the four are the same idea at different scopes: `/read` clears one,
        # `/read-all` clears the badge. `unread-count` exists as its own route rather than
        # being derived from the list, so the cheapest question costs a `COUNT`.
        ("GET", "/api/v1/notifications"): {Permission.NOTIFICATION_LIST},
        ("GET", "/api/v1/notifications/unread-count"): {Permission.NOTIFICATION_LIST},
        ("POST", "/api/v1/notifications/read-all"): {Permission.NOTIFICATION_LIST},
        ("POST", "/api/v1/notifications/{notification_id}/read"): {Permission.NOTIFICATION_LIST},
        # --- SLA ----------------------------------------------------------
        # Two routes with two different capabilities, and the asymmetry is §3's matrix
        # rather than a choice made here. Reading the targets is `SLA_VIEW`, which admin,
        # manager, and agent all hold — an agent working to a deadline needs to know what
        # the deadline is. Setting them is `SLA_CONFIGURE`, admin alone: the targets are a
        # business commitment, and a manager who wants the operational ceiling raised asks
        # rather than grants it.
        #
        # Addressed by priority rather than by id, so there is no `GET
        # /sla/policies/{policy_id}` to list: a policy *is* its priority within a tenant.
        # A ticket's own position is not here at all — it is `TicketRead.sla`, computed at
        # read time, because a second path to one number is a second place for the
        # authorization decision to be got wrong.
        ("GET", "/api/v1/sla/policies"): {Permission.SLA_VIEW},
        ("PATCH", "/api/v1/sla/policies/{priority}"): {Permission.SLA_CONFIGURE},
    }
