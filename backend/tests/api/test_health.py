"""Health endpoint tests.

These also serve as the smoke test that the app assembles: if configuration,
routing, or middleware wiring is broken, these fail first.
"""

import pytest


@pytest.mark.unit
def test_health_returns_ok(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.unit
def test_readiness_reports_dependency_checks(client) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"database", "redis"}


@pytest.mark.unit
def test_health_does_not_leak_infrastructure_detail(client) -> None:
    """Health output must stay free of connection strings and hostnames."""
    body = client.get("/health/ready").text.lower()

    for leak in ("postgres", "redis://", "password", "localhost", "5432"):
        assert leak not in body
