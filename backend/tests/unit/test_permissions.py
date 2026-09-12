"""The permission matrix, asserted against docs/requirements.md §3.

The matrix below is transcribed **independently** of `app/core/permissions.py` — from
the documentation table, not from the implementation. That is the whole point: if the
two ever disagree, this file fails, and the failure names the exact capability and
role. A test that imported the mapping and asserted it equalled itself would pass
while the application granted an agent the right to reassign tickets.

The second half guards the *shape* of the code rather than its values: Phase G
requires centralized permission checks, and a source scan is what keeps the next
resource from quietly adding a fifth place where a role is compared.
"""

import dataclasses
import re
import uuid
from pathlib import Path
from typing import cast

import pytest

from app.core.permissions import (
    ATTACHMENT_SCOPE_BY_ROLE,
    MESSAGE_SCOPE_BY_ROLE,
    PORTAL_ROLES,
    ROLE_PERMISSIONS,
    SENDER_TYPE_BY_ROLE,
    TICKET_SCOPE_BY_ROLE,
    Permission,
    RowScope,
    has_permission,
    permissions_for,
    row_scope_for,
)
from app.core.tenancy import TenantContext
from app.models.enums import SenderType, UserRole

pytestmark = pytest.mark.unit

ADMIN = UserRole.ADMIN
MANAGER = UserRole.MANAGER
AGENT = UserRole.AGENT
CUSTOMER = UserRole.CUSTOMER
ROLES = (ADMIN, MANAGER, AGENT, CUSTOMER)

# Transcribed from docs/requirements.md §3, row for row. `True` is `✓`, `False` is
# `—`. The `own` and `assigned` qualifiers are recorded in the scope maps below
# rather than here, because they restrict which rows a capability reaches and not
# whether the role holds it at all.
MATRIX: dict[Permission, dict[UserRole, bool]] = {
    # --- Organization -----------------------------------------------------
    Permission.ORG_VIEW: {ADMIN: True, MANAGER: True, AGENT: False, CUSTOMER: False},
    Permission.ORG_UPDATE: {ADMIN: True, MANAGER: False, AGENT: False, CUSTOMER: False},
    # --- Users ------------------------------------------------------------
    Permission.USER_LIST: {ADMIN: True, MANAGER: True, AGENT: False, CUSTOMER: False},
    Permission.USER_CREATE: {ADMIN: True, MANAGER: False, AGENT: False, CUSTOMER: False},
    Permission.USER_UPDATE_ROLE: {ADMIN: True, MANAGER: False, AGENT: False, CUSTOMER: False},
    Permission.USER_DEACTIVATE: {ADMIN: True, MANAGER: False, AGENT: False, CUSTOMER: False},
    Permission.PROFILE_VIEW: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
    # --- Customers --------------------------------------------------------
    # No customer directory for customers themselves — they see their own tickets,
    # not the organization's other customers.
    Permission.CUSTOMER_LIST: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    Permission.CUSTOMER_CREATE: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    Permission.CUSTOMER_UPDATE: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    # --- Tickets ----------------------------------------------------------
    # "List all org tickets", "List assigned tickets", and "List own tickets" are one
    # capability with three scopes; see TICKET_SCOPE_BY_ROLE.
    Permission.TICKET_LIST: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
    Permission.TICKET_CREATE: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
    Permission.TICKET_VIEW: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
    Permission.TICKET_ASSIGN: {ADMIN: True, MANAGER: True, AGENT: False, CUSTOMER: False},
    Permission.TICKET_CHANGE_PRIORITY: {ADMIN: True, MANAGER: True, AGENT: False, CUSTOMER: False},
    Permission.TICKET_CHANGE_STATUS: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    Permission.TICKET_CLOSE: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
    Permission.TICKET_REOPEN: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
    # --- Messages ---------------------------------------------------------
    Permission.MESSAGE_READ_PUBLIC: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
    # Internal notes are staff-only, at every scope.
    Permission.MESSAGE_READ_INTERNAL: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    Permission.MESSAGE_POST_REPLY: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
    Permission.MESSAGE_POST_INTERNAL: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    # --- Attachments ------------------------------------------------------
    Permission.ATTACHMENT_UPLOAD: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
    Permission.ATTACHMENT_DOWNLOAD: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
    # --- AI ---------------------------------------------------------------
    Permission.AI_REQUEST_ANALYSIS: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    Permission.AI_REQUEST_SUGGESTION: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    Permission.AI_QUERY_KNOWLEDGE: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    Permission.AI_CONFIGURE: {ADMIN: True, MANAGER: False, AGENT: False, CUSTOMER: False},
    Permission.AI_VIEW_USAGE: {ADMIN: True, MANAGER: True, AGENT: False, CUSTOMER: False},
    # --- Knowledge base ---------------------------------------------------
    Permission.KB_LIST: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    Permission.KB_UPLOAD: {ADMIN: True, MANAGER: False, AGENT: False, CUSTOMER: False},
    Permission.KB_DELETE: {ADMIN: True, MANAGER: False, AGENT: False, CUSTOMER: False},
    # --- SLA --------------------------------------------------------------
    Permission.SLA_VIEW: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    Permission.SLA_CONFIGURE: {ADMIN: True, MANAGER: False, AGENT: False, CUSTOMER: False},
    # --- Analytics --------------------------------------------------------
    Permission.ANALYTICS_ORG: {ADMIN: True, MANAGER: True, AGENT: False, CUSTOMER: False},
    Permission.ANALYTICS_OWN: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: False},
    # --- Audit ------------------------------------------------------------
    Permission.AUDIT_VIEW: {ADMIN: True, MANAGER: False, AGENT: False, CUSTOMER: False},
    # --- Notifications ----------------------------------------------------
    Permission.NOTIFICATION_LIST: {ADMIN: True, MANAGER: True, AGENT: True, CUSTOMER: True},
}


# ---------------------------------------------------------------------------
# The matrix itself
# ---------------------------------------------------------------------------


def test_the_matrix_covers_every_permission() -> None:
    """A capability with no documented row is a capability with no stated policy.

    This is the assertion that catches a permission added to the enum without a
    corresponding §3 entry — the gap would otherwise be silent, because a permission
    nothing grants simply never fires.
    """
    documented = set(MATRIX)
    declared = set(Permission)

    assert documented == declared, (
        f"undocumented: {sorted(declared - documented)}; "
        f"stale rows: {sorted(documented - declared)}"
    )


@pytest.mark.parametrize("role", ROLES)
def test_role_permissions_match_the_documented_matrix(role: UserRole) -> None:
    """The one assertion that keeps the implementation honest.

    Reports every disagreement at once rather than stopping at the first, so a
    mistaken transcription is fixed in one pass.
    """
    expected = {permission for permission, by_role in MATRIX.items() if by_role[role]}
    actual = set(permissions_for(role))

    assert actual == expected, (
        f"{role.value} — granted but not documented: "
        f"{sorted(p.value for p in actual - expected)}; "
        f"documented but not granted: {sorted(p.value for p in expected - actual)}"
    )


def test_the_roles_are_exactly_the_four_in_the_matrix() -> None:
    assert set(ROLE_PERMISSIONS) == set(ROLES)


def test_every_role_holds_at_least_one_permission() -> None:
    """A role with no capabilities is a role that cannot use the product.

    Cheap to assert and it catches the failure mode where a mapping is emptied or a
    name is mistyped, which otherwise only shows up as a customer who can do nothing.
    """
    for role in ROLES:
        assert permissions_for(role), f"{role.value} holds no permissions"


# ---------------------------------------------------------------------------
# The properties that matter most
# ---------------------------------------------------------------------------


def test_only_an_admin_holds_every_permission() -> None:
    """The admin column is `✓` on every row; no other column is."""
    all_permissions = frozenset(Permission)

    assert permissions_for(ADMIN) == all_permissions
    for role in (MANAGER, AGENT, CUSTOMER):
        assert permissions_for(role) < all_permissions, f"{role.value} holds everything"


def test_the_admin_only_capabilities_are_exactly_the_ones_the_matrix_reserves() -> None:
    """Written out explicitly: these are the capabilities whose accidental grant is
    an incident rather than a bug, so the list is asserted rather than derived."""
    admin_only = {
        Permission.ORG_UPDATE,
        Permission.USER_CREATE,
        Permission.USER_UPDATE_ROLE,
        Permission.USER_DEACTIVATE,
        Permission.AI_CONFIGURE,
        Permission.KB_UPLOAD,
        Permission.KB_DELETE,
        Permission.SLA_CONFIGURE,
        Permission.AUDIT_VIEW,
    }

    for permission in admin_only:
        holders = {role for role in ROLES if has_permission(role, permission)}
        assert holders == {ADMIN}, f"{permission.value} is held by {holders}"


def test_a_customer_is_denied_every_staff_capability() -> None:
    """The customer role is the untrusted one — it is the external party.

    Every capability listed here crosses the boundary between "my own ticket" and
    "the organization's operations".
    """
    forbidden = {
        Permission.ORG_VIEW,
        Permission.ORG_UPDATE,
        Permission.USER_LIST,
        Permission.USER_CREATE,
        Permission.USER_UPDATE_ROLE,
        Permission.USER_DEACTIVATE,
        Permission.CUSTOMER_LIST,
        Permission.CUSTOMER_CREATE,
        Permission.CUSTOMER_UPDATE,
        Permission.TICKET_ASSIGN,
        Permission.TICKET_CHANGE_PRIORITY,
        Permission.TICKET_CHANGE_STATUS,
        Permission.MESSAGE_READ_INTERNAL,
        Permission.MESSAGE_POST_INTERNAL,
        Permission.AI_REQUEST_ANALYSIS,
        Permission.AI_REQUEST_SUGGESTION,
        Permission.AI_QUERY_KNOWLEDGE,
        Permission.AI_CONFIGURE,
        Permission.AI_VIEW_USAGE,
        Permission.KB_LIST,
        Permission.KB_UPLOAD,
        Permission.KB_DELETE,
        Permission.SLA_VIEW,
        Permission.SLA_CONFIGURE,
        Permission.ANALYTICS_ORG,
        Permission.ANALYTICS_OWN,
        Permission.AUDIT_VIEW,
    }

    for permission in forbidden:
        assert not has_permission(CUSTOMER, permission), f"customer holds {permission.value}"


def test_a_customer_can_still_use_the_product() -> None:
    """The complement of the test above.

    "Denied everything" and "denied everything except the one thing that was
    forgotten" look identical from a denied-by-default implementation, so the
    capabilities a customer *must* keep are asserted positively.
    """
    required = {
        Permission.PROFILE_VIEW,
        Permission.TICKET_LIST,
        Permission.TICKET_CREATE,
        Permission.TICKET_VIEW,
        Permission.TICKET_CLOSE,
        Permission.TICKET_REOPEN,
        Permission.MESSAGE_READ_PUBLIC,
        Permission.MESSAGE_POST_REPLY,
        Permission.ATTACHMENT_UPLOAD,
        Permission.ATTACHMENT_DOWNLOAD,
        Permission.NOTIFICATION_LIST,
    }

    for permission in required:
        assert has_permission(CUSTOMER, permission), f"customer lacks {permission.value}"


def test_an_agent_cannot_supervise_other_agents_work() -> None:
    """Assignment and priority stay with management even though the agent holds the
    ticket capabilities themselves — the distinction the scope map encodes."""
    assert not has_permission(AGENT, Permission.TICKET_ASSIGN)
    assert not has_permission(AGENT, Permission.TICKET_CHANGE_PRIORITY)

    # And the capabilities the agent does hold are real.
    assert has_permission(AGENT, Permission.TICKET_CHANGE_STATUS)
    assert has_permission(AGENT, Permission.TICKET_CLOSE)


def test_a_manager_cannot_administer_the_organization() -> None:
    """Manager is a supervisory role, not a second admin."""
    for permission in (
        Permission.ORG_UPDATE,
        Permission.USER_CREATE,
        Permission.USER_UPDATE_ROLE,
        Permission.USER_DEACTIVATE,
        Permission.AI_CONFIGURE,
        Permission.KB_UPLOAD,
        Permission.KB_DELETE,
        Permission.SLA_CONFIGURE,
        Permission.AUDIT_VIEW,
    ):
        assert not has_permission(MANAGER, permission), f"manager holds {permission.value}"


def test_has_permission_agrees_with_permissions_for() -> None:
    """The single-permission helper is a view of the set, not a second policy."""
    for role in ROLES:
        granted = permissions_for(role)
        for permission in Permission:
            assert has_permission(role, permission) is (permission in granted)


def test_an_unknown_role_is_granted_nothing() -> None:
    """Unreachable through the enum, which is exactly why it is worth pinning.

    Denied-by-default is the only safe direction for a lookup that cannot find an
    answer, and this is the assertion that keeps it that way. `UserRole` is a `str`
    subclass, so a lookalike value is what a stray string from the database or a token
    claim would actually be.
    """
    impostor = cast("UserRole", "not-a-role")

    assert permissions_for(impostor) == frozenset()
    assert has_permission(impostor, Permission.PROFILE_VIEW) is False


# ---------------------------------------------------------------------------
# Row scope
# ---------------------------------------------------------------------------

SCOPE_MAPS = {
    "ticket": TICKET_SCOPE_BY_ROLE,
    "message": MESSAGE_SCOPE_BY_ROLE,
    "attachment": ATTACHMENT_SCOPE_BY_ROLE,
}


@pytest.mark.parametrize("resource", sorted(SCOPE_MAPS))
def test_the_scope_maps_cover_every_role(resource: str) -> None:
    """A role missing from a scope map would fall back to `OWN`, silently narrowing
    an admin's view rather than raising — so the coverage is asserted."""
    assert set(SCOPE_MAPS[resource]) == set(ROLES)


@pytest.mark.parametrize("resource", sorted(SCOPE_MAPS))
def test_the_scopes_match_the_matrix_qualifiers(resource: str) -> None:
    """§3's `all` / `assigned` / `own` qualifiers, row for row.

    Admin and manager see the whole organization; an agent sees only assigned work;
    a customer sees only their own. Messages and attachments inherit the ticket rule,
    which is the relationship the mapping is meant to express.
    """
    scopes = SCOPE_MAPS[resource]

    assert scopes[ADMIN] is RowScope.ORGANIZATION
    assert scopes[MANAGER] is RowScope.ORGANIZATION
    assert scopes[AGENT] is RowScope.ASSIGNED
    assert scopes[CUSTOMER] is RowScope.OWN


def test_the_three_scope_maps_agree_today() -> None:
    """Separate objects, one rule — for now.

    They are kept apart so a later divergence is a one-line change rather than a
    redesign. Until that divergence exists, they are required to agree; this test is
    what will make the change deliberate, since it will have to be edited.
    """
    assert TICKET_SCOPE_BY_ROLE == MESSAGE_SCOPE_BY_ROLE == ATTACHMENT_SCOPE_BY_ROLE


def test_an_unknown_role_resolves_to_the_narrowest_scope() -> None:
    """`OWN` rather than `ORGANIZATION`: an unrecognised role sees the least."""
    impostor = cast("UserRole", "not-a-role")

    assert row_scope_for(impostor, TICKET_SCOPE_BY_ROLE) is RowScope.OWN


# ---------------------------------------------------------------------------
# Role-derived attributes
# ---------------------------------------------------------------------------
# Two facts about a role that are not authorization decisions but are still role
# knowledge, so they live beside the matrix rather than being repeated wherever they
# are needed. `test_no_module_outside_permissions_compares_a_role` is what enforces
# that: it permits only `UserRole.ADMIN` outside this module, so a second
# `role is UserRole.CUSTOMER` anywhere would fail that guard.


def test_the_portal_roles_are_exactly_the_roles_with_an_own_scope() -> None:
    """The two must not be able to disagree.

    `PORTAL_ROLES` decides which accounts require a linked `Customer`, and
    `TICKET_SCOPE_BY_ROLE` decides which accounts are narrowed to their own rows. They
    are the same set of roles — an account that is scoped to its own records is exactly
    an account that needs to *have* records — but they are written separately because
    they are consumed in different layers.

    If they ever diverged, the failure would be subtle and bad in one of two ways: a
    role scoped to `OWN` with no `customer_id` would be silently refused everything, or
    a role with a `customer_id` but organization-wide scope would read every customer's
    tickets. Deriving one from the other would hide that; asserting they agree makes it
    a decision someone has to make on purpose.
    """
    own_scoped = {role for role, scope in TICKET_SCOPE_BY_ROLE.items() if scope is RowScope.OWN}

    assert set(PORTAL_ROLES) == own_scoped


def test_every_role_has_a_sender_type() -> None:
    """A message's `sender_type` follows from its author's role.

    The lookup in `message_service` is written with a fail-closed default, so a missing
    key would produce a plausible wrong answer — a staff member's note recorded as
    having come from the customer, which is the one direction that must never happen.
    Coverage is asserted here so the default is unreachable rather than load-bearing.
    """
    assert set(SENDER_TYPE_BY_ROLE) == set(ROLES)


def test_only_a_portal_role_sends_as_the_customer() -> None:
    """The mapping's one meaningful distinction, stated directly.

    Everything else about it is "staff send as an agent". This asserts the direction
    that matters: no non-portal role can produce a message attributed to the customer,
    and the customer cannot produce one attributed to staff.
    """
    for role, sender_type in SENDER_TYPE_BY_ROLE.items():
        expected = SenderType.CUSTOMER if role in PORTAL_ROLES else SenderType.AGENT
        assert sender_type is expected, f"{role.value} sends as {sender_type.value}"


# ---------------------------------------------------------------------------
# TenantContext
# ---------------------------------------------------------------------------


def test_a_context_derives_its_permissions_from_the_role() -> None:
    """Nothing constructs a context with a permission set of its own. The role is the
    only input, which is what makes "the database role is authoritative" enforceable."""
    context = TenantContext(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role=MANAGER)

    assert context.permissions == permissions_for(MANAGER)
    assert context.role is MANAGER


def test_a_context_cannot_have_its_permissions_rewritten() -> None:
    """Frozen, and the derived field is derived on construction.

    A mutable context would allow a caller to widen its own permissions after
    authentication — the dependency builds it once, and it must stay that way.
    """
    context = TenantContext(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role=CUSTOMER)

    with pytest.raises(dataclasses.FrozenInstanceError):
        context.role = ADMIN  # type: ignore[misc]


def test_has_requires_every_permission_it_is_given() -> None:
    """Conjunctive, so a route can state its full requirement in one call.

    Disjunctive would mean `require_permission(A, B)` silently granting access to
    anyone holding either one — a plausible misreading that this pins down.
    """
    context = TenantContext(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role=AGENT)

    assert context.has(Permission.PROFILE_VIEW)
    assert context.has(Permission.TICKET_LIST, Permission.MESSAGE_POST_REPLY)
    assert not context.has(Permission.TICKET_LIST, Permission.USER_CREATE)
    assert not context.has(Permission.USER_CREATE)
    # Vacuously true, and never a useful call — but defined rather than surprising.
    assert context.has()


def test_a_context_resolves_its_own_row_scope() -> None:
    context = TenantContext(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role=CUSTOMER)
    assert context.scope_for(TICKET_SCOPE_BY_ROLE) is RowScope.OWN


# ---------------------------------------------------------------------------
# Centralization
# ---------------------------------------------------------------------------
# Phase G: "no bare role == comparison outside app/core/permissions.py". These tests
# read the source tree, because the property being asserted is about the codebase and
# cannot be observed from the running application — a scattered role check works
# perfectly until someone updates one of the copies.

APP_DIR = Path(__file__).resolve().parents[2] / "app"

# The only places a specific role name may appear, each for a reason that is *not*
# authorization. Everything here is the "last admin" invariant — a structural fact
# about an organization (it must retain one administrator), not a decision about who
# may do what. Authorization decisions must all resolve through ROLE_PERMISSIONS.
_ROLE_NAME_EXCEPTIONS = {
    # `count_admins` — the invariant's query.
    "repositories/user_repository.py": "last-admin invariant: counts administrators",
    # `update_role` / `deactivate_user` — refuse to remove the final admin.
    "services/user_service.py": "last-admin invariant: refuses to strand the org",
    # Registration mints the organization's first user, who must be an admin.
    "services/auth_service.py": "assigns ADMIN to a newly registered organization",
}

# Definition sites, not uses.
_ROLE_DEFINITION_FILES = {"models/enums.py"}

_ROLE_NAME = re.compile(r"\bUserRole\.(ADMIN|MANAGER|AGENT|CUSTOMER)\b")
# `role == "admin"`, `role: str = "manager"` — the string form of the same mistake.
_ROLE_LITERAL = re.compile(
    r"\brole\w*\s*(?:==|!=|is\s+not|is|:|=)\s*(?:str\s*=\s*)?[\"'](admin|manager|agent|customer)[\"']"
)


def _app_sources() -> list[Path]:
    return sorted(APP_DIR.rglob("*.py"))


def test_no_module_outside_permissions_compares_a_role() -> None:
    """The mechanical guard against scattered role checks.

    Two rules, both checkable:

    * No file may reference `UserRole.MANAGER`, `.AGENT`, or `.CUSTOMER` — only
      `permissions.py` knows what those roles may do. An exception list rather than a
      blanket ban, so the exemptions are visible and reviewed rather than assumed.
    * The files that *are* exempt may reference `UserRole.ADMIN` only. Their business
      is the last-admin invariant, which is about one role; a reference to any other
      role in one of them would be an authorization decision in disguise.
    """
    offenders: list[str] = []

    for path in _app_sources():
        relative = path.relative_to(APP_DIR).as_posix()
        if relative == "core/permissions.py" or relative in _ROLE_DEFINITION_FILES:
            continue

        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in _ROLE_NAME.finditer(line):
                role = match.group(1)
                if relative not in _ROLE_NAME_EXCEPTIONS:
                    offenders.append(f"{relative}:{number} references UserRole.{role}")
                elif role != "ADMIN":
                    offenders.append(
                        f"{relative}:{number} references UserRole.{role}, but this file "
                        f"is exempt only for the last-admin invariant "
                        f"({_ROLE_NAME_EXCEPTIONS[relative]})"
                    )

    assert not offenders, "scattered role checks:\n" + "\n".join(offenders)


def test_no_module_outside_permissions_compares_a_role_string() -> None:
    """The string form of the same mistake — `role == "admin"` bypasses the enum
    entirely and would not be caught by the check above."""
    offenders: list[str] = []

    for path in _app_sources():
        relative = path.relative_to(APP_DIR).as_posix()
        if relative in _ROLE_DEFINITION_FILES or relative == "core/permissions.py":
            continue

        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _ROLE_LITERAL.search(line):
                offenders.append(f"{relative}:{number} — {line.strip()}")

    assert not offenders, "role names compared as strings:\n" + "\n".join(offenders)


def test_the_exception_list_has_not_gone_stale() -> None:
    """Every exemption must still be earning itself.

    An exception left behind after its code is refactored away is a hole in the guard
    that nobody would notice, because a stale entry still passes.
    """
    for relative, reason in _ROLE_NAME_EXCEPTIONS.items():
        path = APP_DIR / relative
        assert path.exists(), f"{relative} is exempted but does not exist"
        assert _ROLE_NAME.search(path.read_text(encoding="utf-8")), (
            f"{relative} is exempted for {reason!r} but no longer references a role"
        )
