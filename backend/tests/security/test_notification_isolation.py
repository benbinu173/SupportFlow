"""Notification isolation — one inbox per person, and one tenant per inbox.

Notifications are the only resource in this application whose access rule is *not* a role
scope. §3's matrix gives `NOTIFICATION_LIST` to all four roles, so the guard on every
route is uniform, and everything that narrows a query lives in one predicate:
`user_id == context.user_id`. That makes this file the whole of the authorization story
for the feature, and it has two axes rather than the usual one.

**Across tenants.** Two organizations, and a notification id from one asked for by the
other. Nothing in the request carries a tenant — the id is the only input — so the tenant
predicate is the `TenantScopedRepository` half of the query and nothing else is standing
in for it.

**Across users in one tenant.** The axis that does not exist anywhere else here. Two
agents, one ticket between them, and the notification the first one was sent asked for by
the second. Both agents hold the same capability, are in the same organization, and may
legitimately read the same ticket — so nothing about the request is wrong except that the
row is not theirs. A predicate dropped from `NotificationRepository._own` is invisible to
a role-based test and shows up only here.

**Every refusal is followed by proof the row was untouched**, and that second assertion is
the point rather than a flourish. A 404 that marked the notification read on the way to
refusing would leave the owner's unread badge wrong, and the status code alone cannot see
it. `tests/api/test_notifications.py` makes the same check for the single-user case; here
it is made against a row in another tenant, which is the case where a bug would be a
cross-tenant write.
"""

import uuid
from collections.abc import Callable

import pytest

from tests.conftest import NOTIFICATIONS, TICKETS, OrgSession
from tests.security.test_tenant_isolation import assert_indistinguishable_from_a_missing_record

pytestmark = pytest.mark.security


@pytest.fixture(autouse=True)
def _isolate(truncate_tables: None) -> None:
    """Truncate after every test in this file, as the other isolation file does."""
    # Nothing to do in the body: `truncate_tables` yields, so depending on it is what
    # places the truncation on the far side of the test.


@pytest.fixture
def two_orgs(register_org: Callable[..., OrgSession]) -> tuple[OrgSession, OrgSession]:
    """Two unrelated organizations, each with an authenticated admin."""
    return register_org(organization_name="Northwind"), register_org(organization_name="Southwind")


@pytest.fixture
def notified(register_org: Callable[..., OrgSession]) -> dict[str, object]:
    """One organization with a notification addressed to a named agent.

    Returns the pieces rather than only the id, because every test here needs to ask a
    *different* principal for the same row — and several need to check afterwards that
    the row is unchanged, which means knowing who owns it.
    """
    org = register_org(organization_name="Notify Co")
    agent = org.add_user("agent", email="agent@notifyco.com")
    colleague = org.add_user("agent", email="colleague@notifyco.com")
    record = org.add_customer(name="Ada Lovelace", email="ada@analytical.com")
    ticket = org.add_ticket(record["id"], subject="Printer on fire")

    assigned = org.post(
        f"{TICKETS}/{ticket['id']}/assign", json={"assigned_agent_id": agent.user_id}
    )
    assert assigned.status_code == 200, assigned.text

    listed = agent.get(NOTIFICATIONS)
    assert listed.status_code == 200, listed.text
    notifications = listed.json()
    assert len(notifications) == 1

    return {"org": org, "agent": agent, "colleague": colleague, "notification": notifications[0]}


def unread(session: OrgSession) -> int:
    response = session.get(f"{NOTIFICATIONS}/unread-count")
    assert response.status_code == 200, response.text
    return int(response.json()["unread"])


# ---------------------------------------------------------------------------
# Across tenants
# ---------------------------------------------------------------------------


def test_another_organization_cannot_read_a_notification(
    notified: dict[str, object], two_orgs: tuple[OrgSession, OrgSession]
) -> None:
    """The id is a uuid and the caller is authenticated; the tenant predicate is all there is."""
    notification = notified["notification"]
    assert isinstance(notification, dict)
    southwind = two_orgs[1]

    response = southwind.post(f"{NOTIFICATIONS}/{notification['id']}/read")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOTIFICATION_NOT_FOUND"


def test_a_cross_tenant_read_is_indistinguishable_from_a_missing_one(
    notified: dict[str, object], two_orgs: tuple[OrgSession, OrgSession]
) -> None:
    """Compared in full against a uuid nobody wrote, using the tenant suite's own helper.

    The helper is imported rather than reimplemented, because the property being asserted
    is the same one and a second copy of the comparison would be free to drift from it.
    """
    notification = notified["notification"]
    assert isinstance(notification, dict)

    cross_tenant = two_orgs[1].post(f"{NOTIFICATIONS}/{notification['id']}/read")
    never_existed = two_orgs[1].post(f"{NOTIFICATIONS}/{uuid.uuid4()}/read")

    assert_indistinguishable_from_a_missing_record(cross_tenant, never_existed)


def test_a_refused_cross_tenant_read_leaves_the_row_untouched(
    notified: dict[str, object], two_orgs: tuple[OrgSession, OrgSession]
) -> None:
    """The second half of the claim: nothing was written on the way to the 404.

    Worth its own test because the failure it catches is silent. A handler that fetched
    the row by id, marked it read, and *then* checked the tenant would return the same 404
    as the correct one — and the only evidence would be the owner's badge, in a different
    organization, counting down.
    """
    notification = notified["notification"]
    agent = notified["agent"]
    assert isinstance(notification, dict)
    assert isinstance(agent, OrgSession)

    assert two_orgs[1].post(f"{NOTIFICATIONS}/{notification['id']}/read").status_code == 404

    assert unread(agent) == 1
    assert agent.get(NOTIFICATIONS).json()[0]["read_at"] is None


def test_a_cross_tenant_read_all_cannot_clear_another_organization_s_inbox(
    notified: dict[str, object], two_orgs: tuple[OrgSession, OrgSession]
) -> None:
    """`read-all` is the route with no id in it, so it is the one that could go wrong quietly.

    There is nothing in the request to point at the wrong tenant — only the predicate in
    the `UPDATE`. And the response is not an error in the buggy case: it reports
    `marked_read: 0`, which is exactly what a correct implementation reports for an empty
    inbox. So the assertion that matters is the last one, on the owner's badge.
    """
    agent = notified["agent"]
    assert isinstance(agent, OrgSession)

    cleared = two_orgs[1].post(f"{NOTIFICATIONS}/read-all")

    assert cleared.status_code == 200
    assert cleared.json() == {"marked_read": 0}
    assert unread(agent) == 1


def test_a_cross_tenant_unread_count_is_zero_not_the_other_tenant_s(
    notified: dict[str, object], two_orgs: tuple[OrgSession, OrgSession]
) -> None:
    """A count is a disclosure even when it names nothing.

    "Two unread" from an organization whose users have no notifications would confirm
    that somebody else's rows are reachable, without returning a single one of them.
    """
    counted = two_orgs[1].get(f"{NOTIFICATIONS}/unread-count")

    assert counted.status_code == 200
    assert counted.json() == {"unread": 0}


# ---------------------------------------------------------------------------
# Across users in one tenant
# ---------------------------------------------------------------------------


def test_a_colleague_cannot_read_a_notification(notified: dict[str, object]) -> None:
    """Same tenant, same role, same capability, and the row is still not theirs.

    This is the axis a role-based test cannot see. `NOTIFICATION_LIST` is held by every
    role, so a request from the colleague is authorized by every check the API makes
    before it reaches the query — and the query is the only thing that can refuse it.
    """
    notification = notified["notification"]
    colleague = notified["colleague"]
    assert isinstance(notification, dict)
    assert isinstance(colleague, OrgSession)

    response = colleague.post(f"{NOTIFICATIONS}/{notification['id']}/read")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOTIFICATION_NOT_FOUND"


def test_a_colleague_s_refusal_is_indistinguishable_from_a_missing_one(
    notified: dict[str, object],
) -> None:
    """The same comparison as across tenants, on the axis where the capability is identical.

    A `403` here would be defensible in isolation — the caller *may* read notifications,
    just not this one — and it would still hand a colleague a way to test whether an id
    they guessed belongs to someone they know. So it is a 404, and the same 404.
    """
    notification = notified["notification"]
    colleague = notified["colleague"]
    assert isinstance(notification, dict)
    assert isinstance(colleague, OrgSession)

    theirs = colleague.post(f"{NOTIFICATIONS}/{notification['id']}/read")
    absent = colleague.post(f"{NOTIFICATIONS}/{uuid.uuid4()}/read")

    assert_indistinguishable_from_a_missing_record(theirs, absent)


def test_a_colleague_cannot_see_it_in_their_inbox(notified: dict[str, object]) -> None:
    """A read that is refused and a read that returns nothing are different failures.

    The list is where a dropped predicate would be least visible of all: it would return
    the colleague's *own* notifications plus somebody else's, and every row in the
    response would validate against the schema.
    """
    colleague = notified["colleague"]
    assert isinstance(colleague, OrgSession)

    assert colleague.get(NOTIFICATIONS).json() == []
    assert colleague.get(NOTIFICATIONS, params={"unread_only": True}).json() == []
    assert unread(colleague) == 0


def test_a_refused_colleague_read_leaves_the_row_untouched(notified: dict[str, object]) -> None:
    """The 404 must not have marked it read on the way out."""
    notification = notified["notification"]
    agent = notified["agent"]
    colleague = notified["colleague"]
    assert isinstance(notification, dict)
    assert isinstance(agent, OrgSession)
    assert isinstance(colleague, OrgSession)

    assert colleague.post(f"{NOTIFICATIONS}/{notification['id']}/read").status_code == 404

    assert unread(agent) == 1
    assert agent.get(NOTIFICATIONS).json()[0]["read_at"] is None


def test_a_colleague_s_read_all_cannot_clear_another_user_s_inbox(
    notified: dict[str, object],
) -> None:
    """Two agents in one organization, one of whom has nothing to clear.

    The `UPDATE` in `mark_all_read` carries both predicates — tenant and user — and this
    is the test that fails if the second one is ever dropped, because the first one alone
    would happily empty a colleague's badge.
    """
    agent = notified["agent"]
    colleague = notified["colleague"]
    assert isinstance(agent, OrgSession)
    assert isinstance(colleague, OrgSession)

    cleared = colleague.post(f"{NOTIFICATIONS}/read-all")

    assert cleared.json() == {"marked_read": 0}
    assert unread(agent) == 1


def test_the_owner_can_still_do_all_of_it(notified: dict[str, object]) -> None:
    """The negative tests above are only meaningful if the positive one holds in the same setup.

    Four refusals in a row would be equally consistent with a feature that refuses
    everything, including its owner.
    """
    notification = notified["notification"]
    agent = notified["agent"]
    assert isinstance(notification, dict)
    assert isinstance(agent, OrgSession)
    assert unread(agent) == 1

    marked = agent.post(f"{NOTIFICATIONS}/{notification['id']}/read")

    assert marked.status_code == 200, marked.text
    assert marked.json()["read_at"] is not None
    assert unread(agent) == 0
    assert agent.post(f"{NOTIFICATIONS}/read-all").json() == {"marked_read": 0}


# ---------------------------------------------------------------------------
# What the responses do not carry
# ---------------------------------------------------------------------------


def test_no_response_names_the_tenant_or_the_recipient(notified: dict[str, object]) -> None:
    """A field-by-field check that the payload carries only what a client renders.

    Asserted here rather than only in the API suite because the reason is a §4 one: an
    `organization_id` in a response is a tenant identifier handed to a client that has no
    use for it, and a `user_id` on a notification would let a caller correlate rows across
    accounts from an endpoint that is supposed to be per-person.
    """
    notification = notified["notification"]
    agent = notified["agent"]
    assert isinstance(notification, dict)
    assert isinstance(agent, OrgSession)

    for payload in (notification, agent.get(NOTIFICATIONS).json()[0]):
        assert set(payload) == {
            "id",
            "notification_type",
            "title",
            "body",
            "ticket_id",
            "read_at",
            "created_at",
        }
        serialized = str(payload)
        assert agent.user_id not in serialized
