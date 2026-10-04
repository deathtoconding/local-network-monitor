# Developer tasks for POSIX shells. Windows users: use .\scripts\dev.ps1 instead,
# which runs the same commands.
#
# Everything here is a thin wrapper over the tools CI runs, so "works on my
# machine" and "works in CI" stay the same sentence.

PYTHON ?= python
VENV   ?= .venv

# Import the package from the working tree even before an editable install.
export PYTHONPATH := src
BIN    := $(VENV)/bin
ifeq ($(OS),Windows_NT)
	BIN := $(VENV)/Scripts
endif

.DEFAULT_GOAL := help

.PHONY: help setup test cov lint format check run once api-only build clean audit

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtual environment and install dev requirements
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -r requirements-dev.txt

test: ## Run the test suite
	$(BIN)/python -m pytest

cov: ## Run tests with coverage (CI enforces >= 85%)
	$(BIN)/python -m pytest --cov=network_monitor --cov-report=term-missing --cov-fail-under=85

lint: ## Lint and check formatting
	$(BIN)/python -m ruff check src tests
	$(BIN)/python -m ruff format --check src tests

format: ## Apply formatting and auto-fixes
	$(BIN)/python -m ruff check src tests --fix
	$(BIN)/python -m ruff format src tests

check: lint cov ## Everything CI runs before packaging
	$(BIN)/python -m network_monitor --check-config

run: ## Start the dashboard on http://127.0.0.1:8000
	$(BIN)/python -m network_monitor

once: ## Run a single collection cycle and print a summary
	$(BIN)/python -m network_monitor --once

api-only: ## Serve the dashboard without collecting
	$(BIN)/python -m network_monitor --api-only

audit: ## Audit dependencies for known vulnerabilities
	$(BIN)/python -m pip_audit || $(BIN)/python -m pip install pip-audit && $(BIN)/python -m pip_audit

build: ## Build the wheel and sdist, and verify the metadata
	$(BIN)/python -m build
	$(BIN)/python -m twine check dist/*

clean: ## Remove build artefacts and caches (never touches data/ or logs/)
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .coverage coverage.xml
	find . -type d -name __pycache__ -not -path "./$(VENV)/*" -prune -exec rm -rf {} +
