#!/usr/bin/env bash
# The repo's checks, the same steps as CI: the lock matches pyproject.toml, lint, format check, type check, the
# offline tests and the specification's numerical checks.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --locked --quiet
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -q
uv run kokoah-subnet verify
