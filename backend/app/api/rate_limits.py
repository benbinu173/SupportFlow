"""Rate-limit guards — the whole limit surface, in one file.

Spec §45 names the endpoints to limit; this module is the list. Every guard is a route
dependency rather than a call inside an endpoint body, so the limit is enforced before
the work is reached: FastAPI resolves a path operation's dependencies before its own
parameters, which means an upload guard runs before the multipart body is parsed. The
honest boundary is that uvicorn has already pulled the bytes off the socket — what the
limit prevents is the validation, the storage write, and the object in the bucket.

**Read the guards here and you have read every limit the API applies.** That property is
the reason for the module: an abuse control whose full extent can only be discovered by
scanning the routers is one where a new endpoint quietly arrives unlimited, which is
exactly how upload came to have no limit at all between Phase L and Phase O.

Keying is per-endpoint and deliberate, not uniform — see each guard, and
`app/core/rate_limit.py` for the key builders:

| Guard | Key | Window | Setting |
|---|---|---|---|
| `limit_login` | client IP | 60s | `RATE_LIMIT_LOGIN_PER_MINUTE` |
| `limit_register` | client IP | 3600s | `RATE_LIMIT_REGISTER_PER_HOUR` |
| `limit_upload` | user id | 3600s | `RATE_LIMIT_UPLOAD_PER_HOUR` |
| `limit_ai` | user id | 3600s | `RATE_LIMIT_AI_PER_HOUR` |

The first two are per address because they run *before* authentication, where no
identity exists yet and the attack is spread across many accounts. The last two are per
user because they run *after* authentication, where identity exists and the abuse is one
account's — see `upload_rate_limit_key` and `ai_rate_limit_key`.

`limit_ai` is the one guard whose subject is not an attack but a bill: §53 names repeated
AI calls as waste, and every request it admits can become a provider call charged per
token. It is stated here because the module's rule is that every limit is stated here.

Nothing here holds a logger: `RateLimiter.enforce` does the logging, including the
warning that makes fail-open audible (ADR-014).
"""

from fastapi import Request

from app.api.deps import Context, client_ip
from app.core.config import get_settings
from app.core.rate_limit import (
    RateLimiter,
    ai_rate_limit_key,
    login_rate_limit_key,
    register_rate_limit_key,
    upload_rate_limit_key,
)

# One limiter for the process. It borrows the shared Redis client on first use, so there
# is one client and one pool regardless of how many guards exist.
_limiter = RateLimiter()


async def limit_login(request: Request) -> None:
    """Count one login attempt against the caller's address."""
    settings = get_settings()
    await _limiter.enforce(
        login_rate_limit_key(client_ip(request)),
        limit=settings.RATE_LIMIT_LOGIN_PER_MINUTE,
        window_seconds=60,
    )


async def limit_register(request: Request) -> None:
    """Count one registration against the caller's address."""
    settings = get_settings()
    await _limiter.enforce(
        register_rate_limit_key(client_ip(request)),
        limit=settings.RATE_LIMIT_REGISTER_PER_HOUR,
        window_seconds=3600,
    )


async def limit_upload(context: Context) -> None:
    """Count one upload against the authenticated user.

    Takes `Context` rather than `Request` because the key is the user, which means this
    guard necessarily runs after `get_current_user` — and therefore after the token has
    been verified and the user confirmed active. An unauthenticated caller never reaches
    the counter and is refused a 401 by the auth layer instead, which is the right
    answer: there is no account to hold responsible.
    """
    settings = get_settings()
    await _limiter.enforce(
        upload_rate_limit_key(context.user_id),
        limit=settings.RATE_LIMIT_UPLOAD_PER_HOUR,
        window_seconds=3600,
    )


async def limit_ai(context: Context) -> None:
    """Count one analysis request against the authenticated user.

    Keyed by user for the same reason as `limit_upload`, and it is the same key shape on
    purpose: this runs after authentication because it must — the caller has to hold
    `AI_REQUEST_ANALYSIS`, which is a decision about a role, and there is no role before
    the token is verified.

    **What it protects is a budget, not a credential.** The other three guards here keep
    an attacker out; this one keeps a client from spending the tenant's AI allowance by
    looping over a ticket id, which §53 names as waste and which the provider would bill
    for every time. It is a dependency rather than a check inside the handler so the
    counter is incremented before the rows are written — a request refused at 429 must
    leave no `pending` analysis behind, or the refusal would itself queue work.
    """
    settings = get_settings()
    await _limiter.enforce(
        ai_rate_limit_key(context.user_id),
        limit=settings.RATE_LIMIT_AI_PER_HOUR,
        window_seconds=3600,
    )
