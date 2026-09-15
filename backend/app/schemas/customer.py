"""Customer request and response schemas.

A customer is a support contact: someone an organization corresponds with. It is not
an account — `app/models/user.py` records that a customer may exist without ever
holding portal credentials, and `UserCreate.customer_id` is what links the two when
they do.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.fields import Email


class CustomerSortKey(StrEnum):
    """The columns a customer list may be ordered by.

    Only two, and that is the decision rather than a first instalment. `name` answers
    "find me the account" and `created_at` answers "what came in recently"; `email` is
    searchable through `q` and sorting by it would order the list by domain, which is a
    grouping nobody asked for. A member with no use is a member whose index has to be
    maintained for nothing.

    The direction is `order`, not the key. `created_at` with `desc` stays the default so
    that adding this parameter changed no existing response.
    """

    NAME = "name"
    CREATED_AT = "created_at"


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
