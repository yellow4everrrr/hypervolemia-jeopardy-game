.DEFAULT_GOAL := help
SHELL := /bin/bash

API := services/api
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Setup ---------------------------------------------------------------------

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

.PHONY: install
install: $(VENV)/bin/activate ## Install the API and its dev dependencies
	$(PIP) install -e "$(API)[dev]"

# --- Quality -------------------------------------------------------------------

.PHONY: test
test: ## Run the test suite
	cd $(API) && ../../$(PY) -m pytest

.PHONY: test-cov
test-cov: ## Run tests with a coverage report
	cd $(API) && ../../$(PY) -m pytest --cov --cov-report=term-missing

.PHONY: lint
lint: ## Lint with ruff
	cd $(API) && ../../$(VENV)/bin/ruff check .

.PHONY: format
format: ## Auto-fix lint findings and format
	cd $(API) && ../../$(VENV)/bin/ruff check . --fix && ../../$(VENV)/bin/ruff format .

.PHONY: typecheck
typecheck: ## Type-check with mypy (strict)
	cd $(API) && ../../$(VENV)/bin/mypy app

.PHONY: check
check: lint typecheck test ## Everything CI runs

# --- Database ------------------------------------------------------------------

.PHONY: migrate
migrate: ## Apply migrations to the configured database
	cd $(API) && ../../$(VENV)/bin/alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add x"
	cd $(API) && ../../$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

.PHONY: downgrade
downgrade: ## Roll back one migration
	cd $(API) && ../../$(VENV)/bin/alembic downgrade -1

.PHONY: seed
seed: ## Load reference instruments (ES, NQ, CL, GC and their micros)
	cd $(API) && ../../$(PY) -m app.scripts.seed_instruments

# --- Local stack ---------------------------------------------------------------

.PHONY: up
up: ## Start Postgres/TimescaleDB, Redis and the API
	docker compose -f infra/docker-compose.yml up -d --build

.PHONY: down
down: ## Stop the local stack
	docker compose -f infra/docker-compose.yml down

.PHONY: logs
logs: ## Tail API logs
	docker compose -f infra/docker-compose.yml logs -f api

.PHONY: dev
dev: ## Run the API against a local database with hot reload
	cd $(API) && ../../$(VENV)/bin/uvicorn app.main:app --reload --port 8000

.PHONY: clean
clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(API)/.pytest_cache $(API)/.mypy_cache $(API)/.ruff_cache $(API)/.coverage
