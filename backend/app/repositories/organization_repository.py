"""Organization persistence.

Not tenant-scoped, and cannot be: an organization *is* the tenant, so there is no
outer scope to filter by. This is the one repository where an unscoped lookup is
correct rather than a hazard, which is why it is a separate class with a small,
auditable surface.
"""

import uuid

from sqlalchemy import select

from app.models.enums import OrganizationStatus
from app.models.organization import Organization
from app.repositories.base import Repository


class OrganizationRepository(Repository[Organization]):
    model = Organization

    async def get(self, organization_id: uuid.UUID) -> Organization | None:
        return await self.session.get(Organization, organization_id)

    async def slug_exists(self, slug: str) -> bool:
        """Whether the slug is taken.

        `slug` is globally unique — it appears in URLs, so it has to be — which means
        this lookup is deliberately not filtered by anything.
        """
        return (
            await self.session.scalar(select(Organization.id).where(Organization.slug == slug))
            is not None
        )

    async def list_active_ids(self) -> list[uuid.UUID]:
        """Every active organization's id, oldest first.

        **The one query that legitimately spans tenants**, and it can only be written
        here: this class is not tenant-scoped because an organization *is* the tenant, so
        there is no outer scope for it to be missing. A `TenantScopedRepository` could not
        express this query at all, which is the property that makes unscoped reads
        impossible rather than merely discouraged everywhere else.

        Read by the SLA sweep's dispatcher, which has no tenant to start from and whose
        whole job is to enumerate them. `id`s alone rather than `Organization` rows: the
        only thing any caller does with the result is pass one to a per-tenant task, and a
        full row would be a model loaded to have one attribute read off it.

        Suspended tenants are excluded. `get_current_user` refuses to serve a suspended
        organization, so its users cannot sign in to read an alert — and a tenant that has
        stopped being served should not keep accumulating notifications nobody can see.
        """
        statement = (
            select(Organization.id)
            .where(Organization.status == OrganizationStatus.ACTIVE)
            .order_by(Organization.created_at, Organization.id)
        )
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    def add(self, organization: Organization) -> Organization:
        self.session.add(organization)
        return organization
