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

**Prices are USD per million tokens**, as published, from three sources. Anthropic's model
pricing page, <https://platform.claude.com/docs/en/about-claude/pricing>, read **2026-09-17**
— Sonnet 5's $2/$10 was announced as introductory pricing through 2026-08-31 and that page
now records it as standard, so the scheduled increase did not happen. Groq's model page,
<https://console.groq.com/docs/model/openai/gpt-oss-120b>, read **2026-09-22**. And OpenAI's
pricing page, <https://platform.openai.com/docs/pricing>, read **2026-10-04**, for the one
embedding model Phase X can call. That is exactly the kind of change that would silently
corrupt a running ledger if the rate were read from the provider at display time instead of
recorded at write time.

**A rate names its provider, and that is load-bearing.** Before there were two vendors a model
name was enough to identify a rate, because every name in the table belonged to the same one.
With two, `AI_MODEL=claude-sonnet-5` beside `AI_PROVIDER=groq` is a *priced* model served by
the *wrong* vendor — a pairing that passes a "do we know this rate?" check and then fails as a
401 from a service that was never going to recognise the key. So `ModelRate` carries the vendor
and `app/core/config.py` refuses a mismatch at startup, where the message can say what to
change.

**Groq's free tier bills nothing, and the number here is still its published rate.** The
column answers "what did these tokens cost at the provider's published price", which is the
question a cost dashboard is asked and the one that survives the tier changing underneath it.
Recording zero because the account happens to be free would make every figure in the product
$0.000000 — a number that is neither the charge nor the value, and therefore no use as either.
The distinction is written down here because it is the kind of thing a reader is entitled to
disagree with once they have seen it stated.

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
    """One model's published price, per million tokens, and the vendor that serves it."""

    #: The `AI_PROVIDER` value that can serve this model. Config refuses a mismatch, so a
    #: model and its vendor cannot be configured apart from each other.
    provider: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal


# The models a support desk would plausibly configure. Not the full catalogue: a rate table
# that lists models nobody can select is a maintenance burden with no consumer, which is the
# same rule `Settings` follows about unused keys. Groq is reachable with eleven models and
# this table carries one of them, for the same reason — `openai/gpt-oss-120b` is the one whose
# tool calling this project has actually verified.
#
# **The last row is an embedding model, and its output rate of zero is a fact rather than an
# unknown.** An embedding call has no completion tokens — the same input produces the same
# vector, so there is nothing generated to bill — and OpenAI's page quotes one number for it.
# That is a different thing from the zero this module refuses elsewhere, which is the price we
# do not know. The two are distinguished by being written down in different places: an unknown
# price raises from `rate_for`, and this one multiplies out to the input cost alone.
#
# It is also why `EMBEDDING_MODEL` can be validated by exactly the same two checks `AI_MODEL`
# is — priced at all, and served by the provider it is configured against. Phase X added this
# row and no arithmetic: `cost_usd` already computes `prompt * input + completion * output`,
# and a zero in the second term is a zero.
RATES: dict[str, ModelRate] = {
    "claude-sonnet-5": ModelRate("anthropic", Decimal("2"), Decimal("10")),
    "claude-opus-5": ModelRate("anthropic", Decimal("5"), Decimal("25")),
    "claude-haiku-4-5-20251001": ModelRate("anthropic", Decimal("1"), Decimal("5")),
    "claude-fable-5-1": ModelRate("anthropic", Decimal("10"), Decimal("50")),
    "claude-fable-5": ModelRate("anthropic", Decimal("10"), Decimal("50")),
    "openai/gpt-oss-120b": ModelRate("groq", Decimal("0.15"), Decimal("0.60")),
    "text-embedding-3-small": ModelRate("openai", Decimal("0.02"), Decimal("0")),
}


def is_priced(model: str) -> bool:
    """Whether a rate is known for `model`. Used by `Settings` at validation time."""
    return model in RATES


def priced_models() -> tuple[str, ...]:
    """Every model that may be configured, for an error message that says what to do."""
    return tuple(sorted(RATES))


def models_for(provider: str) -> tuple[str, ...]:
    """Every model `provider` can serve, for the same reason and in the same voice.

    Exists so the pairing check in `app/core/config.py` can answer "so what *should* I have
    written?" — a validator that only reports the mismatch leaves the reader to find the rate
    table, and on a first run that is the whole difficulty.
    """
    return tuple(sorted(model for model, rate in RATES.items() if rate.provider == provider))


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
