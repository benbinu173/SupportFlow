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
from app.models.enums import Sentiment
from app.schemas.ai import (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)

pytestmark = pytest.mark.unit

# Every output schema the AI layer can produce. Parameterised over rather than listed
# three times, so a fifth schema added later is covered by the shared properties below
# without anybody remembering to add it.
ALL_SCHEMAS: tuple[type[BaseModel], ...] = (
    Classification,
    ConversationSummary,
    SentimentResult,
    SuggestedReply,
)


# ---------------------------------------------------------------------------
# §46 — valid structured response
# ---------------------------------------------------------------------------


def test_a_model_shaped_mapping_validates() -> None:
    """§18's own example, verbatim."""
    result = validate_output(
        Classification,
        {"category": "Billing", "subcategory": "Duplicate Charge", "confidence": 0.94},
    )

    assert isinstance(result, Classification)
    assert result.category == "Billing"
    assert result.subcategory == "Duplicate Charge"
    assert result.confidence == 0.94


def test_a_json_string_validates() -> None:
    """A provider that hands back text instead of a decoded object is still answerable."""
    result = validate_output(Classification, '{"category": "Billing", "confidence": 0.94}')

    assert result.category == "Billing"


def test_an_instance_is_returned_unchanged() -> None:
    """The short circuit the fake provider relies on — see `validate_output`'s docstring."""
    given = Classification(category="Billing", confidence=0.94)

    assert validate_output(Classification, given) is given


def test_an_absent_subcategory_is_allowed() -> None:
    """`subcategory` is optional because the subdivision is not always available.

    A model forced to choose would invent one, and an invented `"General"` is
    indistinguishable from a real answer in a way `null` is not.
    """
    result = validate_output(Classification, {"category": "Billing", "confidence": 0.5})

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
        # An object missing the field the schema requires.
        ({"confidence": 0.94}, "missing"),
        # A confidence outside the range, in the two ways a model produces it: a
        # percentage, and a negative.
        ({"category": "Billing", "confidence": 1.4}, "less_than_equal"),
        ({"category": "Billing", "confidence": -0.1}, "greater_than_equal"),
        # A field of the wrong type entirely.
        ({"category": "Billing", "confidence": "very high"}, "float_parsing"),
        # A field the schema does not have. `extra="forbid"` is what makes this a
        # rejection rather than a value that silently disappears.
        ({"category": "Billing", "confidence": 0.9, "urgency": "high"}, "extra_forbidden"),
        # A category past the column's own width, so the failure lands here and not in a
        # transaction that has already spent the tokens.
        ({"category": "B" * 101, "confidence": 0.9}, "string_too_long"),
        # An empty category — a model that answered with nothing.
        ({"category": "", "confidence": 0.9}, "string_too_short"),
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
            {"category": "Refund for order 88213", "confidence": "extremely likely"},
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
# The schema handed to the model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=lambda s: s.__name__)
def test_no_schema_reaches_the_provider_with_a_ref(schema: type[BaseModel]) -> None:
    """`$defs` and `$ref` are Pydantic's, not the provider's.

    Every one of these schemas is read by a model as much as by a validator, and an
    indirection it has to chase is one more thing to get wrong. `Sentiment` is the only
    `$ref` the four have, which is why this is a parameterised test over all of them rather
    than a check on the one that currently needs it.
    """
    rendered = str(_tool_schema(schema))

    assert "$ref" not in rendered
    assert "$defs" not in rendered


def test_the_inlined_enum_keeps_its_members() -> None:
    """Resolving the reference must not lose the vocabulary it pointed at."""
    sentiment = _tool_schema(SentimentResult)["properties"]["sentiment"]

    assert sentiment["enum"] == ["positive", "neutral", "negative"]


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
