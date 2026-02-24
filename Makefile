.DEFAULT_GOAL := help

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD  := \033[1m
RESET := \033[0m
GREEN := \033[32m
CYAN  := \033[36m

# ── Helpers ───────────────────────────────────────────────────────────────────
.PHONY: help install lint format typecheck check test test-unit test-integration \
        cluster-up cluster-down build clean

help: ## Show this help message
	@printf '$(BOLD)langchain-k8s — available targets$(RESET)\n\n'
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-20s$(RESET) %s\n", $$1, $$2}'

# ── Setup ─────────────────────────────────────────────────────────────────────
install: ## Install all dependencies (including dev) via uv
	uv sync --all-groups

# ── Linting & formatting ──────────────────────────────────────────────────────
lint: ## Run ruff linter
	uv run ruff check src/ tests/

format: ## Auto-format code with ruff
	uv run ruff format src/ tests/

format-check: ## Check formatting without making changes (used in CI)
	uv run ruff format --check src/ tests/

typecheck: ## Run pyright type checker
	uv run pyright src/

check: lint format-check typecheck ## Run all checks (lint + format + typecheck)

# ── Tests ─────────────────────────────────────────────────────────────────────
test-unit: ## Run unit tests (no cluster required)
	uv run pytest tests/unit/ -v --cov=src/langchain_k8s --cov-report=term-missing -m "not integration"

test-integration: ## Run integration tests (requires Kind cluster — run 'make cluster-up' first)
	uv run pytest tests/integration/ -v -m integration

test: test-unit test-integration ## Run all tests (unit + integration)

# ── Kind cluster ──────────────────────────────────────────────────────────────
cluster-up: ## Spin up a local Kind cluster for integration testing
	./scripts/kind-setup.sh

cluster-down: ## Tear down the local Kind cluster
	./scripts/kind-teardown.sh

# ── Build ─────────────────────────────────────────────────────────────────────
build: ## Build the distributable package
	uv build

# ── Clean ─────────────────────────────────────────────────────────────────────
clean: ## Remove build artefacts and cache directories
	rm -rf dist/ .coverage .pytest_cache
	find . -type d -name '__pycache__' -exec rm -rf {} +
	find . -type d -name '*.egg-info' -exec rm -rf {} +
