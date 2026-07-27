PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip

.PHONY: install test lint format compose-up compose-down smoke backup restore-test clean

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -e ".[dev]"

test:
	$(VENV_PYTHON) -m pytest

lint:
	$(VENV_PYTHON) -m ruff check .

format:
	$(VENV_PYTHON) -m ruff format .

compose-up:
	docker compose up --build -d

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
