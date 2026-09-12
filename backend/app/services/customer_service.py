"""Customer management within a tenant.

Every function takes a `TenantContext` and builds a repository from it, so the
organization filter is never a parameter a caller could get wrong. A customer id
naming another tenant's row produces `CUSTOMER_NOT_FOUND` and a 404, identical to a
genuinely absent id (ADR-009).

The capability check is not here — that happens at the route, via `require_permission`.
This layer enforces the rules a capability alone cannot express, of which there is one:
email is unique within the organization.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CustomerAlreadyExistsError, ErrorCode, NotFoundError
from app.core.tenancy import TenantContext
from app.models.customer import Customer
from app.repositories.customer_repository import CustomerRepository
from app.schemas.customer import CustomerCreate, CustomerUpdate

logger = structlog.get_logger(__name__)


async def list_customers(
    session: AsyncSession,
    context: TenantContext,
    *,
    term: str | None,
    limit: int,
    offset: int,
) -> list[Customer]:
    """A page of customers in the caller's organization, optionally searched."""
    return list(
        await CustomerRepository(session, context).search(term=term, limit=limit, offset=offset)
    )


async def get_customer(
    session: AsyncSession, context: TenantContext, customer_id: uuid.UUID
) -> Customer:
    """One customer, or 404 — including when the id belongs to another organization."""
    customer = await CustomerRepository(session, context).get(customer_id)
    if customer is None:
        raise NotFoundError(ErrorCode.CUSTOMER_NOT_FOUND)
    return customer


async def create_customer(
    session: AsyncSession, context: TenantContext, payload: CustomerCreate
) -> Customer:
    """Add a support contact to the caller's organization.

    The new customer's `organization_id` comes from `context` and from nowhere else.

    The duplicate check is a courtesy that produces a clear 409; the `UniqueConstraint`
    on `(organization_id, email)` is what actually guarantees it. Both are wanted — the
    constraint because two concurrent requests could pass the check together, and the
    check because a raw integrity error is a 500 rather than something a client can act
    on.
    """
    repository = CustomerRepository(session, context)

    if await repository.email_taken(payload.email):
        raise CustomerAlreadyExistsError()

    customer = Customer(
        organization_id=context.organization_id,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        external_reference=payload.external_reference,
    )
    repository.add(customer)
    await session.commit()

    logger.info(
        "customer_created",
        customer_id=str(customer.id),
        organization_id=str(context.organization_id),
        actor_id=str(context.user_id),
    )
    return customer


async def update_customer(
    session: AsyncSession,
    context: TenantContext,
    customer_id: uuid.UUID,
    payload: CustomerUpdate,
) -> Customer:
    """Update a customer's details.

    **Fields absent from the request are not touched**, read from `model_fields_set`
    rather than by comparing to `None`. The two are not the same question: `{"phone":
    null}` is a client clearing a phone number, and `{}` is a client changing nothing.
    Treating both as "no value" would make clearing a field impossible; treating both
    as a write would make an omitted field null. Only one of the two readings can be
    right, and it has to be chosen explicitly.

    `email` is re-checked for uniqueness against every *other* customer in the tenant.
    """
    repository = CustomerRepository(session, context)

    customer = await repository.get(customer_id)
    if customer is None:
        raise NotFoundError(ErrorCode.CUSTOMER_NOT_FOUND)

    fields = payload.model_fields_set

    if "email" in fields and payload.email is not None:
        if await repository.email_taken(payload.email, excluding=customer.id):
            raise CustomerAlreadyExistsError()
        customer.email = payload.email

    if "name" in fields and payload.name is not None:
        customer.name = payload.name
    if "phone" in fields:
        customer.phone = payload.phone
    if "external_reference" in fields:
        customer.external_reference = payload.external_reference

    await session.commit()

    logger.info(
        "customer_updated",
        customer_id=str(customer.id),
        organization_id=str(context.organization_id),
        actor_id=str(context.user_id),
        fields=sorted(fields),
    )
    return customer
