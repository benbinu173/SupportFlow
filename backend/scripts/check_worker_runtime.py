"""Check production worker imports and drivers without contacting external services."""

import os
from importlib import import_module
from typing import cast

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool


def main() -> None:
    # Exercise the driverless URL supplied by managed PostgreSQL providers. Use only
    # throwaway settings: this check imports tasks but never runs them or opens sockets.
    os.environ.update(
        {
            "ENVIRONMENT": "production",
            "DEBUG": "false",
            "KNOWLEDGE_ENABLED": "false",
            "DATABASE_URL": (
                "postgresql://runtime:runtime@localhost:5432/runtime"
                "?sslmode=verify-full&sslrootcert=system"
            ),
            "REDIS_URL": "redis://localhost:6379/0",
            "CELERY_BROKER_URL": "redis://localhost:6379/2",
            "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
            "JWT_SECRET": "runtime-check-only-secret-at-least-32-characters",
            "S3_ENDPOINT": "http://localhost:9000",
            "S3_ACCESS_KEY": "runtime-check",
            "S3_SECRET_KEY": "runtime-check",
            "AI_API_KEY": "",
            "EMBEDDING_API_KEY": "",
        }
    )

    from app.workers.celery_app import celery_app

    # This is the same loader Celery uses before its worker starts. Checking only the
    # Celery app object misses task-module imports and their database engine setup.
    celery_app.loader.import_default_modules()
    for module_name in celery_app.conf.include:
        engine = cast(AsyncEngine, vars(import_module(module_name))["_engine"])
        assert engine.dialect.is_async, module_name
        assert engine.url.drivername == "postgresql+psycopg", module_name
        assert engine.url.query["sslmode"] == "verify-full", module_name
        assert engine.url.query["sslrootcert"] == "system", module_name
        assert isinstance(engine.pool, NullPool), module_name
        print(f"Worker module loaded: {module_name}")  # noqa: T201

    with celery_app.connection() as connection:
        assert connection.transport.driver_type == "redis"
    assert celery_app.backend.__class__.__name__ == "RedisBackend"
    print("PostgreSQL driver, TLS options, task imports, and Redis transport passed.")  # noqa: T201


if __name__ == "__main__":
    main()
