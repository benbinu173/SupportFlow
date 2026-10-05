"""§18's guarantee, tested without a provider.

Spec §18: *"The backend must validate the returned structure. Never assume LLM output is
automatically valid."* `app/ai/provider.py`'s `validate_output` is the single
implementation of that sentence, and §46 names the two cases it has to answer: a **valid
structured response**, and a **malformed** one.

Both are tested here as pure functions, which is the whole reason validation was lifted out
of `ClaudeProvider`. A malformed-output test that needed a live model would be a test of
Anthropic's willingness to misbehave on request; this one is a test of what this codebase
does when it does.

The last two groups are the ones worth reading: that a rejection **names the rule and never
the value**, and that the schema sent to the model carries no `$ref` for it to chase.
"""

import pytest
from pydantic import BaseModel

from app.ai.errors import AIOutputError, AIPermanentError
from app.ai.provider import _tool_schema, validate_output
from app.models.enums import Sentiment, TicketPriority
from app.schemas.ai import (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)
from app.schemas.knowledge import KnowledgeAnswer

pytestmark = pytest.mark.unit

# Every output schema the AI layer can produce. Parameterised over rather than listed
# three times, so a fifth schema added later is covered by the shared properties below
# without anybody remembering to add it.
#
# **Phase X is that fifth schema, and it arrived from the other protocol.** `KnowledgeAnswer`
# is answered by the retrieval call rather than by one of `AIProvider`'s four, and it reaches
# the provider through the same `validate_output` — so it belongs in this table for the same
# reason the other four do, and the fact that it is governed by a second protocol is exactly
# why the table is a tuple of schemas rather than a walk of one module's methods.
ALL_SCHEMAS: tuple[type[BaseModel], ...] = (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
    KnowledgeAnswer,
)


# ---------------------------------------------------------------------------
# §46 — valid structured response
# ---------------------------------------------------------------------------


def test_a_model_shaped_mapping_validates() -> None:
    """§18's own example, extended with the one field §51 asks for beyond it.

    The spec's example answer is a category, a subcategory, and a confidence; §51's priority
    recommendation rides on the same call (see `Classification`'s docstring), so a real
    answer has four fields and this is what one looks like.
    """
    result = validate_output(
        Classification,
        {
            "category": "Billing",
            "subcategory": "Duplicate Charge",
            "priority": "high",
            "confidence": 0.94,
        },
    )

    assert isinstance(result, Classification)
    assert result.category == "Billing"
    assert result.subcategory == "Duplicate Charge"
    assert result.priority is TicketPriority.HIGH
    assert result.confidence == 0.94


def test_a_json_string_validates() -> None:
    """A provider that hands back text instead of a decoded object is still answerable."""
    result = validate_output(
        Classification,
        '{"category": "Billing", "priority": "low", "confidence": 0.94}',
    )

    assert result.category == "Billing"


def test_an_instance_is_returned_unchanged() -> None:
    """The short circuit the fake provider relies on — see `validate_output`'s docstring."""
    given = Classification(category="Billing", priority=TicketPriority.MEDIUM, confidence=0.94)

    assert validate_output(Classification, given) is given


def test_an_absent_subcategory_is_allowed() -> None:
    """`subcategory` is optional because the subdivision is not always available.

    A model forced to choose would invent one, and an invented `"General"` is
    indistinguishable from a real answer in a way `null` is not.

    **`priority` is not optional, and the contrast is the point.** A ticket always has a
    band, so a model that left it out did not decline to answer — it failed to. Making it
    defaulted would let that failure pass validation as a quiet `medium`, which is a
    fabricated recommendation wearing the model's name, and §6's comparison between the
    suggestion and the business decision would be reading a number nobody chose.
    """
    result = validate_output(
        Classification, {"category": "Billing", "priority": "medium", "confidence": 0.5}
    )

    assert result.subcategory is None


def test_sentiment_uses_the_database_enum() -> None:
    """The label has to land in a column typed by `app/models/enums.py`'s enum object."""
    result = validate_output(SentimentResult, {"sentiment": "negative", "confidence": 0.96})

    assert result.sentiment is Sentiment.NEGATIVE


# ---------------------------------------------------------------------------
# §46 — malformed AI response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        # Prose instead of JSON: the model answered the question in a sentence.
        ("The customer seems frustrated about a duplicate charge.", "not JSON"),
        # JSON, but not an object — a bare array or scalar from a model that lost the plot.
        ("[1, 2, 3]", "not an object"),
        ('"Billing"', "not an object"),
        # An object missing the fields the schema requires. `priority` is absent too, so
        # this is the "answered nothing at all" case rather than a single-field one.
        ({"confidence": 0.94}, "missing"),
        # `priority` alone, absent: the field §51 added, on an answer that is otherwise
        # well-formed. This is the case that would pass silently if it had a default.
        ({"category": "Billing", "confidence": 0.94}, "priority: missing"),
        # A confidence outside the range, in the two ways a model produces it: a
        # percentage, and a negative.
        ({"category": "Billing", "priority": "high", "confidence": 1.4}, "less_than_equal"),
        ({"category": "Billing", "priority": "high", "confidence": -0.1}, "greater_than_equal"),
        # A field of the wrong type entirely.
        ({"category": "Billing", "priority": "high", "confidence": "very high"}, "float_parsing"),
        # A field the schema does not have. `extra="forbid"` is what makes this a
        # rejection rather than a value that silently disappears.
        (
            {"category": "Billing", "priority": "high", "confidence": 0.9, "urgency": "x"},
            "extra_forbidden",
        ),
        # A category past the column's own width, so the failure lands here and not in a
        # transaction that has already spent the tokens.
        ({"category": "B" * 101, "priority": "high", "confidence": 0.9}, "string_too_long"),
        # An empty category — a model that answered with nothing.
        ({"category": "", "priority": "high", "confidence": 0.9}, "string_too_short"),
        # `null`, from a model that returned an explicit nothing.
        (None, "not an object"),
    ],
)
def test_a_malformed_payload_becomes_an_output_error(payload: object, reason: str) -> None:
    """Every way an answer can fail to be the schema is one condition with one handling.

    §18 asks for one behaviour, not eleven, so the assertion is the exception type first
    and the reason second: the type is the contract, the reason is for the log.
    """
    with pytest.raises(AIOutputError) as caught:
        validate_output(Classification, payload)

    assert reason in str(caught.value)


def test_an_invented_enum_member_is_rejected() -> None:
    """`Sentiment` is the database's vocabulary; a model may not add to it."""
    with pytest.raises(AIOutputError, match="sentiment: enum"):
        validate_output(SentimentResult, {"sentiment": "furious", "confidence": 0.9})


def test_an_invented_priority_is_rejected() -> None:
    """`TicketPriority` likewise, and the reason is stronger than for sentiment.

    A priority a model invented is a band the tenant's SLA policies have no row for — §27's
    targets are keyed by `priority` — so accepting `"critical"` here would move the failure
    from a validation error to a lookup that finds nothing, in a worker, after the tokens
    were spent. `Classification.priority` is an enum for the same reason `tickets.priority`
    is: the prompt teaches the four values by name, so a fifth is a model that did not read.
    """
    with pytest.raises(AIOutputError, match="priority: enum"):
        validate_output(
            Classification, {"category": "Billing", "priority": "critical", "confidence": 0.9}
        )


def test_output_error_is_not_a_permanent_error() -> None:
    """The two are siblings, and the difference is the whole reason both exist.

    `ai_service` retries `AITransientError` and nothing else, so this relationship is not
    cosmetic: if `AIOutputError` were a subclass of `AIPermanentError`, a future branch on
    the permanent type would start catching malformed answers too.
    """
    assert not issubclass(AIOutputError, AIPermanentError)


def test_the_rejection_never_quotes_the_value() -> None:
    """A reason names the rule broken, not what was sent.

    Two reasons this matters and they compound. The offending value is model output derived
    from a customer's words, so quoting it puts customer text in a log line; and
    `ValidationError.errors()` carries the raw input beside every failure, so the naive
    implementation — `str(exc)` — would do exactly that.
    """
    with pytest.raises(AIOutputError) as caught:
        validate_output(
            Classification,
            {
                "category": "Refund for order 88213",
                "priority": "high",
                "confidence": "extremely likely",
            },
        )

    message = str(caught.value)
    assert "confidence: float_parsing" in message
    assert "extremely likely" not in message
    assert "88213" not in message


def test_multiple_failures_are_summarised_not_dumped() -> None:
    """A model that ignored the schema can fail on every field at once."""
    with pytest.raises(AIOutputError) as caught:
        validate_output(
            Classification,
            {
                "category": 5,
                "subcategory": 6,
                "confidence": "high",
                "a": 1,
                "b": 2,
                "c": 3,
                "d": 4,
            },
        )

    # Counted as separators: the cap is five named failures, and the message stops there.
    assert str(caught.value).count(";") <= 4


# ---------------------------------------------------------------------------
# §23's answer schema
# ---------------------------------------------------------------------------


def test_a_grounded_answer_validates_and_keeps_its_indices() -> None:
    """`used_sources` arrives as the model wrote it and is **not** checked here.

    Resolving an index into a chunk is the caller's job, because only the caller holds the list
    of passages that were numbered. So an index past the end of that list is a legal value at this
    layer — `{"answer": ..., "used_sources": [7]}` is what a model that cited a passage it was
    not given produces, and refusing it here would replace a usable answer with a 502. §24's
    "do not fabricate citations" is served by the mapping in `knowledge_service`, not by this
    validator, and the two are separate on purpose.
    """
    result = validate_output(
        KnowledgeAnswer, {"answer": "Refunds take 5 working days.", "used_sources": [1, 3]}
    )

    assert isinstance(result, KnowledgeAnswer)
    assert result.used_sources == [1, 3]


def test_an_answer_with_no_sources_is_legal() -> None:
    """An empty list is a real answer, not a malformed one — a model that used nothing says so.

    §24's closing sentence lives in `KNOWLEDGE_INSTRUCTION`, so a model that read the passages
    and found they did not settle the question answers in prose and cites nothing. Treating that
    as a rejection would turn the specification's own refusal into an error.
    """
    result = validate_output(
        KnowledgeAnswer, {"answer": "The knowledge base does not say.", "used_sources": []}
    )

    assert result.used_sources == []


def test_an_answer_missing_its_citations_is_rejected() -> None:
    """`used_sources` is required, and the contrast with an empty list is the point.

    An answer that names no passages *and says so with an empty list* is the refusal §24 asks for.
    An answer with the field absent is a model that did not follow the shape — and defaulting it
    would let a truncated response pass as a deliberate "I used nothing", which is the one
    outcome the caller cannot tell apart from a real refusal.
    """
    with pytest.raises(AIOutputError, match="used_sources: missing"):
        validate_output(KnowledgeAnswer, {"answer": "Refunds take 5 working days."})


# ---------------------------------------------------------------------------
# The schema handed to the model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=lambda s: s.__name__)
def test_no_schema_reaches_the_provider_with_a_ref(schema: type[BaseModel]) -> None:
    """`$defs` and `$ref` are Pydantic's, not the provider's.

    Every one of these schemas is read by a model as much as by a validator, and an
    indirection it has to chase is one more thing to get wrong. This is a parameterised
    test over all four rather than a check on the one that currently needs it, because
    the set of schemas carrying a `$ref` is a property of the current field types and not
    a fixed list — Phase T had one (`Sentiment`), and `Classification.priority` made it
    two in the same schema. A test written for `SentimentResult` alone would have gone on
    passing while `Classification` started shipping a `$ref` to a model.
    """
    rendered = str(_tool_schema(schema))

    assert "$ref" not in rendered
    assert "$defs" not in rendered


def test_the_inlined_enum_keeps_its_members() -> None:
    """Resolving the reference must not lose the vocabulary it pointed at."""
    sentiment = _tool_schema(SentimentResult)["properties"]["sentiment"]

    assert sentiment["enum"] == ["positive", "neutral", "negative"]


def test_both_enums_in_one_schema_are_inlined() -> None:
    """`Classification` carries two, and each has to arrive whole.

    A model reads the enum members as the vocabulary it may answer in. An inliner that
    resolved the first reference and left the second — or that collapsed both to the same
    definition — would hand the model a schema that asks for a priority from the sentiment
    list, and the rejection would land on a model that answered exactly as instructed.
    """
    rendered = _tool_schema(Classification)["properties"]

    assert rendered["priority"]["enum"] == ["low", "medium", "high", "urgent"]
    assert "enum" not in rendered["category"]


def test_the_schema_forbids_unlisted_fields() -> None:
    """`extra="forbid"` reaches the model as `additionalProperties: false`.

    The constraint is stated to the model *and* enforced on its answer, which is the same
    claim in two places — and §54's "AI output validated" being in two places is the point.
    """
    assert _tool_schema(Classification)["additionalProperties"] is False


def test_every_schema_is_an_object_with_required_fields() -> None:
    """A tool `input_schema` describes an object; one that did not would be a provider error."""
    for schema in ALL_SCHEMAS:
        rendered = _tool_schema(schema)
        assert rendered["type"] == "object"
        assert rendered["required"]
