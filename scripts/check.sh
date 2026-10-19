#!/usr/bin/env bash
# The repo's checks, the same steps as CI: lint, format check, type check and the offline tests.
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --frozen --quiet
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -q
