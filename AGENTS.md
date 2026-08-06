# AGENTS.md — Agent Guide for langchain-k8s

This file provides guidance for AI agents (e.g. Claude, Codex, Copilot) working on this repository.

`CLAUDE.md` is a symlink to this file — there is one canonical agent guide, not two.

> **Trust source over prose.** `README.md` and `openwiki/` describe intent; `src/langchain_k8s/sandbox.py` describes behaviour. Where they disagree, the source wins. See [Invariants and Gotchas](#invariants-and-gotchas).

## OpenWiki

This repository has documentation located in the /openwiki directory.

Start here:

- [OpenWiki quickstart](openwiki/quickstart.md)

OpenWiki includes repository overview, architecture notes, workflows, domain concepts, operations, integrations, testing guidance, and source maps.

When working in this repository, read the OpenWiki quickstart first, then follow its links to the relevant architecture, workflow, domain, operation, and testing notes.

`openwiki/` is generated, not hand-maintained (see `openwiki/.last-update.json`). Use it as a map of the territory; confirm any specific mechanism, attribute name, or default against `src/` before relying on it.

## Project Overview

`langchain-k8s` is a Python package that integrates Kubernetes-native execution sandboxes with LangChain Deep Agents. It exposes a `KubernetesSandbox` class that implements the `BaseSandbox` protocol from `deepagents`, allowing agents to run shell commands and perform file operations inside ephemeral or persistent Kubernetes pods.

**Key concepts:**

- `KubernetesSandbox` — the main class; wraps the `k8s_agent_sandbox.SandboxClient`
- `create_kubernetes_sandbox` — module-level get-or-create factory keyed on `claim_name`; the production entry point for thread-scoped sandboxes
- **Handle mode** (`sandbox=` passed) vs **config-based mode** (`template_name=` passed) — decides who owns the Kubernetes resource lifecycle
- **Connection modes** — production (Gateway API), development (auto port-forward), advanced (direct `api_url`), in-cluster
- `proxy.py` — monkey-patches a bug in the kubernetes Python client's NO_PROXY handling

Public API is exactly `__all__ = ["KubernetesSandbox", "create_kubernetes_sandbox", "__version__"]`.

## Repository Layout

```
src/langchain_k8s/
  __init__.py        # Public API: KubernetesSandbox, create_kubernetes_sandbox, __version__
  sandbox.py         # KubernetesSandbox + create_kubernetes_sandbox — all the logic
  proxy.py           # k8s client NO_PROXY monkey-patch (quarantined infrastructure)
  _version.py        # Version constant — the single source of truth for the package version

tests/
  conftest.py        # Shared fixtures, make_mock_client(), FakeToolModel
  unit/              # Unit tests (no cluster required)
  integration/       # Integration tests (auto-skip without a Kind cluster)

k8s/                 # Kubernetes manifests for local development
scripts/             # kind-setup.sh / kind-teardown.sh

.github/workflows/
  publish.yml        # Publishes to PyPI on GitHub release
  ci.yml             # Runs unit tests on pull requests (linting, type check, tests)
  integration.yml    # Manually triggered integration tests against Kind cluster
```

## Architecture

**This package is a backend adapter, not an agent.** There is no custom Runnable, no LangChain Tool, no agent loop, and no client/server split here. `deepagents.create_deep_agent(backend=...)` owns the tools and the loop; `KubernetesSandbox` (`sandbox.py:45`) only supplies `execute()`, `upload_files()`, `download_files()` and the `id` property.

`BaseSandbox` synthesises `read`/`write`/`edit`/`ls`/`grep`/`glob` on top of the single `execute()` primitive by shipping base64-encoded `python3 -c` scripts into the container. `KubernetesSandbox` overrides those six only to apply path policy, then delegates via `super()`. Everything in `src/` is **synchronous** — `pytest-asyncio` and `asyncio_mode = "auto"` exist only because the agent tests await LangGraph.

Primary code path:

```
execute()  →  _ensure_sandbox()      →  _run()  →  _run_raw()
              lazy, double-checked                 sh -c wrap,
              under self._lock                     stdout+stderr merge,
                                                   max_output_size truncation
```

### Two constructor modes

Selected by `self._owns_lifecycle = sandbox is None` (`sandbox.py:303`):

| | Handle mode | Config-based mode |
| --- | --- | --- |
| Trigger | `sandbox=<k8s_agent_sandbox.Sandbox>` | `template_name=` and/or `claim_name=` |
| `_started` | `True` immediately | `False` until first use |
| `stop()` | closes the connection only | deletes the `SandboxClaim` (unless `skip_cleanup=True`) |
| Auto-reconnect | disabled | enabled when `reuse_sandbox=True` |

`create_kubernetes_sandbox()` returns a backend in handle mode.

### Connection-mode resolution

Strict precedence chain in `_create_client` (`sandbox.py:1073`):

`connection_config` → `api_url` (`SandboxDirectConnectionConfig`) → `gateway_name` (`SandboxGatewayConnectionConfig`) → default (`SandboxLocalTunnelConnectionConfig`, i.e. automatic `kubectl port-forward`).

Passing `connection_config` alongside `api_url` or `gateway_name` raises `ValueError` at construction. All configuration is constructor arguments — no env vars, no settings file.

### Kubernetes access

The library never writes YAML at runtime and normally never calls the Kubernetes API directly — `_ensure_sandbox` goes through `SandboxClient.create_sandbox()` / `get_sandbox()`. The one exception is `create_kubernetes_sandbox` (`sandbox.py:1174-1244`), which reaches past the SDK into `client.k8s_helper` to do a direct `get_namespaced_custom_object` on `sandboxclaims`. Two reasons, both load-bearing: the direct GET returns in ~20 ms versus the SDK's internal 30 s watch timeout, and `create_sandbox()` auto-generates claim names, which would break get-or-create. It handles 409 Conflict for concurrent callers and rolls back on ready-timeout.

The unit of identity is the `SandboxClaim` name. Persist `claim_name`, pass it to a fresh `KubernetesSandbox`, and `_ensure_sandbox` re-attaches instead of creating — that plus `skip_cleanup=True` is what survives an agent-process restart.

## Invariants and Gotchas

**Invariants — breaking these breaks the package:**

1. **`__init__` must never contact the cluster.** Enforced by `tests/unit/test_sandbox.py::TestConstruction::test_not_started_on_init`. All cluster work goes through `_ensure_sandbox()` under `self._lock`.
2. **`_resolve_virtual_path` (`sandbox.py:892`) must stay idempotent.** `BaseSandbox.write` internally calls `self.upload_files()`, and both layers resolve. Break the already-resolved short-circuit and you silently get `/tmp/tmp/src/main.py`.
3. **`allow_prefixes` is checked against the _resolved_ path**, after virtual-mode resolution — never the caller-supplied path.
4. **The `k8s_agent_sandbox` SDK is imported lazily inside function bodies**, plus `TYPE_CHECKING` at module scope. Keeps import cost down and keeps `test_imports.py` honest.

**Gotchas — surprising but intended:**

- **`reuse_sandbox=False` does not create ephemeral pods.** `self._reuse_sandbox` is read at exactly one place, `sandbox.py:467`, where it gates a single auto-reconnect-and-retry inside `execute()`. `start()`/`stop()` behave identically either way. Older prose in `README.md` and `openwiki/` claims per-invocation pod isolation — it does not exist.
- **`allow_prefixes` and `virtual_mode` are tool-level only.** `execute("echo bad > /etc/passwd")` bypasses both. Real containment needs the pod `securityContext`.
- **`allow_prefixes` normalisation appends a trailing slash**, so `["/tmp"]` permits `/tmp/x` but not the literal path `/tmp`.
- **Policy passing ≠ writable.** `write`/`edit` sniff for `PermissionError` and emit a specific warning. `/tmp` is the safe default; `/workspace` needs an explicit `emptyDir` mount in the `SandboxTemplate`.
- **Handle mode is one-shot.** After `stop()`, `_template_name` and `_claim_name` are `None`, so a later `execute()` trips `assert self._template_name is not None` (`sandbox.py:999`). The backend is not restartable.
- **`max_output_size` truncation applies to every `_run_raw` call**, including the internal JSON-emitting scripts that `BaseSandbox.read`/`ls`/`glob` depend on. A large listing truncated mid-JSON surfaces as a parse error, not a clean truncation.
- **`BaseSandbox.write` fails if the file already exists** — surprising for a method with that name.
- **`id` returns a construction-time UUID until a handle exists**, even when `claim_name` was supplied. Use the `claim_name` property for persistence.
- **`upload_files`/`download_files` deliberately bypass the SDK's native endpoints** (native `write()` only supports a fixed upload dir; `/download` is restricted to `/app`). Hence base64-over-shell. `_download_files_native` (`sandbox.py:828`) is currently unreachable — `download_files` always routes to `_download_files_shell`.
- **`labels` are creation-time only** and silently ignored on reconnect.
- **`shutdown_after_seconds` and `warmpool` are config-mode only**, and only forwarded when not `None` so older SDKs don't choke on unknown kwargs. `warmpool` is a `str` — the *name* of an existing `SandboxWarmPool` resource, not a config object.

## Quick Reference (Makefile)

Run `make` or `make help` to list all available targets. Common ones:

| Command                 | What it does                                       |
| ----------------------- | -------------------------------------------------- |
| `make install`          | Install all dependencies via `uv sync --all-groups` |
| `make check`            | Lint + format check + typecheck        |
| `make test-unit`        | Unit tests with coverage               |
| `make test-integration` | Integration tests (needs cluster)      |
| `make test`             | Both unit and integration tests        |
| `make cluster-up`       | Spin up local Kind cluster             |
| `make cluster-down`     | Tear down local Kind cluster           |
| `make build`            | Build distributable package            |
| `make clean`            | Remove build artefacts and caches      |

## Package Manager

This project uses **[uv](https://docs.astral.sh/uv/)** — not pip or poetry.

```bash
uv sync --all-groups     # Install all dependencies (including dev)
uv add <package>         # Add a runtime dependency
uv add --dev <package>   # Add a dev dependency
uv run <command>         # Run a command in the project venv
```

Dev dependencies live in a PEP 735 `[dependency-groups]` table, not `[project.optional-dependencies]` — hence `--all-groups`, which is what the Makefile and all three CI workflows use.

Do **not** use `pip install` or `pip freeze` directly.

## Running Tests

### Unit tests (no cluster needed)

```bash
uv run pytest tests/unit/ -v
```

### A single test

```bash
uv run pytest tests/unit/test_sandbox.py::TestExecute::test_execute_success -v   # one test
uv run pytest tests/unit/test_sandbox.py::TestExecute -v                         # one class
uv run pytest tests/unit/ -k "virtual_mode" -v                                   # by keyword
```

`testpaths = ["tests"]`, so a bare `uv run pytest` collects integration tests too. Scope to `tests/unit/` or pass `-m "not integration"`.

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

`make test-integration` runs `REUSE_CLUSTER=1 ./scripts/kind-setup.sh` for you first, so it is safe to invoke repeatedly.

Integration tests are marked via `pytestmark = pytest.mark.integration` at module scope in each integration file — there are no per-test decorators. They are excluded from unit test runs via marker filtering, and `tests/integration/conftest.py` additionally **auto-skips** them (`pytest_collection_modifyitems` shells out to `kind get clusters`) when the expected cluster is absent. So running them without a cluster skips rather than fails.

`addopts = "--strict-markers"`: `integration` is the only registered marker, and any unregistered one is a hard error.

## Linting and Type Checking

```bash
uv run ruff check src/ tests/     # Lint
uv run ruff format src/ tests/    # Format
uv run pyright src/               # Type check
```

Ruff is configured with rules: E, F, I, UP, B, SIM. Line length is 120 characters, target Python 3.11. Docstrings follow the Google convention.

`pyright` runs in **basic** mode (`typeCheckingMode = "basic"` in `[tool.pyright]`), with `reportOptionalMemberAccess`, `reportOptionalCall`, `reportOptionalIterable`, `reportOptionalSubscript`, `reportOptionalOperand` and `reportArgumentType` downgraded to warnings. There is **no mypy** in this project — no dependency, no config, never invoked. Ignore the stale `.mypy_cache/`.

## Code Conventions

- **Python 3.11+** — use modern type hints (`X | Y`, `list[X]`, etc.); every module starts with `from __future__ import annotations`
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

| File                           | What to update                                                  |
| ------------------------------ | --------------------------------------------------------------- |
| `pyproject.toml`               | `k8s-agent-sandbox>=X.Y.Z` version constraint                   |
| `k8s/sandbox-template.yaml`    | `python-runtime-sandbox` container image tag                    |
| `scripts/kind-setup.sh`        | `AGENT_SANDBOX_VERSION` default value                           |
| `src/langchain_k8s/sandbox.py` | Adapt to any SDK API changes (renamed methods, new parameters)  |
| `tests/conftest.py`            | Update mocks if SDK interfaces changed (e.g. method renames)    |
| `tests/unit/test_sandbox.py`   | Update tests for mock changes and new features                  |
| `README.md`                    | Document new connection modes, parameters, or behaviour changes |

After updating, run `uv sync` then `make check && make test-unit` to verify.

## Key Files to Read First

When investigating a bug or adding a feature, start here:

| File                           | Why                                                   |
| ------------------------------ | ----------------------------------------------------- |
| `src/langchain_k8s/sandbox.py` | Core logic — lifecycle, execution, file ops           |
| `tests/unit/test_sandbox.py`   | Existing unit test patterns and mock usage            |
| `tests/conftest.py`            | Shared fixtures, mock `SandboxClient` factory. Note it also mocks `client.k8s_helper`, because `create_kubernetes_sandbox` reaches through it |
| `src/langchain_k8s/proxy.py`   | Only touch if investigating proxy/k8s client issues   |
| `specs/plans/foundation.md`    | Original architectural decisions and design rationale. **Historical** — the v0.1 plan, much of it superseded. Read it for *why*, never for *what* |

## Environment Variables (Integration Tests)

Integration tests rely on a `kubectl` context pointing to a local Kind cluster named **`langchain-k8s`** (`CLUSTER_NAME` in `scripts/kind-setup.sh`, matched by `CLUSTER_NAME` in `tests/integration/conftest.py`). The setup script handles this:

```bash
./scripts/kind-setup.sh   # Creates cluster + installs controller + deploys manifests
```

Script knobs, all with defaults:

| Variable                 | Default         | Effect                                  |
| ------------------------ | --------------- | --------------------------------------- |
| `CLUSTER_NAME`           | `langchain-k8s` | Kind cluster name                       |
| `AGENT_SANDBOX_VERSION`  | `v0.4.6`        | Controller / CRD release to install     |
| `REUSE_CLUSTER=1`        | unset           | Reuse an existing cluster instead of recreating |
| `SKIP_CLUSTER=1`         | unset           | Skip cluster creation entirely          |

No secrets or credentials are required — not for unit tests, and not for integration tests either. The deep-agent tests use `FakeToolModel` rather than a real LLM, so no API keys are read anywhere in `src/` or `tests/`.

## CI Workflows

| Workflow          | Trigger                      | What it does                                                                              |
| ----------------- | ---------------------------- | ----------------------------------------------------------------------------------------- |
| `ci.yml`          | PR to `main`                 | Lint + format check + pyright; unit tests on Python 3.11 and 3.12                          |
| `integration.yml` | Manual (`workflow_dispatch`) | `python-version` choice input; installs Kind via `helm/kind-action` (`install_only: true`), runs `kind-setup.sh`, integration tests, teardown `if: always()` |
| `publish.yml`     | GitHub release published     | `uv build`, then PyPI trusted publishing, then attaches `dist/*` to the release           |

CI does not invoke `make` — it duplicates the raw commands. Keep the Makefile and the workflows in step when changing either.

## Do Not

- Do not call `pip install` — always use `uv`
- Do not add cluster-dependent code to unit tests — mock the `SandboxClient`
- Do not skip `pyright` — fix type errors properly
- Do not contact the Kubernetes API in `__init__` — initialization must remain lazy
- Do not modify `proxy.py` unless fixing the specific NO_PROXY bug it addresses
- Do not break the idempotency of `_resolve_virtual_path` — see [Invariants and Gotchas](#invariants-and-gotchas)
- Do not treat `README.md`, `openwiki/`, or `specs/plans/` as authoritative on mechanism — verify against `src/`
