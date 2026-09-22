"""What a call cost — the provider's published rate, applied at write time.

`app/models/ai_usage.py` says why this exists as a table rather than as a formula: *"Cost is
computed at write time from the provider's published rate, because rates change and a
historical row must keep the price actually charged."* So a row's `cost_usd` is a fact about
the past, and this module is the only thing that produces it.

**Rates live here, not in `Settings`.** A price is a property of the model, not of the
deployment: two organizations pointing at the same model are charged the same, and a
setting a deployment can get wrong is a setting that will be got wrong — for money. So the
table is code, and `AI_MODEL` is validated against it in `app/core/config.py`, which means a
model whose price is unknown cannot be configured at all. The alternative was a deployment
quietly writing `cost_usd = 0` for every call, which a dashboard renders as "free" rather
than as "unpriced" — a wrong number rather than a missing one, which is the worse of the two.

Adding a model is therefore a code change, and that is the point: the rate has to come from
somewhere, and this is where it is written down.

**Prices are USD per million tokens**, as published. Source: Anthropic's model pricing page,
<https://platform.claude.com/docs/en/about-claude/pricing>, read **2026-09-17**. Sonnet 5's
$2/$10 was announced as introductory pricing through 2026-08-31 and that page now records it
as standard — the scheduled increase did not happen. That is exactly the kind of change that
would silently corrupt a running ledger if the rate were read from the provider at display
time instead of recorded at write time.

**`Decimal` throughout, never `float`.** `cost_usd` is `Numeric(12, 6)` because it is money
that gets summed over thousands of rows, and the third decimal place of a fraction of a cent
is where binary floating point starts being wrong in the last digit. A per-call cost is
tiny — a few thousand tokens of Sonnet 5 is a fraction of a cent — so the rounding here is
not cosmetic: it is the difference between a monthly total that reconciles and one that does
not.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.ai.errors import AIPermanentError

#: Tokens are counted in ones and prices are quoted per million, so this is the divisor.
_MTOK = Decimal(1_000_000)

#: `ai_usage.cost_usd` is `Numeric(12, 6)`. Rounding to the column's own scale here means
#: the value the service hands SQLAlchemy is the value PostgreSQL stores, with no silent
#: second rounding at the driver.
_SCALE = Decimal("0.000001")


@dataclass(frozen=True)
class ModelRate:
    """One model's published price, per million tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


# The models a support desk would plausibly configure. Not the full catalogue: a rate table
# that lists models nobody can select is a maintenance burden with no consumer, which is the
# same rule `Settings` follows about unused keys.
RATES: dict[str, ModelRate] = {
    "claude-sonnet-5": ModelRate(Decimal("2"), Decimal("10")),
    "claude-opus-5": ModelRate(Decimal("5"), Decimal("25")),
    "claude-haiku-4-5-20251001": ModelRate(Decimal("1"), Decimal("5")),
    "claude-fable-5-1": ModelRate(Decimal("10"), Decimal("50")),
    "claude-fable-5": ModelRate(Decimal("10"), Decimal("50")),
}


def is_priced(model: str) -> bool:
    """Whether a rate is known for `model`. Used by `Settings` at validation time."""
    return model in RATES


def priced_models() -> tuple[str, ...]:
    """Every model that may be configured, for an error message that says what to do."""
    return tuple(sorted(RATES))


def rate_for(model: str) -> ModelRate:
    """The published rate for `model`.

    Raises rather than returning zero, because zero is a claim that a call was free and
    this is the case where the honest answer is that we do not know. `Settings` refuses the
    model at startup, so reaching this with an unknown name means a model reached the call
    path without passing configuration — a bug worth a loud failure rather than a ledger
    full of free calls.
    """
    try:
        return RATES[model]
    except KeyError as exc:
        raise AIPermanentError(
            f"No published rate for {model!r}. Add it to app/ai/pricing.py "
            f"(known: {', '.join(priced_models())})."
        ) from exc


def cost_usd(model: str, *, prompt_tokens: int, completion_tokens: int) -> Decimal:
    """What one call cost, rounded to the ledger column's six decimal places.

    `ROUND_HALF_UP` rather than Python's default `ROUND_HALF_EVEN`: bankers' rounding is
    the right default for statistics and the wrong one for money, where a half-cent is
    conventionally rounded away from zero.

    A failed call still reaches here. The provider bills for the tokens it generated before
    it failed, and `ai_usage`'s own comment is that such a call *"is recorded rather than
    dropped"* — so the tokens a failure reported are priced exactly like any others, and a
    failure that reported none costs nothing.
    """
    rate = rate_for(model)
    total = (
        Decimal(prompt_tokens) * rate.input_per_mtok
        + Decimal(completion_tokens) * rate.output_per_mtok
    ) / _MTOK
    return total.quantize(_SCALE, rounding=ROUND_HALF_UP)
