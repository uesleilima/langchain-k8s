# langchain-k8s

Kubernetes Agent Sandbox integration for [LangChain Deep Agents](https://github.com/langchain-ai/deepagents).

Implements the `BaseSandbox` / `SandboxBackendProtocol` contract using [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) as the execution backend. Agents get isolated, ephemeral Kubernetes pods for running shell commands and file operations — fully self-hosted, no vendor lock-in.

## Installation

```bash
pip install langchain-k8s
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add langchain-k8s
```

### Prerequisites

- A Kubernetes cluster with the [agent-sandbox controller](https://github.com/kubernetes-sigs/agent-sandbox) installed
- A `SandboxTemplate` resource defining the pod spec for your sandboxes
- `kubectl` configured with cluster access

## Quick start

```python
from langchain_anthropic import ChatAnthropic
from deepagents import create_deep_agent
from langchain_k8s import KubernetesSandbox

backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
)

agent = create_deep_agent(
    model=ChatAnthropic(model="claude-sonnet-4-20250514"),
    system_prompt="You are a Python coding assistant with sandbox access.",
    backend=backend,
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "Create a Python script that prints the Fibonacci sequence"}]}
)

backend.stop()
```

## Usage

### Context manager (recommended)

```python
with KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
) as backend:
    agent = create_deep_agent(model=model, backend=backend, ...)
    result = agent.invoke({"messages": [...]})
# Sandbox pod is automatically cleaned up
```

### Explicit lifecycle

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
)
backend.start()
try:
    resp = backend.execute("python3 --version")
    print(resp.output)
finally:
    backend.stop()
```

### Direct execution

```python
with KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
) as backend:
    # Execute commands
    resp = backend.execute("echo 'Hello from K8s!'")
    print(resp.output, resp.exit_code)

    # Upload files
    backend.upload_files([("/workspace/script.py", b"print('hello')\n")])

    # Download files
    results = backend.download_files(["/workspace/script.py"])
    print(results[0].content)
```

## Connection modes

| Mode | Configuration | Use case |
|------|--------------|----------|
| **Production** | `gateway_name="my-gateway"` | Cluster with Gateway API |
| **Development** | *(default — no gateway, no api_url)* | Auto `kubectl port-forward` |
| **Advanced** | `api_url="http://localhost:8080"` | Pre-existing port-forward or in-cluster |

```python
# Production — cluster Gateway
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    gateway_name="sandbox-gateway",
)

# Development — automatic port-forward
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
)

# Advanced — existing port-forward or in-cluster routing
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    api_url="http://localhost:8080",
)
```

## Sandbox lifecycle strategies

Control how sandboxes are managed across multiple agent invocations via the `reuse_sandbox` parameter:

### Persistent (default)

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    reuse_sandbox=True,  # default
)
```

One sandbox pod is created lazily and reused across all calls. Fast for cached, long-lived agents. Filesystem state persists between invocations. Auto-reconnects if the pod dies.

### Ephemeral

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    reuse_sandbox=False,
)
```

A fresh sandbox is created for each `start()`/`stop()` cycle. Maximum isolation between invocations at the cost of cold-start latency.

## Configuration reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `template_name` | `str` | *(required)* | `SandboxTemplate` CRD name |
| `namespace` | `str` | `"default"` | Kubernetes namespace |
| `gateway_name` | `str \| None` | `None` | Gateway name (production mode) |
| `gateway_namespace` | `str` | `"default"` | Gateway namespace |
| `api_url` | `str \| None` | `None` | Direct router URL (advanced mode) |
| `server_port` | `int` | `8888` | Sandbox runtime port |
| `reuse_sandbox` | `bool` | `True` | Reuse sandbox across calls |
| `max_output_size` | `int` | `1048576` | Max output bytes before truncation |
| `command_timeout` | `int` | `300` | Command timeout in seconds |

## Development

```bash
# Clone and install
git clone https://github.com/uesleilima/langchain-k8s.git
cd langchain-k8s
uv sync

# Run unit tests (no cluster needed)
uv run pytest tests/unit/ -v

# Lint and type check
uv run ruff check src/ tests/
uv run mypy src/

# Integration tests (requires Kind cluster with agent-sandbox)
uv run pytest tests/integration/ -v -m integration
```

## License

MIT
