#!/usr/bin/env bash
# Developer tasks for Linux/macOS. Windows users: .\scripts\dev.ps1
#
#   ./scripts/dev.sh test
#   ./scripts/dev.sh check
#
# Thin wrapper over the same commands CI runs, so a green local run means a green
# pipeline (barring platform differences, which the CI matrix covers).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="${VENV:-.venv}"
PYTHON="$VENV/bin/python"

# Import the package from the working tree even before an editable install.
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"

task="${1:-help}"

ensure_venv() {
    if [[ ! -x "$PYTHON" ]]; then
        echo "Creating virtual environment in $VENV ..."
        python3 -m venv "$VENV"
        "$PYTHON" -m pip install --upgrade pip
        "$PYTHON" -m pip install -r requirements-dev.txt
    fi
}

case "$task" in
    setup)    ensure_venv; "$PYTHON" -m pip install -r requirements-dev.txt ;;
    test)     ensure_venv; "$PYTHON" -m pytest ;;
    cov)      ensure_venv; "$PYTHON" -m pytest --cov=network_monitor --cov-report=term-missing --cov-fail-under=85 ;;
    lint)     ensure_venv; "$PYTHON" -m ruff check src tests; "$PYTHON" -m ruff format --check src tests ;;
    format)   ensure_venv; "$PYTHON" -m ruff check src tests --fix; "$PYTHON" -m ruff format src tests ;;
    check)    ensure_venv; "$PYTHON" -m ruff check src tests; "$PYTHON" -m ruff format --check src tests
                          "$PYTHON" -m pytest --cov=network_monitor --cov-fail-under=85
                          "$PYTHON" -m network_monitor --check-config ;;
    run)      ensure_venv; shift || true; "$PYTHON" -m network_monitor "$@" ;;
    once)     ensure_venv; "$PYTHON" -m network_monitor --once ;;
    api-only) ensure_venv; "$PYTHON" -m network_monitor --api-only --host 127.0.0.1 --port 8000 ;;
    build)    ensure_venv; "$PYTHON" -m build; "$PYTHON" -m twine check dist/* ;;
    audit)    ensure_venv; "$PYTHON" -m pip_audit ;;
    *)        echo "Usage: $0 {setup|test|cov|lint|format|check|run|once|api-only|build|audit}" ;;
esac
