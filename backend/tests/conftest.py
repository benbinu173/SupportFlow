"""Shared pytest fixtures.

Environment variables are set before importing application code so that
`Settings` validation succeeds without a real .env file present.
"""

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/supportflow_test",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("JWT_SECRET", "test-secret-value-that-is-long-enough-32")

# Imported after the environment is populated.
from app.main import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Synchronous test client against the real ASGI app."""
    with TestClient(create_app()) as test_client:
        yield test_client
