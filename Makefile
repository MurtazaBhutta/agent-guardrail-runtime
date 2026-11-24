.PHONY: install test test-all lint db-up db-down db-schema demo clean

# Connection strings for the isolation tests. Two roles on purpose: the owner runs
# migrations and seeds fixtures, the application role is subject to row-level security.
# If the app connected as the owner, RLS would be bypassed and the isolation suite would
# pass while proving nothing.
ADMIN_DSN ?= postgresql://postgres:postgres@localhost:5432/guardrail
APP_DSN   ?= postgresql://guardrail_app:guardrail_app_dev_only@localhost:5432/guardrail

install:
	pip install -e ".[dev,postgres]"

# The middleware suite. No database, no credentials, no network.
test:
	pytest -m "not postgres"

# Everything, including the cross-tenant isolation tests. Needs `make db-up` first.
test-all:
	GUARDRAIL_ADMIN_DATABASE_URL=$(ADMIN_DSN) GUARDRAIL_DATABASE_URL=$(APP_DSN) pytest

lint:
	ruff check .

db-up:
	docker compose up -d --wait postgres
	$(MAKE) db-schema

db-schema:
	docker compose exec -T postgres psql -U postgres -d guardrail \
		< src/guardrail_runtime/tenancy/schema.sql

db-down:
	docker compose down -v

demo:
	python -m examples.walkthrough

clean:
	rm -rf .pytest_cache .ruff_cache
