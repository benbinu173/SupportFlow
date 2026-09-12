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
    created_at: datetime
    last_login_at: datetime | None

    # Deliberately absent: password_hash, organization_id. The organization is implied
    # by the caller's own token, and echoing it back would suggest it is theirs to set.


class UserCreate(BaseModel):
    """An administrator creating a staff account within their own organization."""

    name: str = Field(min_length=1, max_length=200)
    email: Email
    password: Password
    # Required, not defaulted. A default of `agent` would be a silent privilege
    # decision; the caller should have to say which role they are creating.
    role: UserRole


class UserRoleUpdate(BaseModel):
    """Changing a user's role."""

    role: UserRole
