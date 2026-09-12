"""Organization persistence.

Not tenant-scoped, and cannot be: an organization *is* the tenant, so there is no
outer scope to filter by. This is the one repository where an unscoped lookup is
correct rather than a hazard, which is why it is a separate class with a small,
auditable surface.
"""

import uuid

from sqlalchemy import select

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

    def add(self, organization: Organization) -> Organization:
        self.session.add(organization)
        return organization
