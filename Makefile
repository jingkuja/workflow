PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip

.PHONY: install test lint format compose-up compose-down smoke backup restore-test clean test-pg

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -e ".[dev]"

test:
	$(VENV_PYTHON) -m pytest

# 真实 PostgreSQL 集成测试：仅绑定回环地址，使用独立 workflow_test 数据库。
test-pg:
	docker compose up -d postgres
	docker compose exec -T postgres sh -c \
		'dropdb --if-exists --username="$$POSTGRES_USER" workflow_test; \
		 createdb --username="$$POSTGRES_USER" workflow_test'
	set -a && . ./.env && set +a && \
		TEST_DATABASE_URL="postgresql+psycopg://$${POSTGRES_USER}:$${POSTGRES_PASSWORD}@127.0.0.1:5432/workflow_test" \
		$(VENV_PYTHON) -m pytest tests/test_pg_integration.py -q

lint:
	$(VENV_PYTHON) -m ruff check .

format:
	$(VENV_PYTHON) -m ruff format .

compose-up:
	docker compose up --build -d --force-recreate

compose-down:
	docker compose down

smoke:
	$(VENV_PYTHON) -m workflow.scripts.t0_smoke

backup:
	scripts/backup_db.sh

restore-test:
	scripts/restore_db_test.sh $(BACKUP_FILE)

clean:
	$(VENV_PYTHON) -c "from pathlib import Path; import shutil; [shutil.rmtree(p, ignore_errors=True) for p in (Path('.pytest_cache'), Path('.ruff_cache'))]"
