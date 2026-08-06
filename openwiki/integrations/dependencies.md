# Integrations: Dependencies

This document describes the runtime and development dependencies, Python version support, and key external projects that langchain-k8s integrates with.

**Source:** `pyproject.toml`, `README.md`

## Runtime Dependencies

### `deepagents` (≥0.5.3)

**Purpose:** Defines the `BaseSandbox` protocol and `SandboxBackendProtocol` that `KubernetesSandbox` implements.

**What it provides:**

- `BaseSandbox` — Abstract base class with default implementations of all sandbox tools
- `SandboxBackendProtocol` — Type protocol for sandbox backends
- `FileOperationError` and error types — For classifying file operation failures
- `create_deep_agent()` — Factory for creating agents with sandboxes

**Dependency range:** `>=0.5.3`

- Allows patches and minor version bumps
- Breaking changes would require major version bump

**When to upgrade:** When LangChain releases new sandbox features or fixes bugs in the protocol.

**Related links:**

- [LangChain DeepAgents repo](https://github.com/langchain-ai/deepagents)
- [PyPI: deepagents](https://pypi.org/project/deepagents/)

### `k8s-agent-sandbox` (≥0.4.6)

**Purpose:** The Python SDK for the Kubernetes agent-sandbox controller. Provides the `SandboxClient` for creating/managing sandboxes.

**What it provides:**

- `SandboxClient` — Main client for sandbox lifecycle
- `Sandbox` — Handle to a created sandbox pod
- `SandboxConnectionConfig`, `SandboxGatewayConnectionConfig`, `SandboxLocalTunnelConnectionConfig`, `SandboxInClusterConnectionConfig` — Connection modes
- `WarmPool` — Warm pool configuration
- `ExecutionResult` — Command execution results

**Dependency range:** `>=0.4.6`

- Allows patches and minor version bumps
- Minor version bumps may add new features (new parameters, methods)
- Major version bumps (0.5.0) would be breaking

**When to upgrade:** When a new release adds features (e.g., warm pools), fixes bugs, or improves performance.

**Upgrade complexity:** Moderate

- May require adapting to new SDK API (method renames, parameter names)
- See [Integrations: Upgrading](upgrading.md)

**Related links:**

- [kubernetes-sigs/agent-sandbox repo](https://github.com/kubernetes-sigs/agent-sandbox)
- [PyPI: k8s-agent-sandbox](https://pypi.org/project/k8s-agent-sandbox/)
- [Upgrade notes](upgrading.md)

## Development Dependencies

### Testing

| Package          | Version        | Purpose                  |
| ---------------- | -------------- | ------------------------ |
| `pytest`         | ≥7.3.0, <9.0.0 | Test runner              |
| `pytest-cov`     | ≥4.0.0         | Coverage reporting       |
| `pytest-asyncio` | ≥1.3.0         | Async test support       |
| `pytest-timeout` | ≥2.3.1, <3.0.0 | Test timeout enforcement |

**Why these versions?**

- `pytest` 7.3+ for modern features, <9.0 to avoid major breaks
- `pytest-asyncio` 1.3+ for reliable async fixtures
- `pytest-timeout` for integration tests (prevent hangs)

### Linting & Type Checking

| Package   | Version | Purpose                                            |
| --------- | ------- | -------------------------------------------------- |
| `ruff`    | ≥0.9.0  | Linter + formatter (replaces flake8, isort, black) |
| `pyright` | ≥1.1.0  | Type checker (strict mode)                         |

**Configuration:**

- `ruff`: Line length 120, rules E, F, I, UP, B, SIM (see `pyproject.toml`)
- `pyright`: Strict mode, Python 3.11 target (see `pyproject.toml`)

## Python Version Support

**Supported versions:** 3.11, 3.12, 3.13

```toml
requires-python = ">=3.11,<4.0"
```

**Why 3.11+?**

- Python 3.11 is the minimum version with modern type hint syntax (`X | Y`, `list[X]`)
- Allows clean type annotations without `from __future__ import annotations`
- LangChain ecosystem also targets 3.11+

**Testing:** CI tests on Python 3.11 and 3.12 (3.13 supported but not tested in CI)

## Related Projects

### LangChain Ecosystem

**LangChain Core:**

- Used by `deepagents` for chat models and tools
- No direct dependency in langchain-k8s (via deepagents)

**LangChain CLI / LangServe:**

- Deploy agents via `langchain-cli serve` or LangServe
- Works seamlessly with `KubernetesSandbox`

**LangGraph:**

- Build agentic workflows with LangGraph
- `KubernetesSandbox` is compatible with LangGraph graph factories

### Kubernetes Ecosystem

**kubernetes-sigs/agent-sandbox:**

- The open-source controller we wrap
- Provides CRDs: `Sandbox`, `SandboxTemplate`, `SandboxClaim`, `SandboxWarmPool`
- Handles pod lifecycle, tunneling, service creation

**Gateway API:**

- Optional: For production connectivity
- Provides routable endpoints for sandboxes
- Alternative to port-forward

**Kind (Kubernetes in Docker):**

- Used for local development and integration tests
- Not a hard dependency; any Kubernetes cluster works

### LangChain Partner Packages

langchain-k8s follows the same design patterns as other LangChain partner packages:

| Package              | What it does                       |
| -------------------- | ---------------------------------- |
| langchain-anthropic  | Anthropic LLM integration          |
| langchain-openai     | OpenAI LLM integration             |
| langchain-databricks | Databricks SQL integration         |
| langchain-k8s        | **Kubernetes sandbox integration** |

All follow:

- `BaseSandbox` protocol implementation
- PyPI naming: `langchain-<provider>`
- Source module naming: `langchain_<provider>`
- Same test structure and CI patterns

## Dependency Tree

```
langchain-k8s
├── deepagents >=0.5.3
│   ├── langchain-core
│   ├── pydantic
│   └── ...
└── k8s-agent-sandbox >=0.4.6
    ├── kubernetes (Python client)
    ├── grpcio
    └── ...

[dev]
├── pytest >=7.3.0
├── ruff >=0.9.0
├── pyright >=1.1.0
└── ...
```

## Security Considerations

### No Credentials in Dependencies

- langchain-k8s does NOT require API keys or secrets to function
- SDK credentials are passed by the user (Kubernetes auth via kubeconfig, Gateway credentials, etc.)
- Safe to include in production deployments

### Kubernetes Client Credentials

The `kubernetes` package (pulled by `k8s-agent-sandbox`) uses standard Kubernetes auth:

- In-cluster: Service account token (automatic)
- Out-of-cluster: kubeconfig file (usually `~/.kube/config`)

**Security note:** kubeconfig often contains sensitive credentials. Never commit to version control.

## Compatibility

### With Other LangChain Backends

`KubernetesSandbox` is compatible with other `BaseSandbox` implementations:

```python
from langchain_k8s import KubernetesSandbox
from langchain_runloop import RunloopSandbox  # hypothetical

# Can switch backends by changing this line
backend = KubernetesSandbox(...)
# backend = RunloopSandbox(...)

agent = create_deep_agent(model=model, backend=backend)
```

### With LangGraph

Works seamlessly with LangGraph state graphs and entrypoints.

### With LangServe

Works seamlessly with LangServe HTTP endpoints.

## Updating Dependencies

### Check for Updates

```bash
# See what's available
uv pip list --outdated

# Or use pip-audit for security updates
pip-audit
```

### Adding a New Dependency

```bash
# Add a runtime dependency
uv add requests

# Add a dev dependency
uv add --dev mypy
```

This updates `pyproject.toml` and `uv.lock`.

### Updating Existing Dependency

```bash
# Update to latest
uv add requests@latest

# Update to specific version
uv add requests@2.31.0
```

### Removing a Dependency

```bash
uv remove requests
```

## License

langchain-k8s is MIT licensed. All dependencies should be compatible with MIT (no GPL or restrictive licenses).

**Dependency licenses:**

- `deepagents` — MIT (LangChain)
- `k8s-agent-sandbox` — Apache 2.0 (Kubernetes)
- `kubernetes` (via k8s-agent-sandbox) — Apache 2.0
- `ruff`, `pyright` — MIT (dev only)

## Further Reading

- [Integrations: Upgrading](upgrading.md) — Detailed upgrade procedures
- `pyproject.toml` — Authoritative dependency declarations
- `README.md` — Installation and setup instructions
