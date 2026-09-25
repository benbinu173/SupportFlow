"""What a call cost — exact, in `Decimal`, or refused.

`AIUsage`'s docstring gives the reason this is a table and not a formula: *"Cost is computed
at write time from the provider's published rate, because rates change and a historical row
must keep the price actually charged."* Two properties follow from that, and both are
tested here:

* **The arithmetic is exact.** `cost_usd` is `Numeric(12, 6)` and it is summed over
  thousands of rows, so the assertion is against an exact `Decimal` and never an approximate
  float comparison.
* **An unpriced model is refused, not priced at zero.** Zero is a claim that a call was
  free; the honest answer when the rate is unknown is to say so. `Settings` refuses the
  model at startup, which is where that failure actually lands — see `test_config.py` — but
  the refusal has to exist here too, because a rate table with no bottom is what makes
  `cost_usd = 0` rows possible in the first place.

The published rates are pinned rather than recomputed. That is deliberate: a price change
should be an edit to two places and a reviewer looking at both, not a silent change in what
the ledger records.
"""

from decimal import Decimal

import pytest

from app.ai.errors import AIPermanentError
from app.ai.pricing import (
    RATES,
    ModelRate,
    cost_usd,
    is_priced,
    models_for,
    priced_models,
    rate_for,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The published rates
# ---------------------------------------------------------------------------


def test_sonnet_5_is_two_and_ten_per_million() -> None:
    """Read from <https://platform.claude.com/docs/en/about-claude/pricing> on 2026-09-17.

    Pinned on purpose. Sonnet 5's $2/$10 was announced as introductory pricing through
    2026-08-31 and that page now records it as standard — the scheduled increase to $3/$15
    did not happen. If this test ever fails, the fix is to read the page again and edit
    `app/ai/pricing.py` *and* this test, which is exactly the review a price change should
    get.
    """
    rate = rate_for("claude-sonnet-5")

    assert (rate.input_per_mtok, rate.output_per_mtok) == (Decimal("2"), Decimal("10"))


def test_every_priced_model_has_a_positive_rate() -> None:
    """A zero or negative rate is a rate-table typo, and it would be invisible otherwise."""
    for name in priced_models():
        rate = rate_for(name)
        assert rate.input_per_mtok > 0, name
        assert rate.output_per_mtok > 0, name


def test_the_configured_default_model_is_priced() -> None:
    """`Settings.AI_MODEL` defaults to a model this table has to be able to price."""
    assert is_priced("claude-sonnet-5")


def test_gpt_oss_120b_is_fifteen_and_sixty_per_million() -> None:
    """Read from Groq's model page on 2026-09-22 (ADR-028).

    Pinned for the reason the Sonnet 5 test above gives. Note that this is the *published*
    rate and not what this project's account is billed: on Groq's free tier nothing is, and
    `cost_usd` still records the published price so the column keeps meaning something. The
    argument for that is in `app/ai/pricing.py`'s docstring, where a reader can disagree with
    it; the consequence is that a dashboard's total is "what these tokens are worth", which is
    what a cost panel is for.
    """
    rate = rate_for("openai/gpt-oss-120b")

    assert (rate.input_per_mtok, rate.output_per_mtok) == (Decimal("0.15"), Decimal("0.60"))
    assert rate.provider == "groq"


def test_every_rate_names_a_provider_that_serves_it() -> None:
    """The field exists so configuration can refuse a mismatch, which needs it to be right.

    A row whose `provider` were wrong would let `AI_MODEL` and `AI_PROVIDER` disagree with a
    clean bill of health from the pairing check — and the failure would reappear at the first
    call, as a 401 from a vendor holding a key for somebody else, which is the exact confusion
    the check was added to prevent.
    """
    for name, rate in RATES.items():
        assert rate.provider in {"anthropic", "groq"}, name
        assert name in models_for(rate.provider), name


def test_models_for_partitions_the_table() -> None:
    """Every priced model is reachable through exactly one provider, and none is orphaned."""
    listed = [model for provider in ("anthropic", "groq") for model in models_for(provider)]

    assert sorted(listed) == sorted(priced_models())
    assert len(listed) == len(set(listed))


def test_models_for_an_unknown_provider_is_empty() -> None:
    """`Settings` uses this to say what *should* have been written; a provider with no models
    is a case it has to survive, because that is what a `Literal` member added before its rate
    would look like."""
    assert models_for("nobody") == ()


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def test_cost_is_exact_for_a_known_model() -> None:
    """1 000 in at $2/MTok plus 500 out at $10/MTok is $0.007, to the column's scale."""
    assert cost_usd("claude-sonnet-5", prompt_tokens=1_000, completion_tokens=500) == Decimal(
        "0.007000"
    )


def test_cost_of_nothing_is_zero() -> None:
    """A failure that reported no tokens spent nothing, and the column says so."""
    assert cost_usd("claude-sonnet-5", prompt_tokens=0, completion_tokens=0) == Decimal("0")


def test_the_result_is_a_decimal_and_not_a_float() -> None:
    """The column is `Numeric`; a float on the way in is float error on the way out."""
    result = cost_usd("claude-sonnet-5", prompt_tokens=1, completion_tokens=1)

    assert isinstance(result, Decimal)


def test_the_result_is_rounded_to_the_columns_scale() -> None:
    """`Numeric(12, 6)` — more precision than this would be rounded by the driver anyway."""
    result = cost_usd("claude-haiku-4-5-20251001", prompt_tokens=1, completion_tokens=1)

    assert result == Decimal("0.000006")
    assert -result.as_tuple().exponent == 6


def test_a_half_is_rounded_away_from_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ROUND_HALF_UP`, not Python's `ROUND_HALF_EVEN` default.

    Bankers' rounding is the right default for statistics and the wrong one for money. No
    rate in the table is fractional today, so the distinction is unreachable through the
    published prices — which is precisely why it is tested through a patched table rather
    than left to be discovered the day a promotional rate arrives.
    """
    monkeypatch.setitem(
        RATES, "test-fractional", ModelRate("elsewhere", Decimal("0.5"), Decimal("0.5"))
    )

    # 1 token at $0.5/MTok is 0.0000005 — a half at the seventh place.
    assert cost_usd("test-fractional", prompt_tokens=1, completion_tokens=0) == Decimal("0.000001")


def test_a_large_sum_does_not_drift() -> None:
    """Ten thousand calls at $0.007 each is exactly $70 — the property the column exists for.

    The same sum in binary floating point is not; this assertion is the reason
    `app/schemas/analytics.py` renders `cost_usd` as a string rather than a JSON number.
    """
    per_call = cost_usd("claude-sonnet-5", prompt_tokens=1_000, completion_tokens=500)

    total = sum((per_call for _ in range(10_000)), Decimal(0))

    assert total == Decimal("70.000000")


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


def test_an_unpriced_model_is_refused_rather_than_priced_at_zero() -> None:
    """The failure has to be loud, because the alternative is a wrong number.

    A ledger full of zero-cost rows is not a missing answer — it is a *wrong* one, and a
    dashboard renders it as "free" rather than as "unpriced".
    """
    with pytest.raises(AIPermanentError) as caught:
        cost_usd("gpt-4-turbo", prompt_tokens=100, completion_tokens=100)

    message = str(caught.value)
    assert "gpt-4-turbo" in message
    # The message says what to do, not just what went wrong.
    assert "app/ai/pricing.py" in message
    assert "claude-sonnet-5" in message


def test_an_unpriced_model_is_not_reported_as_priced() -> None:
    assert not is_priced("gpt-4-turbo")
