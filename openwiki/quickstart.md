# OpenWiki: langchain-k8s

> **These pages are generated** (see `.last-update.json`), then hand-corrected. Use them as a map of the territory. Before relying on a specific mechanism, attribute name, method signature, or default value, confirm it against `src/langchain_k8s/sandbox.py` — the source is authoritative. Note also that re-running the generator will overwrite corrections made here.

## What is langchain-k8s?

**langchain-k8s** is a Python package that bridges LangChain Deep Agents with [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox), enabling LangChain agents to run shell commands and file operations inside isolated Kubernetes pods.

**Key features:**

- **Isolated execution** — Each agent gets its own pod with its own filesystem
- **Self-hosted** — Runs on any Kubernetes cluster you control; no vendor lock-in
- **Disposable or durable** — Tear a pod down at the end of a task, or keep it alive and reconnect to it across turns
- **Drop-in compatible** — Implements the LangChain `BaseSandbox` protocol
- **Enterprise-ready** — Path policies, virtual filesystems, sticky sessions, reconnection

## For Humans: Quick Start

### Installation

```bash
pip install langchain-k8s
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add langchain-k8s
```

### Setup (Prerequisites)

1. **Kubernetes cluster** with the [agent-sandbox controller](https://github.com/kubernetes-sigs/agent-sandbox) installed
2. **`SandboxTemplate` resource** defining the pod spec for your sandboxes
3. **`kubectl` configured** with cluster access

### First Agent

```python
from langchain_k8s import KubernetesSandbox
from langchain_anthropic import ChatAnthropic
from deepagents import create_deep_agent

# Create backend (config-based mode — simplest)
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
)

# Create agent
agent = create_deep_agent(
    model=ChatAnthropic(model="claude-sonnet-4-20250514"),
    backend=backend,
)

# Invoke
result = agent.invoke({
    "messages": [{"role": "user", "content": "Write a Python script that prints the Fibonacci sequence"}]
})

# Cleanup
backend.stop()
```

Or use context manager:

```python
with backend:
    result = agent.invoke({...})
# Automatically cleaned up
```

**What happens inside:**

1. On first `invoke()`, KubernetesSandbox lazily creates a Kubernetes pod
2. Agent can run shell commands and read/write files inside the pod
3. Filesystem state persists between agent turns (by default)
4. On `stop()`, the pod is deleted

## Key Concepts

| Concept                  | What it does                                                          | See                                                        |
| ------------------------ | --------------------------------------------------------------------- | ---------------------------------------------------------- |
| **KubernetesSandbox**    | Main class implementing LangChain's `BaseSandbox` protocol            | [Architecture](architecture/design.md)                     |
| **Connection modes**     | How the agent reaches the sandbox (Gateway, port-forward, direct API) | [Workflows: Connection](workflows/connection.md)           |
| **Lifecycle strategies** | Pod lifetime, auto-reconnect, thread-scoped sandboxes                 | [Workflows: Lifecycle](workflows/lifecycle.md)             |
| **Enterprise features**  | Path policies, virtual filesystem, sticky sessions                    | [Architecture: Enterprise](architecture/enterprise.md)     |
| **k8s-agent-sandbox**    | The Kubernetes backend SDK we wrap                                    | [Integrations: Dependencies](integrations/dependencies.md) |

## Repository Overview

```
langchain-k8s/
├── src/langchain_k8s/
│   ├── __init__.py              Public API: KubernetesSandbox, create_kubernetes_sandbox
│   ├── sandbox.py               Core implementation
│   ├── proxy.py                 Kubernetes client NO_PROXY monkey-patch (infrastructure only)
│   └── _version.py              Version constant
│
├── tests/
│   ├── conftest.py              Shared fixtures, mock SandboxClient factory
│   ├── unit/
│   │   ├── test_sandbox.py      Comprehensive unit tests (mocked)
│   │   ├── test_agent.py        Agent integration tests (mocked)
│   │   ├── test_proxy.py        Proxy patch tests
│   │   └── test_imports.py      Smoke test
│   └── integration/
│       ├── test_kind.py         Integration tests (requires Kind cluster)
│       ├── test_agent_kind.py   Agent integration tests
│       └── test_deepagent_kind.py DeepAgent integration tests
│
├── k8s/
│   ├── sandbox-template.yaml    Example SandboxTemplate for local dev
│   └── sandbox-router.yaml      Example Gateway API resource
│
├── scripts/
│   ├── kind-setup.sh            Create local Kind cluster + controller + manifests
│   └── kind-teardown.sh         Tear down local Kind cluster
│
├── specs/plans/
│   ├── foundation.md            Original architecture + lifecycle design
│   └── enterprise-features.md   Enterprise features (policies, virtual FS)
│
├── README.md                    Public-facing user documentation
├── AGENTS.md                    Agent guide (this repo has one)
├── pyproject.toml               uv-managed dependencies
├── Makefile                     Development targets
└── openwiki/                    This documentation (you are here)
```

## For Agents: Repository Structure

**If you are a coding agent working in this repo:**

### Where to Start

1. **Understand the architecture**: Read [Architecture: Design](architecture/design.md) and [Architecture: Enterprise](architecture/enterprise.md)
2. **Understand the lifecycle**: Read [Workflows: Lifecycle](workflows/lifecycle.md)
3. **Look at tests**: Unit tests in `tests/unit/test_sandbox.py` show expected behavior
4. **Understand the dependencies**: Read [Integrations: Dependencies](integrations/dependencies.md) and [Integrations: Upgrading](integrations/upgrading.md)

### Making Changes

**Adding a feature to `KubernetesSandbox`:**

1. Update `src/langchain_k8s/sandbox.py` (the core class)
2. Add unit test in `tests/unit/test_sandbox.py` (use mock client)
3. If it involves Kubernetes interaction, add integration test in `tests/integration/test_kind.py`
4. Export new public symbols from `src/langchain_k8s/__init__.py`
5. Update `README.md` if behavior or config options change
6. Run `make check && make test-unit` to verify

**Upgrading the k8s-agent-sandbox dependency:**

1. Bump version in `pyproject.toml`
2. Update container image tag in `k8s/sandbox-template.yaml`
3. Update `AGENT_SANDBOX_VERSION` in `scripts/kind-setup.sh`
4. Adapt to any SDK API changes in `src/langchain_k8s/sandbox.py` and `tests/conftest.py`
5. Update tests and `README.md` as needed
6. Run full test suite

See [Integrations: Upgrading](integrations/upgrading.md) for detailed upgrade checklist.

### Key Principles

- **Lazy initialization**: Never contact the Kubernetes cluster in `__init__`; use `threading.Lock` to defer to first use
- **Thread-safe**: Use locks for state mutations to support multi-threaded agent environments
- **Policy over permission**: Tool-level policies (`allow_prefixes`, `virtual_mode`) enforce agent constraints before commands reach the container
- **Base64 file I/O**: File upload/download uses base64 shell commands to avoid SDK limitations
- **Shell wrapping**: Commands wrapped in `sh -c '...'` for full POSIX semantics
- **Auto-reconnect**: With `reuse_sandbox=True` (default) and config-based mode, `execute()` retries once on connection error before propagating. The flag controls only this — it does not change pod lifetime
- **Error classification**: `_classify_error(output)` maps stderr patterns to `FileOperationError` types

## Development Workflow

### Install and Setup

```bash
# Install dependencies with uv
uv sync --all-groups

# Spin up a local Kind cluster (for integration tests)
./scripts/kind-setup.sh

# Run linting, type check, and unit tests
make check && make test-unit

# Run integration tests
make test-integration

# Tear down Kind cluster when done
./scripts/kind-teardown.sh
```

### Common Tasks

| Task                  | Command                         |
| --------------------- | ------------------------------- |
| Install               | `uv sync --all-groups` or `make install`     |
| Lint + format         | `make check`                    |
| Type check            | `make check` (includes pyright) |
| Unit tests            | `make test-unit`                |
| Integration tests     | `make test-integration`         |
| All tests             | `make test`                     |
| Build package         | `make build`                    |
| Clean build artifacts | `make clean`                    |
| Cluster setup         | `./scripts/kind-setup.sh`       |
| Cluster teardown      | `./scripts/kind-teardown.sh`    |

See `Makefile` for all targets (run `make help` or `make`).

## Documentation Map

- **[Architecture: Design](architecture/design.md)** — `KubernetesSandbox` class design, sandbox lifecycle strategies, error handling, file operations
- **[Architecture: Enterprise](architecture/enterprise.md)** — Path access policies, virtual filesystem, horizontal scaling, sticky sessions
- **[Workflows: Connection](workflows/connection.md)** — Connection modes (Production/Gateway, Development/port-forward, Advanced/direct API, InCluster)
- **[Workflows: Lifecycle](workflows/lifecycle.md)** — Pod lifetime, auto-reconnect, thread-scoped patterns, sandbox reuse
- **[Operations: Testing](operations/testing.md)** — Unit test patterns, integration test setup, mocking the SDK, running tests
- **[Operations: Deployment](operations/deployment.md)** — Local Kind setup, production cluster setup, example manifests
- **[Integrations: Dependencies](integrations/dependencies.md)** — Runtime and dev dependencies, Python version support, k8s-agent-sandbox SDK
- **[Integrations: Upgrading](integrations/upgrading.md)** — Dependency upgrade checklist, breaking changes, compatibility

## Common Questions

### How do I run agents on my Kubernetes cluster?

1. Install the [agent-sandbox controller](https://github.com/kubernetes-sigs/agent-sandbox)
2. Create a `SandboxTemplate` CRD defining your pod spec
3. Use config-based mode: `KubernetesSandbox(template_name="...", namespace="...")`
4. Create a deep agent with this backend

See [Workflows: Connection](workflows/connection.md) for connection options.

### How do I scale agents horizontally?

Use sticky sessions to ensure requests from the same user/session route to the same service instance (sandbox state is in-process). See [Architecture: Enterprise](architecture/enterprise.md) for configuration.

### How do I restrict which directories agents can write to?

Use `allow_prefixes` to enforce a tool-level write policy. See [Architecture: Enterprise](architecture/enterprise.md).

### How do I make agents use a virtual filesystem?

Use `virtual_mode=True` and `root_dir` to anchor all agent paths under a directory. See [Architecture: Enterprise](architecture/enterprise.md).

### How do I run integration tests?

See [Operations: Testing](operations/testing.md).

### How do I upgrade k8s-agent-sandbox?

See [Integrations: Upgrading](integrations/upgrading.md).

## External Links

- **LangChain Deep Agents**: https://github.com/langchain-ai/deepagents
- **kubernetes-sigs/agent-sandbox**: https://github.com/kubernetes-sigs/agent-sandbox
- **k8s-agent-sandbox SDK**: https://pypi.org/project/k8s-agent-sandbox/
- **Kind (local Kubernetes)**: https://kind.sigs.k8s.io/
- **This project on PyPI**: https://pypi.org/project/langchain-k8s/
- **This project on GitHub**: https://github.com/uesleilima/langchain-k8s
