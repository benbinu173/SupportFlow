"""User request and response schemas.

`UserRead` is the only shape a user is ever returned in, so `password_hash` has no
route to a response by accident — it is not a field, rather than a field with an
exclusion that someone could forget (architecture §4: passwords never in a response).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import UserRole
from app.schemas.fields import Email, Password


class UserRead(BaseModel):
    """A user as the API presents one."""

    # Reads straight off the ORM object, so services can return entities unchanged.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    role: UserRole
    is_active: bool
    # Null for staff. Non-null for a portal account, naming the customer it acts as —
    # useful to an admin wondering why a customer login sees nothing.
    customer_id: uuid.UUID | None
    created_at: datetime
    last_login_at: datetime | None

    # Deliberately absent: password_hash, organization_id. The organization is implied
    # by the caller's own token, and echoing it back would suggest it is theirs to set.


class UserCreate(BaseModel):
    """An administrator creating a staff account within their own organization.

    `customer_id` is the one field that is conditional on `role`: it links a portal
    account to the customer record it acts as, which is what makes `RowScope.OWN`
    resolvable. It is optional in the schema and required in practice for a portal
    role — the check lives in `user_service.create_user`, because "a customer login
    that cannot see any tickets" is a domain problem rather than a shape problem, and
    the same validation has to apply to any future path that creates a user.
    """

    name: str = Field(min_length=1, max_length=200)
    email: Email
    password: Password
    # Required, not defaulted. A default of `agent` would be a silent privilege
    # decision; the caller should have to say which role they are creating.
    role: UserRole
    customer_id: uuid.UUID | None = None


class UserRoleUpdate(BaseModel):
    """Changing a user's role."""

    role: UserRole
