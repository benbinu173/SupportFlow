"""Health endpoint tests.

These also serve as the smoke test that the app assembles: if configuration,
routing, or middleware wiring is broken, these fail first.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.unit
def test_health_returns_ok(client: TestClient) -> None:
    """Liveness probes no dependencies, so it stays a unit test."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
def test_readiness_reports_dependency_checks(client: TestClient) -> None:
    """Readiness genuinely connects, so this needs Postgres and Redis running."""
    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"database", "redis"}


@pytest.mark.integration
def test_readiness_reports_degraded_when_a_dependency_is_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed probe degrades the endpoint rather than raising.

    503 is what an orchestrator acts on; a 500 from an unhandled driver error
    would be indistinguishable from the app itself being broken.
    """
    monkeypatch.setattr("app.api.health._check_database", _always_fails)

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] is False


async def _always_fails() -> bool:
    return False


@pytest.mark.integration
def test_health_does_not_leak_infrastructure_detail(client: TestClient) -> None:
    """Health output must stay free of connection strings and hostnames."""
    body = client.get("/health/ready").text.lower()

    for leak in ("postgres", "redis://", "password", "localhost", "5432"):
        assert leak not in body
