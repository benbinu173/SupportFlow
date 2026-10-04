"""The analysis read model — what the API says about a row the provider wrote.

Separate from `app/schemas/ai.py`, and the separation is the point. Those four models are
the *provider's* vocabulary: the shapes a model is allowed to answer in, validated before
anything reads them. This is the *API's*: one `ai_analyses` row, reported to a client. They
happen to agree about what a classification contains today, and they answer to different
constraints — the provider's models are bounded by what a model can be asked for, and this
one is bounded by what the caller is allowed to see.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.enums import AIOperation, ProcessingStatus


class AIAnalysisRead(BaseModel):
    """One `ai_analyses` row as the API reports it.

    **The three questions a client has, in the order it asks them.** `operation` and
    `status` say what is happening — a queued analysis and a finished one are the same row
    at different moments, and the read route exists precisely so the first is visible rather
    than looking like nothing happened. `result` and `confidence` say what the model
    concluded. `provider` and `model` say who concluded it, which is §41's *"make it clear
    this is AI-generated"* satisfied as data rather than as a sentence the UI has to invent:
    a hardcoded "AI" label in a client keeps making that claim after the row behind it
    changed.

    **`result` is the provider's own JSON, re-serialized and nothing else.** It was validated
    into a `Classification` or a `SentimentResult` on the way in and stored as that model's
    dump, so this is the same document the validation passed rather than a second
    interpretation of it. It is typed as a bare mapping on purpose: naming a shape here would
    be a promise this layer cannot keep for the next operation, and §4's *"treat AI output as
    untrusted data"* is better served by a type that promises nothing. **This is the only
    field on the response that a language model wrote.**

    `error_message` is a staff-facing sentence, and it is why this route is guarded by an AI
    capability rather than `TICKET_VIEW`. Its column's comment: *"Surfaced to staff, never to
    customers: upstream errors can echo prompt content."* `TICKET_VIEW` is held by portal
    customers, so guarding with it would hand a customer the one field on the row the column
    was written to keep from them. It is `None` on anything that has not failed.

    `prompt_tokens` and `completion_tokens` are reported because they are the per-analysis
    half of §28's cost story — `/analytics/overview` totals them across the tenant, and this
    says which ticket spent them.

    `None` throughout for a row that has not finished: `confidence`, `result`,
    `completed_at`, and the token counts are all written at the same moment.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    operation: AIOperation
    status: ProcessingStatus

    #: The validated model output, verbatim. `None` until the row completes.
    result: dict[str, Any] | None

    confidence: float | None

    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None

    provider: str
    model: str
    error_message: str | None

    completed_at: datetime | None
    created_at: datetime
