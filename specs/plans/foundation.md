# Plan: `langchain-k8s` — LangChain Sandbox Backend for Kubernetes Agent Sandbox

## Context

LangChain deepagents provides a `BaseSandbox` / `SandboxBackendProtocol` abstraction for sandbox backends. Official integrations exist for Modal, Runloop, and Daytona — all commercial SaaS providers. There is no integration for the open-source **kubernetes-sigs/agent-sandbox** project, which provides Kubernetes-native sandbox infrastructure via CRDs (`Sandbox`, `SandboxTemplate`, `SandboxClaim`, `SandboxWarmPool`).

This plan creates `langchain-k8s`, a public PyPI package that bridges LangChain deepagents with kubernetes-sigs/agent-sandbox, enabling anyone to run LangChain agents with isolated Kubernetes sandbox execution — no vendor lock-in, fully self-hosted.

---

## 1. Package Structure

```
langchain-k8s/
├── .gitignore                          # (exists)
├── LICENSE                             # (exists, MIT)
├── README.md
├── pyproject.toml                      # UV-managed, PyPI-ready
├── src/
│   └── langchain_k8s/
│       ├── __init__.py                 # Public API: KubernetesSandbox
│       ├── sandbox.py                  # KubernetesSandbox class (BaseSandbox impl)
│       └── _version.py                 # Version string
└── tests/
    ├── __init__.py
    ├── conftest.py                     # Shared fixtures
    ├── unit/
    │   ├── __init__.py
    │   ├── test_imports.py             # Smoke test imports
    │   └── test_sandbox.py             # Unit tests with mocked SDK
    └── integration/
        ├── __init__.py
        └── test_kind.py               # Integration tests against Kind cluster
```

**Naming conventions** (following LangChain partner packages):

- **PyPI package**: `langchain-k8s`
- **Python module**: `langchain_k8s`
- **Class**: `KubernetesSandbox`
- **Source file**: `sandbox.py`

---

## 2. `KubernetesSandbox` Class Design

### Constructor

```python
class KubernetesSandbox(BaseSandbox):
    def __init__(
        self,
        *,
        template_name: str,
        namespace: str = "default",
        gateway_name: str | None = None,
        api_url: str | None = None,
        server_port: int | None = None,
        reuse_sandbox: bool = True,
        max_output_size: int = 1_048_576,  # 1MB
    ) -> None:
```

- Stores configuration, does NOT create the sandbox yet (lazy initialization)
- All connection modes supported: Production (gateway), Dev (tunnel), Advanced (api_url)
- `reuse_sandbox` controls the lifecycle strategy (see below)
- `max_output_size` controls output truncation threshold

### Lifecycle Design — Configurable Strategy

**Problem**: `SandboxClient` is a context manager (creates sandbox on enter, destroys on exit). But `BaseSandbox` is used as a long-lived backend object passed to `create_deep_agent()`. The agent may be cached and invoked multiple times from different modules.

**Solution**: Two strategies controlled by `reuse_sandbox` parameter:

#### Strategy 1: Persistent Sandbox (`reuse_sandbox=True`, default)

One sandbox stays alive across multiple `invoke()` calls. The backend auto-creates on first use and reuses for all subsequent calls.

```python
# Created once, cached, used many times
backend = KubernetesSandbox(
    template_name="python-sandbox",
    namespace="agent-sandbox-system",
    reuse_sandbox=True,  # default
)
agent = create_deep_agent(model=model, backend=backend, ...)

# Multiple invocations share the same sandbox pod
result1 = agent.invoke({"messages": [...]})  # creates sandbox on first call
result2 = agent.invoke({"messages": [...]})  # reuses same sandbox
result3 = agent.invoke({"messages": [...]})  # reuses same sandbox

backend.stop()  # explicit cleanup when done
```

- Sandbox created lazily on first `execute()` call
- Same pod reused for all subsequent calls — fast, no cold-start
- Filesystem state persists between invocations (shared workspace)
- Thread-safe via `threading.Lock`
- Auto-reconnects if sandbox pod dies (catches connection errors, recreates)
- Must call `stop()` or use context manager for cleanup

#### Strategy 2: Sandbox-per-invocation (`reuse_sandbox=False`)

Each `execute()` sequence gets a fresh, isolated sandbox. Sandboxes are created on first `execute()` and destroyed after an idle timeout.

```python
backend = KubernetesSandbox(
    template_name="python-sandbox",
    namespace="agent-sandbox-system",
    reuse_sandbox=False,
)
agent = create_deep_agent(model=model, backend=backend, ...)

# Each invocation gets a fresh sandbox
result1 = agent.invoke({"messages": [...]})  # creates sandbox A
# sandbox A destroyed after idle timeout
result2 = agent.invoke({"messages": [...]})  # creates sandbox B
```

- Maximum isolation between invocations
- Higher latency (cold-start per invocation)
- Clean filesystem each time
- Internally: tracks "active session" via a session token; when `execute()` is called with no active session, creates a new sandbox; a background cleanup timer destroys idle sandboxes after configurable timeout

#### Context Manager (works with both strategies)

```python
with KubernetesSandbox(template_name="python-sandbox", namespace="ns") as backend:
    agent = create_deep_agent(model=model, backend=backend, ...)
    result = agent.invoke(...)
# Guaranteed cleanup on exit
```

#### Explicit Lifecycle

```python
backend = KubernetesSandbox(template_name="python-sandbox", namespace="ns")
backend.start()   # eagerly create sandbox (optional, otherwise lazy)
# ... use backend ...
backend.stop()    # destroy sandbox, release resources
```

### Internal Architecture

```python
class KubernetesSandbox(BaseSandbox):
    _client: SandboxClient | None    # Active SDK client (None until started)
    _sandbox: Any | None             # Active sandbox context (None until started)
    _lock: threading.Lock            # Thread-safety for lazy init
    _started: bool                   # Whether sandbox is active

    def _ensure_sandbox(self) -> None:
        """Create sandbox if not already running. Thread-safe."""
        with self._lock:
            if not self._started:
                self._client = SandboxClient(
                    template_name=self._template_name,
                    namespace=self._namespace,
                    gateway_name=self._gateway_name,
                    api_url=self._api_url,
                    server_port=self._server_port,
                )
                self._sandbox = self._client.__enter__()
                self._started = True

    def _destroy_sandbox(self) -> None:
        """Destroy current sandbox. Thread-safe."""
        with self._lock:
            if self._started and self._client is not None:
                self._client.__exit__(None, None, None)
                self._client = None
                self._sandbox = None
                self._started = False
```

### Core Methods

**`id` property:**

- Returns the sandbox's unique identifier (from `SandboxClient` attributes, or a generated UUID as fallback)

**`execute(command: str) -> ExecuteResponse`:**

1. Calls `_ensure_sandbox()` (lazy init)
2. Calls `self._sandbox.run(command)`
3. Combines stdout + stderr into `output`
4. If output exceeds `max_output_size`, truncates and sets `truncated=True`
5. Returns `ExecuteResponse(output=..., exit_code=..., truncated=...)`
6. On connection error with `reuse_sandbox=True`: destroys sandbox, recreates, retries once

**`upload_files(files: list[tuple[str, bytes]]) -> list[FileUploadResponse]`:**

- For each `(path, content)`: validates path starts with `/`, then runs `mkdir -p $(dirname path) && printf '%s' '<base64>' | base64 -d > path` via `execute()`
- Returns per-file `FileUploadResponse` with error mapping

**`download_files(paths: list[str]) -> list[FileDownloadResponse]`:**

- For each path: runs `base64 <path>` via `execute()`, decodes result
- Returns per-file `FileDownloadResponse` with content or error

### Error Handling

- Exit code from cat/base64 indicating missing file → `"file_not_found"`
- "Permission denied" in stderr → `"permission_denied"`
- "Is a directory" in stderr → `"is_directory"`
- Invalid/empty paths → `"invalid_path"`
- SDK connection errors with `reuse_sandbox=True` → auto-reconnect once, then raise
- SDK connection errors with `reuse_sandbox=False` → raise immediately

---

## 3. `pyproject.toml`

```toml
[project]
name = "langchain-k8s"
version = "0.1.0"
description = "Kubernetes Agent Sandbox integration for LangChain Deep Agents"
readme = "README.md"
license = "MIT"
requires-python = ">=3.11,<4.0"
authors = [{ name = "Ueslei Lima" }]
keywords = ["langchain", "kubernetes", "sandbox", "agent", "deepagents"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Topic :: Software Development :: Libraries",
]
dependencies = [
    "deepagents>=0.1.0",
    "k8s-agent-sandbox>=0.1.0",
]

[project.urls]
Homepage = "https://github.com/uesleilima/langchain-k8s"
Repository = "https://github.com/uesleilima/langchain-k8s"
Issues = "https://github.com/uesleilima/langchain-k8s/issues"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/langchain_k8s"]

[dependency-groups]
dev = [
    "pytest>=7.3.0,<9.0.0",
    "pytest-cov>=4.0.0",
    "pytest-asyncio>=1.3.0",
    "pytest-timeout>=2.3.1,<3.0.0",
    "ruff>=0.9.0",
    "mypy>=1.10.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
strict_markers = true
testpaths = ["tests"]
markers = [
    "integration: marks tests requiring a Kind cluster (deselect with '-m \"not integration\"')",
]

[tool.ruff]
line-length = 120
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.11"
strict = true
```

---

## 4. Test Strategy

### Unit Tests (`tests/unit/`)

**`test_imports.py`** — Smoke test that the package imports correctly.

**`test_sandbox.py`** — Core logic tests with mocked `SandboxClient`:

- Test `execute()` maps stdout/stderr/exit_code to `ExecuteResponse`
- Test `execute()` truncates large output and sets `truncated=True`
- Test `upload_files()` base64-encodes content and writes via execute
- Test `download_files()` reads and base64-decodes via execute
- Test error mapping (file not found, permission denied, is_directory, invalid_path)
- Test lazy initialization (sandbox starts on first execute)
- Test `start()`/`stop()` explicit lifecycle
- Test context manager protocol (`__enter__`/`__exit__`)
- Test `id` property
- Test `execute()` after `stop()` raises error
- Test `reuse_sandbox=True` reuses same client across calls
- Test `reuse_sandbox=False` creates new client per session
- Test auto-reconnect on connection error (`reuse_sandbox=True`)
- Test thread-safety of lazy init

### Integration Tests (`tests/integration/`)

**`test_kind.py`** — End-to-end tests against a real Kind cluster:

- Marked with `@pytest.mark.integration`
- Prerequisites: Kind cluster, agent-sandbox controller, sandbox-router, python-runtime-sandbox image
- Tests:
  - Create sandbox, execute simple command, verify output
  - Execute Python code in sandbox
  - Upload file via `upload_files()`, verify via `execute("cat ...")`
  - Download file via `download_files()`, verify content
  - Multi-command chaining (&&, pipes)
  - Sandbox cleanup on context exit (verify pod deleted)
  - Error handling for failed commands (non-zero exit code)
  - `reuse_sandbox=True`: verify same pod across multiple execute() calls
  - `reuse_sandbox=False`: verify different pods for sequential sessions

### Running Tests

```bash
# Unit tests only (no cluster needed)
uv run pytest tests/unit/ -v

# Integration tests (requires Kind cluster with agent-sandbox)
uv run pytest tests/integration/ -v -m integration

# All tests with coverage
uv run pytest --cov=langchain_k8s --cov-report=term-missing
```

---

## 5. Files to Create

| #   | File                             | Purpose                                   |
| --- | -------------------------------- | ----------------------------------------- |
| 1   | `pyproject.toml`                 | Package metadata, deps, tooling config    |
| 2   | `src/langchain_k8s/_version.py`  | Version string                            |
| 3   | `src/langchain_k8s/sandbox.py`   | `KubernetesSandbox` — core implementation |
| 4   | `src/langchain_k8s/__init__.py`  | Public API exports                        |
| 5   | `tests/__init__.py`              | Test package                              |
| 6   | `tests/conftest.py`              | Shared fixtures (mock SandboxClient)      |
| 7   | `tests/unit/__init__.py`         | Unit test package                         |
| 8   | `tests/unit/test_imports.py`     | Import smoke tests                        |
| 9   | `tests/unit/test_sandbox.py`     | Unit tests with mocks                     |
| 10  | `tests/integration/__init__.py`  | Integration test package                  |
| 11  | `tests/integration/test_kind.py` | Kind cluster integration tests            |
| 12  | `README.md`                      | Docs: installation, usage, examples       |

---

## 6. Implementation Order

1. **`pyproject.toml`** — Package metadata and dependencies
2. **`src/langchain_k8s/_version.py`** — Version constant
3. **`src/langchain_k8s/sandbox.py`** — Core `KubernetesSandbox` implementation
4. **`src/langchain_k8s/__init__.py`** — Public exports
5. **`tests/` structure** — conftest, unit tests, integration tests
6. **`README.md`** — Documentation with usage examples
7. **Verify** — `uv sync`, `uv run pytest tests/unit/`, `uv run ruff check`, `uv run mypy`

---

## 7. Verification

```bash
# Install dependencies
uv sync

# Lint & type check
uv run ruff check src/ tests/
uv run mypy src/

# Unit tests (no cluster required)
uv run pytest tests/unit/ -v

# Integration tests (requires Kind cluster with agent-sandbox)
# Prerequisites:
#   kind create cluster
#   kubectl apply -f <agent-sandbox controller manifests>
#   kubectl apply -f <sandbox-router deployment>
#   kind load docker-image python-runtime-sandbox:latest
#   kubectl apply -f <python-sandbox-template>
uv run pytest tests/integration/ -v -m integration
```
