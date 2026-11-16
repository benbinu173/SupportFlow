"""Disabled knowledge routes must refuse before accessing data or providers."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.knowledge import router
from app.core.config import get_settings


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/knowledge"),
        ("POST", "/knowledge"),
        ("POST", "/knowledge/upload"),
        ("POST", "/knowledge/search"),
        ("GET", "/knowledge/00000000-0000-4000-8000-000000000001"),
        ("DELETE", "/knowledge/00000000-0000-4000-8000-000000000001"),
    ],
)
def test_disabled_routes_refuse_without_data_access(
    method: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "KNOWLEDGE_ENABLED", False)
    app = FastAPI()
    app.include_router(router, prefix="/knowledge")
    with TestClient(app) as client:
        response = client.request(method, path)
    assert response.status_code == 503
    assert response.json() == {"detail": "Knowledge base is unavailable in this release."}
