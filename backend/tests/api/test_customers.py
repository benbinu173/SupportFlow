"""Customer management: CRUD, search, pagination, and who may do what.

A customer is the first tenant-owned resource this API exposes that is *not* a login,
so it is the first place the organization boundary is the only thing standing between
one support desk's contact list and another's. Cross-tenant behaviour is asserted in
`tests/security/test_tenant_isolation.py`; this file is about the resource working at
all, and about the search behaving like a search rather than like a pattern match.

Every test drives the real API — an organization is registered, its users authenticate
for real, and customers are created through `POST /customers`.
"""

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, CUSTOMERS, OrgSession

pytestmark = pytest.mark.integration


@pytest.fixture
def org(register_org: Callable[..., OrgSession]) -> OrgSession:
    """One organization with an authenticated admin."""
    return register_org(organization_name="Customer Co")


@pytest.fixture
def roles(org: OrgSession) -> dict[str, OrgSession]:
    """One organization staffed with a user in every role, each authenticated."""
    return {
        "admin": org,
        "manager": org.add_user("manager", email="manager@customerco.com"),
        "agent": org.add_user("agent", email="agent@customerco.com"),
        "customer": org.add_user("customer", email="customer@customerco.com"),
    }


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "manager", "agent"])
def test_every_staff_role_may_create_a_customer(roles: dict[str, OrgSession], role: str) -> None:
    """`Create customer` is ✓ for admin, manager, and agent alike — there is no row of
    the matrix that reserves it, because the person who takes the call is the person
    who should be able to record who called."""
    response = roles[role].post(
        CUSTOMERS, json={"name": "Ada Lovelace", "email": f"ada-{role}@analytical.com"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Ada Lovelace"
    assert response.json()["email"] == f"ada-{role}@analytical.com"


def test_a_customer_may_not_create_a_customer(roles: dict[str, OrgSession]) -> None:
    """`Create customer` is `—` for the customer role.

    Asserted separately from the staff test above rather than parametrised with it,
    because the interesting fact is the direction: the portal role is the untrusted
    one, and writing to the contact list is an operation on the organization.
    """
    response = roles["customer"].post(
        CUSTOMERS, json={"name": "Mallory", "email": "mallory@analytical.com"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_a_created_customer_is_returned_with_the_documented_fields(org: OrgSession) -> None:
    response = org.post(
        CUSTOMERS,
        json={
            "name": "Grace Hopper",
            "email": "grace@navy.mil",
            "phone": "+1 555 0100",
            "external_reference": "CRM-42",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "id",
        "name",
        "email",
        "phone",
        "external_reference",
        "extra_data",
        "created_at",
        "updated_at",
    }
    assert body["phone"] == "+1 555 0100"
    assert body["external_reference"] == "CRM-42"
    # The tenant is implied by the caller's token and is never a field.
    assert "organization_id" not in response.text


def test_only_the_optional_fields_may_be_omitted(org: OrgSession) -> None:
    """`name` and `email` are the identity; everything else is a detail."""
    response = org.post(CUSTOMERS, json={"name": "Minimal", "email": "minimal@analytical.com"})

    assert response.status_code == 201, response.text
    assert response.json()["phone"] is None
    assert response.json()["external_reference"] is None


def test_an_email_is_normalized_on_the_way_in(org: OrgSession) -> None:
    """Lowercased and trimmed, so the uniqueness constraint cannot be evaded by
    capitalization and two accounts that look identical to a human cannot coexist."""
    response = org.post(CUSTOMERS, json={"name": "Cased", "email": "  Cased@Analytical.COM  "})

    assert response.status_code == 201, response.text
    assert response.json()["email"] == "cased@analytical.com"


def test_a_duplicate_email_within_the_organization_is_a_conflict(org: OrgSession) -> None:
    """A 409 with a code a client can act on, rather than a 500 from the unique index."""
    org.add_customer(email="taken@analytical.com")

    response = org.post(CUSTOMERS, json={"name": "Second", "email": "taken@analytical.com"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CUSTOMER_ALREADY_EXISTS"


def test_the_same_email_may_belong_to_customers_of_two_organizations(
    register_org: Callable[..., OrgSession],
) -> None:
    """Uniqueness is per tenant. A person may be a customer of two support providers,
    and neither organization learns from the other that the address exists."""
    first = register_org(organization_name="First Co")
    second = register_org(organization_name="Second Co")
    shared = "shared@analytical.com"

    assert first.post(CUSTOMERS, json={"name": "Shared", "email": shared}).status_code == 201
    assert second.post(CUSTOMERS, json={"name": "Shared", "email": shared}).status_code == 201


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "no-name@analytical.com"},
        {"name": "No Email"},
        {"name": "", "email": "blank@analytical.com"},
        {"name": "Not An Email", "email": "not-an-email"},
        {"name": "Bad Phone", "email": "phone@analytical.com", "phone": "x" * 51},
    ],
)
def test_a_malformed_customer_is_rejected(org: OrgSession, payload: dict[str, str]) -> None:
    response = org.post(CUSTOMERS, json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_client_may_not_choose_the_organization(org: OrgSession) -> None:
    """An `organization_id` in the body is not a field, so Pydantic ignores it rather
    than honouring it — and the test asserts the *outcome*: the row is not in the other
    organization, which is what a forged field would have achieved."""
    other = uuid.uuid4()

    response = org.post(
        CUSTOMERS,
        json={
            "name": "Forged",
            "email": "forged@analytical.com",
            "organization_id": str(other),
        },
    )

    assert response.status_code == 201, response.text
    # The customer appears in the creator's own listing, which is where the tenant
    # actually came from.
    listed = {customer["email"] for customer in org.get(CUSTOMERS).json()}
    assert "forged@analytical.com" in listed


# ---------------------------------------------------------------------------
# Listing and searching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["manager", "agent"])
def test_a_manager_and_an_agent_may_list_customers(roles: dict[str, OrgSession], role: str) -> None:
    """`List customers` is ✓ for every staff role. There is no `assigned` qualifier:
    a customer record belongs to the organization, not to one agent."""
    roles["admin"].add_customer(email=f"listed-by-{role}@analytical.com")

    response = roles[role].get(CUSTOMERS)

    assert response.status_code == 200, response.text
    assert any(c["email"] == f"listed-by-{role}@analytical.com" for c in response.json())


def test_a_customer_may_not_list_the_directory(roles: dict[str, OrgSession]) -> None:
    """The organization's other customers are none of a customer's business."""
    response = roles["customer"].get(CUSTOMERS)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_the_listing_is_paginated(org: OrgSession) -> None:
    for index in range(5):
        org.add_customer(name=f"Numbered {index}", email=f"numbered{index}@analytical.com")

    everything = org.get(CUSTOMERS).json()
    first_two = org.get(CUSTOMERS, params={"limit": 2}).json()
    next_two = org.get(CUSTOMERS, params={"limit": 2, "offset": 2}).json()

    assert len(everything) == 5
    assert len(first_two) == 2
    assert [*first_two, *next_two] == everything[:4]


def test_the_listing_is_newest_first(org: OrgSession) -> None:
    org.add_customer(name="Older", email="older@analytical.com")
    org.add_customer(name="Newer", email="newer@analytical.com")

    names = [customer["name"] for customer in org.get(CUSTOMERS).json()]

    assert names.index("Newer") < names.index("Older")


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"limit": -1}, {"offset": -1}])
def test_an_out_of_range_page_size_is_rejected(org: OrgSession, params: dict[str, int]) -> None:
    response = org.get(CUSTOMERS, params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_search_matches_a_substring_of_the_name(org: OrgSession) -> None:
    """A substring, not a prefix: someone remembering a customer is as likely to recall
    the surname as the first letter."""
    org.add_customer(name="Ada Lovelace", email="ada@analytical.com")
    org.add_customer(name="Grace Hopper", email="grace@navy.mil")

    found = org.get(CUSTOMERS, params={"q": "ovelac"}).json()

    assert [customer["name"] for customer in found] == ["Ada Lovelace"]


def test_search_matches_the_email_domain(org: OrgSession) -> None:
    org.add_customer(name="Ada", email="ada@analytical.com")
    org.add_customer(name="Grace", email="grace@navy.mil")

    found = org.get(CUSTOMERS, params={"q": "navy.mil"}).json()

    assert [customer["email"] for customer in found] == ["grace@navy.mil"]


def test_search_ignores_case(org: OrgSession) -> None:
    org.add_customer(name="Ada Lovelace", email="ada@analytical.com")

    found = org.get(CUSTOMERS, params={"q": "LOVELACE"}).json()

    assert len(found) == 1


def test_an_empty_search_term_is_not_a_search(org: OrgSession) -> None:
    """`?q=` is a query string a browser or a form sends when a box was cleared. It
    means "no filter", not "search for the empty string"."""
    org.add_customer(name="Ada", email="ada@analytical.com")

    assert len(org.get(CUSTOMERS, params={"q": ""}).json()) == 1
    assert len(org.get(CUSTOMERS, params={"q": "   "}).json()) == 1


def test_a_search_that_matches_nothing_returns_an_empty_page(org: OrgSession) -> None:
    org.add_customer(name="Ada", email="ada@analytical.com")

    response = org.get(CUSTOMERS, params={"q": "nobody-called-this"})

    assert response.status_code == 200
    assert response.json() == []


def test_a_percent_sign_is_searched_for_literally(org: OrgSession) -> None:
    """The test this file most needed.

    SQLAlchemy parameterizes the term, so there was never an injection — but `%` is a
    LIKE metacharacter, so an unescaped search for `50%` becomes a pattern that matches
    every row in the table. The bug looks like a working search: it returns results,
    just the wrong ones. Escaping is asserted by the *count*, since "finds nothing" and
    "finds everything" are both consistent with a search that did not match the term.
    """
    org.add_customer(name="Fifty Percent 50% Off", email="fifty@analytical.com")
    org.add_customer(name="Ada Lovelace", email="ada@analytical.com")

    found = org.get(CUSTOMERS, params={"q": "50%"}).json()

    assert [customer["name"] for customer in found] == ["Fifty Percent 50% Off"]


def test_a_bare_percent_sign_matches_only_names_containing_one(org: OrgSession) -> None:
    """The complement of the test above, and the one that catches the worst version of
    the bug: `q=%` unescaped matches the entire table."""
    org.add_customer(name="Discount 10%", email="discount@analytical.com")
    org.add_customer(name="Ada Lovelace", email="ada@analytical.com")

    found = org.get(CUSTOMERS, params={"q": "%"}).json()

    assert [customer["name"] for customer in found] == ["Discount 10%"]


def test_an_underscore_is_searched_for_literally(org: OrgSession) -> None:
    """`_` matches any single character, so an unescaped `a_b` finds `aXb` as well.
    Here it would find `aXb` and must not."""
    org.add_customer(name="snake_case", email="snake@analytical.com")
    org.add_customer(name="snakeXcase", email="camel@analytical.com")

    found = org.get(CUSTOMERS, params={"q": "snake_case"}).json()

    assert [customer["name"] for customer in found] == ["snake_case"]


def test_a_backslash_is_searched_for_literally(org: OrgSession) -> None:
    """The escape character itself. Escaping `%` and `_` without escaping `\\` first
    would turn a search for a literal backslash into a search for an escaped nothing —
    and would double the escapes added before it."""
    org.add_customer(name="path\\to\\thing", email="path@analytical.com")
    org.add_customer(name="Ada Lovelace", email="ada@analytical.com")

    found = org.get(CUSTOMERS, params={"q": "path\\to"}).json()

    assert [customer["name"] for customer in found] == ["path\\to\\thing"]


def test_a_search_term_longer_than_the_limit_is_rejected(org: OrgSession) -> None:
    """An unbounded term is an unbounded amount of work for the trigram index."""
    response = org.get(CUSTOMERS, params={"q": "a" * 201})

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Fetching one
# ---------------------------------------------------------------------------


def test_fetching_a_customer_returns_that_customer(org: OrgSession) -> None:
    created = org.add_customer(name="Ada", email="ada@analytical.com")

    response = org.get(f"{CUSTOMERS}/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_an_agent_may_fetch_one_customer(roles: dict[str, OrgSession]) -> None:
    """Reading one customer needs `CUSTOMER_LIST`, the same capability as listing —
    the matrix has no separate "view customer" row."""
    created = roles["admin"].add_customer(email="viewed@analytical.com")

    assert roles["agent"].get(f"{CUSTOMERS}/{created['id']}").status_code == 200


def test_an_unknown_customer_id_is_a_404(org: OrgSession) -> None:
    response = org.get(f"{CUSTOMERS}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CUSTOMER_NOT_FOUND"


def test_a_malformed_customer_id_is_rejected_before_the_lookup(org: OrgSession) -> None:
    response = org.get(f"{CUSTOMERS}/not-a-uuid")

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Updating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "manager", "agent"])
def test_every_staff_role_may_update_a_customer(roles: dict[str, OrgSession], role: str) -> None:
    created = roles["admin"].add_customer(email="updated@analytical.com")

    response = roles[role].patch(f"{CUSTOMERS}/{created['id']}", json={"name": "Renamed"})

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"


def test_a_customer_may_not_update_a_customer(roles: dict[str, OrgSession]) -> None:
    created = roles["admin"].add_customer(email="protected@analytical.com")

    response = roles["customer"].patch(f"{CUSTOMERS}/{created['id']}", json={"name": "Hacked"})

    assert response.status_code == 403


def test_an_omitted_field_is_left_alone(org: OrgSession) -> None:
    created = org.add_customer(email="untouched@analytical.com", phone="+1 555 0199")

    response = org.patch(f"{CUSTOMERS}/{created['id']}", json={"name": "Renamed"})

    assert response.json()["phone"] == "+1 555 0199"


def test_an_explicit_null_clears_a_field(org: OrgSession) -> None:
    """The reason the service reads `model_fields_set` rather than comparing to `None`.

    `{}` and `{"phone": null}` are different requests — "change nothing" and "clear the
    phone number" — and an implementation that treated both as an absent value would
    make clearing a field impossible. The two tests are adjacent so the distinction is
    visible rather than implied.
    """
    created = org.add_customer(email="cleared@analytical.com", phone="+1 555 0199")

    response = org.patch(f"{CUSTOMERS}/{created['id']}", json={"phone": None})

    assert response.status_code == 200, response.text
    assert response.json()["phone"] is None


def test_an_empty_update_body_changes_nothing(org: OrgSession) -> None:
    created = org.add_customer(name="Original", email="same@analytical.com", phone="+1 555 0100")

    response = org.patch(f"{CUSTOMERS}/{created['id']}", json={})

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Original"
    assert response.json()["phone"] == "+1 555 0100"


def test_an_updated_email_still_has_to_be_unique(org: OrgSession) -> None:
    org.add_customer(name="First", email="first@analytical.com")
    second = org.add_customer(name="Second", email="second@analytical.com")

    response = org.patch(f"{CUSTOMERS}/{second['id']}", json={"email": "first@analytical.com"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CUSTOMER_ALREADY_EXISTS"


def test_a_customer_may_keep_their_own_email(org: OrgSession) -> None:
    """Without the `excluding` argument the uniqueness check would find the customer
    themselves and refuse a request that changes nothing — so this is what proves the
    update path is usable at all, not merely that it is strict."""
    created = org.add_customer(name="Ada", email="ada@analytical.com")

    response = org.patch(
        f"{CUSTOMERS}/{created['id']}", json={"email": "ada@analytical.com", "name": "Ada L."}
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Ada L."


def test_an_updated_email_is_normalized_too(org: OrgSession) -> None:
    created = org.add_customer(email="original@analytical.com")

    response = org.patch(f"{CUSTOMERS}/{created['id']}", json={"email": "MIXED@Analytical.Com"})

    assert response.status_code == 200, response.text
    assert response.json()["email"] == "mixed@analytical.com"


def test_updating_an_unknown_customer_is_a_404(org: OrgSession) -> None:
    response = org.patch(f"{CUSTOMERS}/{uuid.uuid4()}", json={"name": "Nobody"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CUSTOMER_NOT_FOUND"


def test_an_invalid_update_is_rejected(org: OrgSession) -> None:
    created = org.add_customer(email="validated@analytical.com")

    response = org.patch(f"{CUSTOMERS}/{created['id']}", json={"email": "not-an-email"})

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Unauthenticated access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", CUSTOMERS),
        ("post", CUSTOMERS),
        ("get", f"{CUSTOMERS}/00000000-0000-0000-0000-000000000000"),
        ("patch", f"{CUSTOMERS}/00000000-0000-0000-0000-000000000000"),
    ],
)
def test_every_customers_route_requires_a_token(client: TestClient, method: str, path: str) -> None:
    """No route here is reachable without a credential, whatever its capability."""
    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_a_portal_login_cannot_reach_the_directory_either_way(
    roles: dict[str, OrgSession],
) -> None:
    """The customer role is refused by capability at every route here, not by a scope.

    Worth stating once explicitly: unlike tickets, there is no narrowing of this
    resource for a portal caller — the endpoint is simply closed to them, and
    `app/repositories/customer_repository.py` records why. The distinction shows up in
    the status code: a ticket out of reach is a **404** (row scope), a customer resource
    is a **403** (capability), and neither is the other.
    """
    customer = roles["customer"]
    target = f"{CUSTOMERS}/{uuid.uuid4()}"

    assert customer.get(CUSTOMERS).status_code == 403
    assert (
        customer.post(CUSTOMERS, json={"name": "Mallory", "email": "m@analytical.com"}).status_code
        == 403
    )
    assert customer.get(target).status_code == 403
    assert customer.patch(target, json={"name": "Renamed"}).status_code == 403


def test_a_customer_login_is_still_a_login(roles: dict[str, OrgSession]) -> None:
    """A sanity check on the session rather than on the API.

    Without this, every 403 above is also consistent with a `customer` session that
    never authenticated at all — `OrgSession.get` attaches a bearer header, but a token
    the API did not accept would be an authentication failure wearing the same status
    code's clothing. Reading `/auth/me` proves the credential is live and the refusals
    above are authorization decisions.
    """
    response = roles["customer"].get(f"{AUTH}/me")

    assert response.status_code == 200, response.text
    assert response.json()["role"] == "customer"
    assert response.json()["customer_id"] is not None
