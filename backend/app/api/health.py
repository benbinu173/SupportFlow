"""Liveness and readiness endpoints.

Responses stay deliberately thin: enough for an orchestrator to act on, nothing that
discloses infrastructure detail to an unauthenticated caller.
"""

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, bool]


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: the process is up and serving. No dependency checks."""
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(response: Response) -> ReadinessResponse:
    """Readiness: critical dependencies are reachable.

    Phase C returns placeholders; Phase D replaces these with real Postgres and
    Redis probes. Failures report a boolean only — never an error string, which
    could leak hostnames or credentials.
    """
    checks = {"database": True, "redis": True}

    if not all(checks.values()):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="degraded", checks=checks)

    return ReadinessResponse(status="ready", checks=checks)
