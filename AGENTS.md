# AGENTS.md — Agent Guide for langchain-k8s

This file provides guidance for AI agents (e.g. Claude, Codex, Copilot) working on this repository.

## Project Overview

`langchain-k8s` is a Python package that integrates Kubernetes-native execution sandboxes with LangChain Deep Agents. It exposes a `KubernetesSandbox` class that implements the `BaseSandbox` protocol from `deepagents`, allowing agents to run shell commands and perform file operations inside ephemeral or persistent Kubernetes pods.

**Key concepts:**

- `KubernetesSandbox` — the main class; wraps the `k8s_agent_sandbox.SandboxClient`
- **Persistent mode** (`reuse_sandbox=True`, default) — one pod is reused across calls
- **Ephemeral mode** (`reuse_sandbox=False`) — a fresh pod is created per `start()`/`stop()` cycle
- **Connection modes** — production (Gateway API), development (auto port-forward), advanced (direct `api_url`)
- `proxy.py` — monkey-patches a bug in the kubernetes Python client's NO_PROXY handling

## Repository Layout

```
src/langchain_k8s/
  __init__.py        # Public API: KubernetesSandbox, __version__
  sandbox.py         # KubernetesSandbox implementation
  proxy.py           # k8s client NO_PROXY monkey-patch
  _version.py        # Version constant

tests/
  conftest.py        # Shared fixtures and mock client factory
  unit/              # Unit tests (no cluster required)
  integration/       # Integration tests (requires Kind cluster)

k8s/                 # Kubernetes manifests for local development
scripts/             # kind-setup.sh / kind-teardown.sh

.github/workflows/
  publish.yml        # Publishes to PyPI on GitHub release
  ci.yml             # Runs unit tests on pull requests (linting, type check, tests)
  integration.yml    # Manually triggered integration tests against Kind cluster
```

## Quick Reference (Makefile)

Run `make` or `make help` to list all available targets. Common ones:

| Command | What it does |
|---------|--------------|
| `make install` | Install all dependencies via `uv sync` |
| `make check` | Lint + format check + typecheck |
| `make test-unit` | Unit tests with coverage |
| `make test-integration` | Integration tests (needs cluster) |
| `make test` | Both unit and integration tests |
| `make cluster-up` | Spin up local Kind cluster |
| `make cluster-down` | Tear down local Kind cluster |
| `make build` | Build distributable package |
| `make clean` | Remove build artefacts and caches |

## Package Manager

This project uses **[uv](https://docs.astral.sh/uv/)** — not pip or poetry.

```bash
uv sync                  # Install all dependencies (including dev)
uv add <package>         # Add a runtime dependency
uv add --dev <package>   # Add a dev dependency
uv run <command>         # Run a command in the project venv
```

Do **not** use `pip install` or `pip freeze` directly.

## Running Tests

### Unit tests (no cluster needed)

```bash
uv run pytest tests/unit/ -v
```

### Integration tests (requires Kind cluster)

First spin up a local Kind cluster:

```bash
./scripts/kind-setup.sh
```

Then run:

```bash
uv run pytest tests/integration/ -v -m integration
```

Tear down when done:

```bash
./scripts/kind-teardown.sh
```

Integration tests are marked with `@pytest.mark.integration`. They are excluded from unit test runs automatically via pytest marker filtering.

## Linting and Type Checking

```bash
uv run ruff check src/ tests/     # Lint
uv run ruff format src/ tests/    # Format
uv run pyright src/               # Type check (strict mode)
```

Ruff is configured with rules: E, F, I, UP, B, SIM. Line length is 120 characters, target Python 3.11.

`pyright` runs in strict mode (Pylance-compatible). Configuration lives in `[tool.pyright]` in `pyproject.toml`.

## Code Conventions

- **Python 3.11+** — use modern type hints (`X | Y`, `list[X]`, etc.)
- **No `return` type annotations needed on `__init__`** — mypy strict mode handles this
- **Lazy initialization** — `KubernetesSandbox.__init__` must not contact the cluster; use `_ensure_sandbox()` with a `threading.Lock`
- **Base64 file I/O** — file upload/download uses base64-encoded shell commands to avoid SDK limitations
- **Shell wrapping** — commands are wrapped in `sh -c '...'` for full POSIX semantics (pipes, redirects, `&&`)
- **Auto-reconnect** — persistent mode retries once on connection error before propagating
- **Error classification** — `_classify_error(output)` maps stderr patterns to `FileOperationError` types

## Adding New Features

1. Implement in `sandbox.py`; keep `proxy.py` isolated (it is infrastructure-only)
2. Export new public symbols from `__init__.py` and add to `__all__`
3. Add unit tests in `tests/unit/test_sandbox.py` using the existing mock client fixtures
4. Add integration tests in `tests/integration/test_kind.py` if cluster interaction is involved, marked with `@pytest.mark.integration`
5. Update `README.md` if behavior or configuration options change

## Upgrading the `k8s-agent-sandbox` Dependency

When bumping the `k8s-agent-sandbox` version, update **all** of the following:

| File | What to update |
|------|---------------|
| `pyproject.toml` | `k8s-agent-sandbox>=X.Y.Z` version constraint |
| `k8s/sandbox-template.yaml` | `python-runtime-sandbox` container image tag |
| `scripts/kind-setup.sh` | `AGENT_SANDBOX_VERSION` default value |
| `src/langchain_k8s/sandbox.py` | Adapt to any SDK API changes (renamed methods, new parameters) |
| `tests/conftest.py` | Update mocks if SDK interfaces changed (e.g. method renames) |
| `tests/unit/test_sandbox.py` | Update tests for mock changes and new features |
| `README.md` | Document new connection modes, parameters, or behaviour changes |

After updating, run `uv sync` then `make check && make test-unit` to verify.

## Key Files to Read First

When investigating a bug or adding a feature, start here:

| File | Why |
|------|-----|
| `src/langchain_k8s/sandbox.py` | Core logic — lifecycle, execution, file ops |
| `tests/unit/test_sandbox.py` | Existing unit test patterns and mock usage |
| `tests/conftest.py` | Shared fixtures, mock `SandboxClient` factory |
| `src/langchain_k8s/proxy.py` | Only touch if investigating proxy/k8s client issues |
| `specs/plans/foundation.md` | Original architectural decisions and design rationale |

## Environment Variables (Integration Tests)

Integration tests rely on a properly configured `kubectl` context pointing to a local Kind cluster named `agent-sandbox`. The setup script handles this:

```bash
./scripts/kind-setup.sh   # Creates cluster + installs controller + deploys manifests
```

No secrets or credentials are required for unit tests.

## CI Workflows

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `ci.yml` | PR to `main` | Lint, type check, unit tests on Python 3.11 and 3.12 |
| `integration.yml` | Manual (`workflow_dispatch`) | Spin up Kind, run integration tests |
| `publish.yml` | GitHub release published | Build and publish package to PyPI |

## Do Not

- Do not call `pip install` — always use `uv`
- Do not add cluster-dependent code to unit tests — mock the `SandboxClient`
- Do not skip `pyright` — fix type errors properly
- Do not contact the Kubernetes API in `__init__` — initialization must remain lazy
- Do not modify `proxy.py` unless fixing the specific NO_PROXY bug it addresses
