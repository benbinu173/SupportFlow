"""`WorkerContext` — a tenant with no authority, and the shape that keeps it that way.

Phase Q refused to invent a `TenantContext` for the scheduler: *"`role` would have no honest
value at all: it decides `permissions`, and there is no role whose permissions describe 'the
scheduler'."* Phase U needed a tenant for a Celery task anyway, because `AIUsage.organization_id`
has to say who paid for a call, so `WorkerContext` is the answer to the question Phase Q left
open.

The tests here are about **absence**, which is an unusual thing for a suite to assert and the
whole reason the type exists. A `TenantContext` with `role=UserRole.ADMIN` handed to a task
would work — every line of `ai_service` would run, the ledger row would be correct, and the
only symptom would be that a background job is carrying a full set of staff capabilities
through code that has no business holding any. Nothing in the application can notice that, so
this file does.

Asserted structurally rather than behaviourally: `not hasattr` is the claim, because "cannot
answer an authorization question" is exactly "has nothing to answer it with".
"""

import uuid
from dataclasses import FrozenInstanceError

import pytest

from app.core.permissions import Permission
from app.core.tenancy import TenantContext, WorkerContext
from app.models.enums import UserRole

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# What it carries
# ---------------------------------------------------------------------------


def test_a_worker_context_carries_a_tenant() -> None:
    """The one field, and the reason the type exists at all.

    `AIUsage.organization_id` is what `/analytics/overview` groups by, so a task that could
    not name its tenant would produce spend no tenant could see — which is a worse failure
    than a task that cannot run.
    """
    organization_id = uuid.uuid4()

    assert WorkerContext(organization_id=organization_id).organization_id == organization_id


def test_the_repr_names_the_tenant_and_nothing_else() -> None:
    """It reaches log lines, and a worker's log line has no user id to show.

    The repr is asserted rather than left to `dataclass`'s generated one because the
    generated one prints the field name, and a reader scanning a worker's output for the
    tenant should not have to know that.
    """
    organization_id = uuid.uuid4()

    assert repr(WorkerContext(organization_id=organization_id)) == (
        f"<WorkerContext org={organization_id}>"
    )


# ---------------------------------------------------------------------------
# What it deliberately does not carry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attribute", ["role", "permissions", "has", "scope_for"])
def test_a_worker_context_cannot_answer_an_authorization_question(attribute: str) -> None:
    """The absence is the guarantee, and it is structural rather than a convention.

    `TenantContext` resolves `permissions` from `role` in `__post_init__`, so a fabricated
    role is a fabricated capability set — and there is no role whose capabilities describe
    "the analysis worker". A `WorkerContext` that *had* `has()` would be one somebody could
    call, and the code that called it would look reasonable.

    `hasattr` rather than a `TypeError` assertion: the claim is about the type's surface,
    and a `TypeError` would only prove the specific call site was written the way this test
    happened to write it.
    """
    context = WorkerContext(organization_id=uuid.uuid4())

    assert not hasattr(context, attribute)


def test_the_two_contexts_are_not_interchangeable() -> None:
    """`isinstance` is what a reviewer checks, and it must answer no in both directions.

    The direction that matters is the second: a caller holding a `WorkerContext` cannot get
    a `TenantContext` out of it, so a task that wants to reach a `TenantScopedRepository` —
    the only object in this codebase that applies a row scope — has nothing to construct one
    with. That is what keeps `sla_repository`'s *"the context-free queries stay small enough
    to count, in one file"* property true as phases add workers.
    """
    worker = WorkerContext(organization_id=uuid.uuid4())
    tenant = TenantContext(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role=UserRole.ADMIN)

    assert not isinstance(worker, TenantContext)
    assert not isinstance(tenant, WorkerContext)


def test_a_tenant_context_still_has_what_a_worker_context_lacks() -> None:
    """The contrast, asserted so that the absences above are a property of one type.

    A `hasattr` assertion passes just as well against a class that forgot to define
    anything — which is the failure mode of testing by absence, and the reason the
    positive case is here beside it.
    """
    context = TenantContext(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role=UserRole.ADMIN)

    assert context.has(Permission.AI_REQUEST_ANALYSIS)
    assert context.role is UserRole.ADMIN


def test_a_worker_context_is_frozen() -> None:
    """`TenantContext`'s docstring gives the reason: something mutates a mutable identity.

    A task that could rewrite its own `organization_id` mid-run would be able to attribute
    its spend to another tenant, and the ledger's tenant column is the one thing this type
    exists to get right.
    """
    context = WorkerContext(organization_id=uuid.uuid4())

    with pytest.raises(FrozenInstanceError):
        context.organization_id = uuid.uuid4()  # type: ignore[misc]
