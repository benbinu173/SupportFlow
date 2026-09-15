"""Liveness and readiness endpoints.

Responses stay deliberately thin: enough for an orchestrator to act on, nothing that
discloses infrastructure detail to an unauthenticated caller.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.database import engine
from app.core.redis import get_client

router = APIRouter()

# A readiness probe must answer faster than the orchestrator's own timeout, or a
# hung dependency turns into a hung probe and the platform cannot tell the
# difference between "slow" and "dead".
_PROBE_TIMEOUT_SECONDS = 2.0


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, bool]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: the process is up and serving. No dependency checks."""
    return HealthResponse(status="ok")


async def _check_database() -> bool:
    """Whether a pooled connection can execute a trivial statement."""
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        # Deliberately broad: a probe reports reachability and must not itself
        # raise. Driver, timeout, and DNS failures are all the same answer here.
        return False
    return True


async def _check_redis() -> bool:
    """Whether Redis answers a PING.

    Uses the process's shared client rather than building one. This probe runs on a
    schedule, and a fresh `Redis.from_url` per probe meant a TCP connect and a Redis
    handshake every time, outside the pool every other consumer shares — visible as a
    `total_connections_received` that climbs by one per probe.

    Nothing is closed here. The client is borrowed, and `aclose()` on a shared pool
    would tear down the connections the rate limiter is using.
    """
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            await get_client().ping()
    except Exception:
        return False
    return True


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(response: Response) -> ReadinessResponse:
    """Readiness: critical dependencies are reachable.

    Both dependencies are probed concurrently, so the endpoint costs the slower of
    the two rather than their sum. Failures report a boolean only — never an error
    string, which could leak hostnames or credentials.
    """
    database_ok, redis_ok = await asyncio.gather(_check_database(), _check_redis())
    checks = {"database": database_ok, "redis": redis_ok}

    if not all(checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="degraded", checks=checks)

    return ReadinessResponse(status="ready", checks=checks)
