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

The first two are per address because they run *before* authentication, where no
identity exists yet and the attack is spread across many accounts. The third is per user
because it runs *after* authentication, where identity exists and the abuse is one
account's — see `upload_rate_limit_key`.

Nothing here holds a logger: `RateLimiter.enforce` does the logging, including the
warning that makes fail-open audible (ADR-014).
"""

from fastapi import Request

from app.api.deps import Context, client_ip
from app.core.config import get_settings
from app.core.rate_limit import (
    RateLimiter,
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
