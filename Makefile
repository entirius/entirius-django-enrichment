.PHONY: help install check fix test
.DEFAULT_GOAL := help

help:  ## List targets
	@grep -E '^[a-z-]+:.*##' $(firstword $(MAKEFILE_LIST)) | awk -F':.*##' '{printf "  %-16s %s\n", $$1, $$2}'
install:  ## Sync dependencies (uv, incl. extras)
	uv sync --all-extras
check:  ## Lint + format-check (ruff) + canonical .gitleaks.toml
	@grep -q "forbidden-names" .gitleaks.toml 2>/dev/null || { echo "Missing or non-canonical .gitleaks.toml - symlink the config per the internal secret-scanning standard"; exit 1; }
	uv run ruff check .
	uv run ruff format --check .
fix:  ## Auto-fix lint + format
	uv run ruff check --fix .
	uv run ruff format .
test:  ## Test suite (pytest)
	uv run pytest -x -q
