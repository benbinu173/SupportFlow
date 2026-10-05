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
	@echo ""
	@echo "Deploy"
	@echo "  make seed           Create the §56 demo tenant in the LOCAL database"
	@echo "  make prod-up        Build and start the production stack, behind Caddy"
	@echo "  make prod-seed      The same tenant, in the RUNNING production stack"
	@echo "  make prod-down      Stop it, keeping its volumes"
	@echo "  make smoke          Smoke-test a running deployment through its proxy"

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

# `-Q notifications,sla,ai,knowledge` and not just `-Q notifications`: a worker consumes
# precisely the queues it names, so omitting one leaves its tasks sitting in Redis forever
# with no error on either side. The wiring test asserts this list equals `task_routes`.
.PHONY: worker
worker:
	cd backend && .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker --pool=solo --loglevel=info -Q notifications,sla,ai,knowledge

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
	# `scripts` is named here because CI names it. It was missing, which made `make check`
	# weaker than the gate that actually decides whether a change lands — three mypy errors
	# sat in a walkthrough script while this target passed. A local check that cannot fail
	# where CI fails is worse than no local check, because it is trusted.
	cd backend && .venv/Scripts/python.exe -m mypy app alembic scripts
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

# --- deploy ----------------------------------------------------------------
# The production stack is a separate compose file, not a profile of the development one.
# The development file bind-mounts source and runs `--reload`; the production file runs
# immutable images with no mounts. Those two cannot both be a default, so they are two
# files and the reader is never asked which one they are looking at.
#
# `--env-file` is explicit rather than relying on the default `.env` lookup, because a
# production deployment that silently picked up a developer's local `.env` would run with
# development secrets — and `Settings._production_is_not_a_development_checkout` refuses
# the published ones, but only the ones it knows about.
PROD_COMPOSE := docker compose --env-file .env.production -f docker-compose.prod.yml

.PHONY: prod-up
prod-up:
	@test -f .env.production || { \
		echo "No .env.production. Start from the template:"; \
		echo "  cp .env.production.example .env.production"; \
		echo "then fill in POSTGRES_PASSWORD, JWT_SECRET, and the rest."; \
		exit 1; \
	}
	$(PROD_COMPOSE) up -d --build
	@echo "The site is on the address in SITE_ADDRESS (default https://localhost)."
	@echo "Run 'make smoke' once it is healthy."

.PHONY: prod-down
prod-down:
	$(PROD_COMPOSE) down

# Destructive: drops the production Postgres, Redis, MinIO, and Caddy volumes.
.PHONY: prod-reset
prod-reset:
	$(PROD_COMPOSE) down -v

# The seed runs *inside* the stack, and that is not a convenience. The production database
# publishes no port (see docker-compose.prod.yml), so a host-run script would reach whatever
# DATABASE_URL is in the ambient .env — the development database — while its output claimed to
# have seeded the deployment. `scripts/` is copied into the production image for this reason.
#
# `-m scripts.seed_demo` and not `scripts/seed_demo.py`: `python <path>` puts the *script's*
# directory on sys.path, so `import app` resolves only because the developer's venv has the
# project installed editable. The production image installs dependencies but not the project —
# `app/` gets there by `COPY`, so it is importable from the working directory and nowhere else.
# The path form fails in the container with "No module named 'app'". `-m` from /app puts /app
# on sys.path, which is the one thing the image actually guarantees.
.PHONY: prod-seed
prod-seed:
	$(PROD_COMPOSE) exec backend python -m scripts.seed_demo $(ARGS)

# Writes through the service layer, so the demo tenant has a real audit trail and real SLA
# timers. Idempotent: refuses to run over an existing Acme Support unless --reset is given.
#
# Targets whatever DATABASE_URL the ambient .env names — the local/dev database. Against a
# running production stack the database has no published port, so this would seed the wrong
# one; `make prod-seed` is the deployment's version and runs inside the network.
#
# Same `-m` form as the target above, for the same reason: the command should not depend on
# the editable install being present, since that is a property of this checkout rather than of
# the project.
.PHONY: seed
seed:
	cd backend && .venv/Scripts/python.exe -m scripts.seed_demo $(ARGS)

# Points at a running deployment through its proxy, which is the only way to test that
# Caddy routes, that TLS terminates, and that a WebSocket upgrade survives the hop.
#
# `-m` for the same reason as the seed targets: the path form's `import app` depends on this
# checkout being installed editable, which is a property of a developer's venv and not of the
# repository. Every target that runs a script under `scripts/` uses this form.
.PHONY: smoke
smoke:
	cd backend && .venv/Scripts/python.exe -m scripts.deploy_smoke $(ARGS)
