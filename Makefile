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

.PHONY: worker
worker:
	cd backend && .venv/Scripts/python.exe -m celery -A app.workers.celery_app worker --pool=solo --loglevel=info

# --- verify ----------------------------------------------------------------
.PHONY: test
test:
	cd backend && .venv/Scripts/python.exe -m pytest
	cd frontend && npm test

.PHONY: check
check:
	cd backend && .venv/Scripts/python.exe -m ruff check .
	cd backend && .venv/Scripts/python.exe -m ruff format --check .
	cd backend && .venv/Scripts/python.exe -m mypy app
	cd frontend && npx tsc -b && npm run lint

.PHONY: security
security:
	cd backend && .venv/Scripts/python.exe -m pytest -m security

.PHONY: ci
ci: check test
	cd frontend && npm run build
	docker build --target production -t supportflow-backend:ci ./backend
