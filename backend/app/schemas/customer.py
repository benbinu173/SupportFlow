"""Customer request and response schemas.

A customer is a support contact: someone an organization corresponds with. It is not
an account — `app/models/user.py` records that a customer may exist without ever
holding portal credentials, and `UserCreate.customer_id` is what links the two when
they do.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.fields import Email


class CustomerRead(BaseModel):
    """A customer as the API presents one."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    phone: str | None
    external_reference: str | None
    extra_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    # Deliberately absent: organization_id. It is implied by the caller's own token,
    # and echoing it back would suggest it is theirs to set.


class CustomerCreate(BaseModel):
    """Adding a support contact.

    `email` is required and unique within the organization — it is how a customer is
    matched to inbound mail and to a portal account. `external_reference` is the
    organization's own id for this person in whatever system they already use, which
    is why it is a free string rather than something this API interprets.
    """

    name: str = Field(min_length=1, max_length=200)
    email: Email
    phone: str | None = Field(default=None, max_length=50)
    external_reference: str | None = Field(default=None, max_length=200)


class CustomerUpdate(BaseModel):
    """Changing a customer's details.

    Every field is optional and `None` means "leave it alone" — with `exclude_unset`
    at the service, a client can send `{"phone": null}` to clear a phone number and
    omit `phone` to keep it. Both are legitimate and the distinction is the whole
    reason the service reads `model_fields_set` rather than comparing to `None`.

    `email` is updatable but re-checked for uniqueness: changing it to an address
    another customer already holds is a conflict, not a silent merge.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: Email | None = None
    phone: str | None = Field(default=None, max_length=50)
    external_reference: str | None = Field(default=None, max_length=200)
