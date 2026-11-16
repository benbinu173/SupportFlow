"""Validate settings and serialize preview migrations in a short-lived process."""

import logging
import subprocess
import sys

import psycopg

from app.core.config import get_settings


def main() -> None:
    settings = get_settings()
    dsn = settings.sqlalchemy_dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as connection:
        connection.execute("SET statement_timeout = '60s'")
        connection.execute("SELECT pg_advisory_lock(734281903)")
        # Inherit the supervisor-owned process group for shutdown during startup.
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)  # noqa: S603


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.getLogger(__name__).error("Preview settings validation or migration failed")
        sys.exit(1)
