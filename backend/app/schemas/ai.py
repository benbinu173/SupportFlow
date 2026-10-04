"""AI output vocabulary — the four shapes the provider is allowed to return.

Spec §18: *"The backend must validate the returned structure. Never assume LLM output
is automatically valid."* These models are how that requirement is kept: a provider
response reaches application code only as an instance of one of them, and
`app/ai/provider.py` holds the single function that turns a raw payload into one or
raises. There is no path from a model's answer to the database that skips this module.

Three rules are shared by all four, each one closing a way a model can be confidently
wrong rather than obviously broken:

* **`extra="forbid"`.** A model that invents a field gets a validation failure instead
  of a field that quietly disappears into the ledger. It also puts
  `"additionalProperties": false` into the JSON schema the provider is handed, so the
  constraint is stated to the model and enforced on its answer — §54's "AI output
  validated" being the same claim in two places.
* **Strings are length-bounded at the column they are destined for.** §18's pipeline
  ends in `UPDATE tickets SET category = ...`, and `tickets.category` is
  `String(100)`. A schema that accepted a 400-character category would move the failure
  from a validation error, which is a handled outcome, to a database error in a
  transaction that had already spent the tokens.
* **Confidence is bounded `[0, 1]`**, matching the check constraints Phase D put on
  `tickets.sentiment_confidence` and `tickets.ai_classification_confidence`. A model
  reporting `1.4` or `94` is not expressing 94% certainty in a unit we asked for, and
  the `CHECK` would reject it anyway — better here, where the message can say what was
  wrong.

**Nothing here computes anything**, for the reason `app/schemas/sla.py` gives about
itself. `Sentiment` is reused from `app/models/enums.py` rather than restated, because
the value has to land in a column typed by the enum object Phase D declared, and two
declarations of "negative" is how the two stop matching.

**`Sentiment` is the vocabulary the database already has; category is not.** Category
and subcategory are free text by design — the spec's example is `"Billing"` /
`"Duplicate Charge"`, which is a tenant's taxonomy and not a fixed list — so they are
bounded strings rather than enums. `docs/data-model.md` records the same decision for
the column.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Sentiment, TicketPriority

# Shared by all four below. See the module docstring for why `forbid` is not optional.
_STRICT = ConfigDict(extra="forbid")


class Classification(BaseModel):
    """§18's classification of a ticket.

    `subcategory` is optional because the subdivision is genuinely not always available:
    a ticket can be squarely "Billing" while being none of the tenant's billing
    subcategories. A model forced to choose would invent one, and `"General"` invented
    to satisfy a required field is worse than `null` — the first is indistinguishable
    from a real answer and the second is not.

    **`priority` rides on the classification call rather than getting one of its own.**
    §51 asks for a priority recommendation and §17's provider interface has exactly four
    operations; a fifth would mean a new `AIOperation` member, which is `ALTER TYPE` on a
    PostgreSQL enum — a migration for a value the model can answer in the call it is
    already making, from the same reading of the same ticket, at no extra cost.

    **It is the model's judgement and that is what makes the column it feeds honest.**
    `tickets.ai_recommended_priority`'s comment says it holds *"what the model suggested"*,
    and the alternative — a Python rule banding a category and a sentiment into a priority
    — would produce a number that is a rule's output wearing a recommendation's name.
    §6 keeps this separate from `tickets.priority` precisely so the business decision and
    the suggestion can be compared; a fabricated suggestion makes that comparison
    meaningless. `ticket_service.change_priority` is the only writer of `tickets.priority`
    and this field never reaches it.

    `TicketPriority` is reused from `app/models/enums.py` for the reason the module
    docstring gives about `Sentiment`: the value is destined for a column typed by that
    enum object, and restating its members is how the two stop matching.

    The lengths match `tickets.category` and `tickets.subcategory`, both `String(100)`.
    """

    model_config = _STRICT

    category: str = Field(min_length=1, max_length=100)
    subcategory: str | None = Field(default=None, max_length=100)
    priority: TicketPriority
    confidence: float = Field(ge=0, le=1)


class SentimentResult(BaseModel):
    """§19's sentiment with its confidence.

    §19 is explicit that this is *"an operational signal, not an unquestionable
    fact"*, and the confidence is what makes that usable — it is stored in
    `tickets.sentiment_confidence` beside the label so a dashboard can set aside the
    low-confidence rows rather than treating them as findings.
    """

    model_config = _STRICT

    sentiment: Sentiment
    confidence: float = Field(ge=0, le=1)


class ConversationSummary(BaseModel):
    """§20's summary of a long conversation.

    One field, because §20 asks for one thing and a `key_points` list invented here
    would be a shape the spec's `summary` column has nowhere to put.

    The ceiling is a runaway guard and not a style rule: "concise" is an instruction in
    the prompt, and a model that ignored it should fail validation rather than write an
    essay into a JSONB column that a UI panel then tries to render. 2 000 characters is
    several times §20's three-sentence example, so a real summary never approaches it.
    """

    model_config = _STRICT

    summary: str = Field(min_length=1, max_length=2_000)


class SuggestedReply(BaseModel):
    """§21's draft reply for an agent to edit and send.

    **No confidence field, deliberately.** §41 says to show confidence where it is
    meaningful, and on a draft it is the least meaningful number in the system: a reply
    is not right or wrong the way a classification is, it is edited. A figure beside it
    would be a number an agent learns to ignore, which is worse than no number. §21 also
    cannot afford the failure mode confidence invites — *"AI must NEVER automatically
    send a customer-facing response"*, and a confidence score next to a send button is
    the first step toward a threshold that sends.

    The ceiling is `messages.body`'s, read from `app/schemas/message.py` rather than
    chosen here: a draft is destined to become a message, so a draft that could not be
    sent would be a schema that validates something the database refuses.

    §21's "AI generated draft" distinction is `SenderType.AI_DRAFT`, which Phase D
    already declared — the schema says what the model wrote, and the sender type says
    who is claiming it.
    """

    model_config = _STRICT

    body: str = Field(min_length=1, max_length=20_000)
