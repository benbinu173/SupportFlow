# SupportFlow — common development commands.
#
# On Windows, run these from Git Bash. Requires Docker Desktop running.

BACKEND_PY := backend/.venv/Scripts/python.exe

.PHONY: help
help:
	@echo "Setup"
	@echo "  make install        Install backend and frontend dependencies"
	@echo "  make up             Start infrastructure (postgres, redis, minio, mailpit)"
	@echo "  make down           Stop infrastructure"
	@echo "  make reset          Stop infrastructure and DELETE all local data"
	@echo ""
	@echo "Run"
	@echo "  make api            Run the FastAPI dev server"
	@echo "  make web            Run the Vite dev server"
	@echo "  make worker         Run the Celery worker (solo pool, Windows)"
	@echo "  make beat           Run the Celery scheduler, which drives the SLA sweep"
	@echo ""
	@echo "Database"
	@echo "  make migrate        Apply all pending migrations"
	@echo "  make migration m=\"...\"  Autogenerate a migration from model changes"
	@echo "  make downgrade      Revert the most recent migration"
	@echo "  make migration-sql  Print the pending DDL without running it"
	@echo "  make migration-check  Fail if models and migrations have drifted"
	@echo ""
	@echo "Verify"
	@echo "  make test           All tests"
	@echo "  make check          Lint, format, and type checks"
	@echo "  make security       Security and tenant-isolation tests only"
	@echo "  make ci             Everything CI runs"

# --- setup -----------------------------------------------------------------
.PHONY: install
install:
	cd backend && python -m venv .venv && $(CURDIR)/$(BACKEND_PY) -m pip install -e ".[dev]"
	cd frontend && npm install

.PHONY: up
up:
	docker compose up -d
	@echo "postgres:5432  redis:6379  minio:9001  mailpit:8025"

.PHONY: down
down:
	docker compose down

# Destructive: drops the Postgres, Redis, and MinIO volumes.
.PHONY: reset
reset:
	docker compose down -v

# --- run -------------------------------------------------------------------
# --loop: psycopg's async mode cannot drive Windows' ProactorEventLoop, which is
# what uvicorn picks by default. See app/core/event_loop.py.
.PHONY: api
api:
	cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --reload --loop app.core.event_loop:loop_factory

.PHONY: web
web:
	cd frontend && npm run dev

# `-Q notifications,sla` and not just `-Q notifications`: a worker consumes precisely the
# queues it names, so omitting the second one leaves SLA tasks sitting in Redis forever
# with no error on either side. The wiring test asserts this list equals `task_routes`.
.PHONY: worker
worker:
	cd backend && .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker --pool=solo --loglevel=info -Q notifications,sla

# A second process, and there must be exactly one of it. Beat publishes on a schedule; it
# consumes nothing, so it takes no pool flag and no `-Q`. Two beats each fire every entry,
# which would run the sweep twice per interval. The `-s` path is explicit because the
# default writes into the working directory and a beat that cannot write it fails at
# startup with a message about the file rather than about the cause.
.PHONY: beat
beat:
	cd backend && .venv/Scripts/python.exe -m celery -A app.workers.celery_app beat --loglevel=info -s .celerybeat-schedule

# --- database --------------------------------------------------------------
# Alembic reads DATABASE_URL from .env via app.core.config, so there is no URL in
# alembic.ini. To target another database, set DATABASE_URL in the environment.
ALEMBIC := cd backend && .venv/Scripts/alembic.exe

.PHONY: migrate
migrate:
	$(ALEMBIC) upgrade head

# `m` is required: an unnamed migration is unreviewable in a listing.
#
# The formatter runs on the generated file so that `make check` passes straight away.
# Alembic ships an unformatted file, and a migrations directory that is exempt from
# the project's formatting rules is one nobody reads comfortably.
.PHONY: migration
migration:
ifndef m
	$(error usage: make migration m="add ticket tags")
endif
	$(ALEMBIC) revision --autogenerate -m "$(m)"
	cd backend && .venv/Scripts/python.exe -m ruff format alembic/versions
	cd backend && .venv/Scripts/python.exe -m ruff check --fix alembic/versions
	@echo "Review the generated migration before committing it."

.PHONY: downgrade
downgrade:
	$(ALEMBIC) downgrade -1

# Review DDL before it runs, or hand it to a DBA.
.PHONY: migration-sql
migration-sql:
	$(ALEMBIC) upgrade head --sql

# Exits non-zero when a model change has no matching migration.
.PHONY: migration-check
migration-check:
	$(ALEMBIC) check

# --- verify ----------------------------------------------------------------
.PHONY: test
test:
	cd backend && .venv/Scripts/python.exe -m pytest
	cd frontend && npm test

.PHONY: check
check:
	cd backend && .venv/Scripts/python.exe -m ruff check .
	cd backend && .venv/Scripts/python.exe -m ruff format --check .
	cd backend && .venv/Scripts/python.exe -m mypy app alembic
	cd frontend && npx tsc -b && npm run lint

.PHONY: security
security:
	cd backend && .venv/Scripts/python.exe -m pytest -m security

.PHONY: ci
ci: check test
	cd frontend && npm run build
	docker build --target production -t supportflow-backend:ci ./backend

# `migration-check` is deliberately not a ci prerequisite. `make test` already
# asserts the same thing, via tests/integration/test_migrations.py, against a scratch
# database it migrates itself. Running `alembic check` here as well would need the
# developer's own database to be at head, which is a different and weaker guarantee.
