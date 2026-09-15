"""Customer endpoints.

Every route is guarded by a capability from `app/core/permissions.py` and scoped to the
caller's organization by the repository it builds. There is no route here that takes an
organization from the request, because there is no field for one.

`GET /{customer_id}` requires `CUSTOMER_LIST` rather than a view capability of its own:
the matrix has no "view customer" row, reading one customer and listing them are the
same permission, and `USER_LIST` guards `/users/{user_id}` for the same reason.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import Context, DbSession, require_permission
from app.core.permissions import Permission
from app.schemas.customer import CustomerCreate, CustomerRead, CustomerSortKey, CustomerUpdate
from app.schemas.fields import SortOrder
from app.services import customer_service

router = APIRouter()


@router.get(
    "",
    response_model=list[CustomerRead],
    summary="List and search customers",
    dependencies=[Depends(require_permission(Permission.CUSTOMER_LIST))],
)
async def list_customers(
    context: Context,
    db: DbSession,
    q: Annotated[str | None, Query(max_length=200, description="Match name or email")] = None,
    created_after: Annotated[datetime | None, Query(description="Inclusive lower bound.")] = None,
    created_before: Annotated[datetime | None, Query(description="Exclusive upper bound.")] = None,
    sort: Annotated[CustomerSortKey, Query()] = CustomerSortKey.CREATED_AT,
    order: Annotated[SortOrder, Query()] = SortOrder.DESC,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CustomerRead]:
    """A page of customers in the caller's organization.

    Returns a plain array rather than a `{items, total}` envelope, consistent with
    `/users`: every field in an envelope has to be maintained and tested, and nothing
    consumes a total today — when a UI needs one, adding it is a compatible change.

    `q` is a substring match over name and email, served by the table's two trigram
    indexes. Wildcards in the term are escaped, so a search for `50%` finds a customer
    whose name contains "50%" rather than every customer in the organization.

    The date bounds are half-open, matching `/tickets` and `/audit-logs`: `created_after`
    inclusive, `created_before` exclusive. `sort` and `order` were added in Phase N and
    are defaulted to `created_at desc`, which is what this route returned before it had
    the parameters — so no existing client saw a change. Ties are broken by customer id,
    so `sort=name` over a repeated name still pages cleanly.
    """
    customers = await customer_service.list_customers(
        db,
        context,
        term=q,
        created_after=created_after,
        created_before=created_before,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    return [CustomerRead.model_validate(customer) for customer in customers]


@router.post(
    "",
    response_model=CustomerRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer",
    dependencies=[Depends(require_permission(Permission.CUSTOMER_CREATE))],
)
async def create_customer(payload: CustomerCreate, context: Context, db: DbSession) -> CustomerRead:
    """Add a support contact to the caller's organization.

    Email is unique per organization — not globally — so the same person may be a
    customer of two support providers, and one organization cannot learn from a
    conflict that an address exists in another.
    """
    customer = await customer_service.create_customer(db, context, payload)
    return CustomerRead.model_validate(customer)


@router.get(
    "/{customer_id}",
    response_model=CustomerRead,
    summary="Fetch one customer",
    dependencies=[Depends(require_permission(Permission.CUSTOMER_LIST))],
)
async def get_customer(customer_id: uuid.UUID, context: Context, db: DbSession) -> CustomerRead:
    """One customer in the caller's organization.

    A `customer_id` belonging to another tenant returns **404**, not 403 —
    indistinguishable from an id that does not exist (ADR-009).
    """
    customer = await customer_service.get_customer(db, context, customer_id)
    return CustomerRead.model_validate(customer)


@router.patch(
    "/{customer_id}",
    response_model=CustomerRead,
    summary="Update a customer",
    dependencies=[Depends(require_permission(Permission.CUSTOMER_UPDATE))],
)
async def update_customer(
    customer_id: uuid.UUID, payload: CustomerUpdate, context: Context, db: DbSession
) -> CustomerRead:
    """Update a customer's details.

    Omitted fields are left alone and an explicit `null` clears one, so
    `{"phone": null}` and `{}` mean different things. Changing `email` to an address
    another customer in the organization already holds is a `409`, not a merge.
    """
    customer = await customer_service.update_customer(db, context, customer_id, payload)
    return CustomerRead.model_validate(customer)
