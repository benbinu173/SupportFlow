"""`ai_analysis_service`'s pure parts — the reductions, the two request builders, the rule.

**Why this file needs no database.** Everything below is a function of its arguments: a
provider result becomes an outcome, a ticket becomes a request, a conversation becomes a
prompt, and an enum becomes a set of senders. The half of the module that does need a
database — `request_analysis`, `request_summary`, `run_analysis` — is exercised against a
real one in `tests/integration/test_ai_summary.py`, and over HTTP in
`tests/api/test_ai_summary.py`. Nothing here reaches for a session, and the builders below
are unsaved models, following `test_notification_policy.py`.

**The claim worth testing is that §20's summary and §21's draft are kinds of answer rather
than degraded versions of the first two.** Neither has a confidence, and the shortest way to
support that would have been to write a `0.0` into the column or to reach for
`getattr(value, "confidence", None)`. Both would let §19's schemas silently lose their
confidence, and neither would fail. So `_reduced` is tested against all four schemas here, and
the column's `None` is asserted to be *absence* rather than a made-up number.
"""

import uuid

import pytest

from app.ai import prompts
from app.ai.provider import AIRequest, AIResult
from app.core.config import get_settings
from app.models.enums import AIOperation, SenderType, Sentiment, TicketPriority
from app.models.message import Message
from app.models.ticket import Ticket
from app.repositories import ai_repository
from app.schemas.ai import (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)
from app.services import ai_analysis_service

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _ticket(subject: str = "The printer is on fire") -> Ticket:
    """An unsaved `Ticket`. Never flushed, so no session is needed to build one."""
    return Ticket(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        number=1042,
        customer_id=uuid.uuid4(),
        subject=subject,
        description="It really is.",
    )


def _message(sender_type: SenderType, body: str, *, is_internal: bool = False) -> Message:
    """An unsaved `Message`, for the conversation builders to read."""
    return Message(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        sender_type=sender_type,
        sender_user_id=uuid.uuid4(),
        body=body,
        is_internal=is_internal,
    )


# ---------------------------------------------------------------------------
# §20's summary has no confidence, and the column has to say so
# ---------------------------------------------------------------------------


def test_a_classification_reduction_keeps_its_confidence() -> None:
    result = AIResult(
        Classification(category="Billing", priority=TicketPriority.HIGH, confidence=0.82),
        prompt_tokens=1200,
        completion_tokens=80,
    )

    outcome = ai_analysis_service._reduced(result)

    assert outcome.confidence == 0.82
    assert outcome.prompt_tokens == 1200
    assert outcome.completion_tokens == 80


def test_a_sentiment_reduction_keeps_its_confidence() -> None:
    result = AIResult(
        SentimentResult(sentiment=Sentiment.NEGATIVE, confidence=0.61),
        prompt_tokens=900,
        completion_tokens=20,
    )

    outcome = ai_analysis_service._reduced(result)

    assert outcome.confidence == 0.61


def test_a_summary_reduction_reports_no_confidence_rather_than_a_number() -> None:
    """`None`, and not `0.0` — the column's check constraint allows the former and the
    latter would be a claim the model never made.

    `ai_analyses.confidence` is nullable with a `[0, 1]` range check, so a summary row is a
    row with no confidence rather than a row asserting zero certainty. A dashboard that
    averaged the column across operations would otherwise report §20's summaries as the
    least confident answers on the board.
    """
    result = AIResult(
        ConversationSummary(summary="The customer cannot download their policy document."),
        prompt_tokens=3000,
        completion_tokens=120,
    )

    outcome = ai_analysis_service._reduced(result)

    assert outcome.confidence is None
    # The tokens are still real spend and are still recorded — the absence is the
    # confidence alone.
    assert outcome.prompt_tokens == 3000
    assert outcome.completion_tokens == 120


def test_a_draft_reduction_reports_no_confidence_either() -> None:
    """§41's edit verb, as a column: a reply is rewritten rather than scored.

    `SuggestedReply` has one field by design — §41 says to show confidence where it is
    meaningful, and a number beside a send button is the first step toward a threshold that
    sends. The two tests above are what make this `None` a decision rather than a default: the
    same function does put a number in the column for the two schemas that have one.
    """
    body = "Thanks for reporting this — I've asked the platform team to look at the file store."
    result = AIResult(
        SuggestedReply(body=body),
        prompt_tokens=2_400,
        completion_tokens=60,
    )

    outcome = ai_analysis_service._reduced(result)

    assert outcome.confidence is None
    # The payload is the model's own dump, so the draft is where the row keeps it — the
    # `messages` row the worker also writes is a copy for the thread, not the record.
    assert outcome.payload == {"body": body}
    assert outcome.prompt_tokens == 2_400
    assert outcome.completion_tokens == 60


def test_the_payload_is_the_wire_form_of_the_model_not_the_enum() -> None:
    """`mode="json"`, so the row's JSONB holds `"high"` and not a `TicketPriority` member.

    A `StrEnum` would serialize to the same thing through `json.dumps`, and this makes that
    a property of the function rather than of every enum anybody adds later.
    """
    outcome = ai_analysis_service._reduced(
        AIResult(
            Classification(category="Billing", priority=TicketPriority.URGENT, confidence=0.5),
            prompt_tokens=1,
            completion_tokens=1,
        )
    )

    assert outcome.payload["priority"] == "urgent"
    assert isinstance(outcome.payload["priority"], str)
    assert outcome.payload["category"] == "Billing"


# ---------------------------------------------------------------------------
# The three request builders
# ---------------------------------------------------------------------------


def test_the_ticket_request_uses_the_operations_own_instruction() -> None:
    ticket = _ticket(subject="Cannot log in")

    request = ai_analysis_service._request_for(AIOperation.CLASSIFY, ticket)

    assert request.instruction == prompts.CLASSIFICATION_INSTRUCTION
    assert "Cannot log in" in request.content
    assert request.content_label == ai_analysis_service._CONTENT_LABEL
    assert request.max_tokens == get_settings().AI_MAX_TOKENS


def test_the_ticket_request_refuses_an_operation_it_has_no_instruction_for() -> None:
    """`_INSTRUCTIONS` is a dict precisely so §20's operation cannot be reached through it.

    A summary is asked about a conversation rather than about a ticket's two text fields, so
    `_summary_request` builds that call and `_INSTRUCTIONS` must not pretend to. The `KeyError`
    is the loud failure a future caller would want, and pinning it here is what stops somebody
    "fixing" the gap by adding the summary instruction to the dict.
    """
    with pytest.raises(KeyError):
        ai_analysis_service._request_for(AIOperation.SUMMARIZE, _ticket())


def test_the_summary_request_carries_the_conversation_and_its_own_label() -> None:
    conversation = [
        _message(SenderType.CUSTOMER, "It will not download."),
        _message(SenderType.AGENT, "Checking with the vendor.", is_internal=True),
    ]

    request = ai_analysis_service._summary_request(conversation)

    assert isinstance(request, AIRequest)
    assert request.instruction == prompts.CONVERSATION_SUMMARY_INSTRUCTION
    # The label is the caller's own words, per `app/ai/prompts.py` — never the ticket's
    # subject and never anything a customer wrote. It is separate from `_CONTENT_LABEL`
    # because a summary is asked about a thread, not about the description that opened it.
    assert request.content_label == ai_analysis_service._CONVERSATION_LABEL
    assert "It will not download." in request.content
    assert "Checking with the vendor." in request.content
    assert request.max_tokens == get_settings().AI_MAX_TOKENS


def test_the_summary_request_fences_nothing_itself() -> None:
    """The provider owns the fence, so the content the service hands it is bare text."""
    request = ai_analysis_service._summary_request([_message(SenderType.CUSTOMER, "hello")])

    assert prompts._OPEN not in request.content
    assert prompts._CLOSE not in request.content


def test_the_draft_request_carries_its_three_blocks_under_its_own_label() -> None:
    """§21's call reads the ticket, the conversation, and §22's passages, so `_request_for`
    cannot build it — it takes only a `Ticket`.

    The label is the third of the three, and it is the caller's own words naming all of the
    blocks rather than the ticket's subject — `app/ai/prompts.py`'s rule, restated here because
    this is the one prompt a person can send onward without retyping it. **Phase X widened the
    label and added the third block**, and the passage is asserted separately from the other two
    because it is the one whose presence varies: most drafts have none.
    """
    request = ai_analysis_service._draft_request(
        _ticket(subject="Cannot log in"),
        [
            _message(SenderType.CUSTOMER, "It will not download."),
            _message(SenderType.AGENT, "Checking with the vendor.", is_internal=True),
        ],
        ["Refunds take five working days to reach an account."],
    )

    assert isinstance(request, AIRequest)
    assert request.instruction == prompts.SUGGESTED_REPLY_INSTRUCTION
    assert request.content_label == ai_analysis_service._DRAFT_LABEL
    assert "Cannot log in" in request.content
    assert "It will not download." in request.content
    assert "Checking with the vendor." in request.content
    assert "Refunds take five working days to reach an account." in request.content
    assert request.content_label not in (
        ai_analysis_service._CONTENT_LABEL,
        ai_analysis_service._CONVERSATION_LABEL,
    )
    assert request.max_tokens == get_settings().AI_MAX_TOKENS


def test_the_draft_request_fences_nothing_itself() -> None:
    """Same rule as the other two builders: the provider is the one place that fences.

    Built with **no** conversation as well as with one, because the ticket-only path is the
    ordinary case (`create_ticket` writes no message) and it is the path a second fence
    implementation would be easiest to leave behind. The passage list is varied with it, since
    §22's block is the other one that may be absent — an empty list is a draft with nothing
    retrieved, which is the fail-open path rather than a missing argument.
    """
    for conversation in ([], [_message(SenderType.CUSTOMER, "hello")]):
        for knowledge in ([], ["Refunds take five working days."]):
            request = ai_analysis_service._draft_request(_ticket(), conversation, knowledge)
            assert prompts._OPEN not in request.content
            assert prompts._CLOSE not in request.content


# ---------------------------------------------------------------------------
# Which messages a summary is made from — §20's "the conversation"
# ---------------------------------------------------------------------------


def test_the_conversation_is_customer_and_agent_messages() -> None:
    assert ai_repository._CONVERSATION_SENDERS == (
        SenderType.CUSTOMER,
        SenderType.AGENT,
    )


def test_every_sender_type_is_eligible_or_deliberately_excluded() -> None:
    """Read from the enum, so a fifth `SenderType` is a decision rather than a default.

    This is the same mechanical sweep the prompt-value tests use. The two exclusions are the
    decision: a `SYSTEM` row is a status change rather than anything anybody said, and an
    `AI_DRAFT` is unsent — §21's *"AI must NEVER automatically send a customer-facing
    response"* made a data question, because a summary of what people said should not include
    a draft nobody has sent. Adding a sixth sender type without deciding which side of this
    it is on fails here.
    """
    eligible = set(ai_repository._CONVERSATION_SENDERS)

    assert set(SenderType) - eligible == {SenderType.SYSTEM, SenderType.AI_DRAFT}
