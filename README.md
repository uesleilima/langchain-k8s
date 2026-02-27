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

## Enterprise features

### Path access policy

Restrict which directories agents can write to using `allow_prefixes`. When set, only `write()` and `edit()` operations targeting paths under the specified prefixes are permitted. All other paths return an error without executing a command.

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    allow_prefixes=["/workspace/", "/tmp/"],
)
```

When `allow_prefixes` is `None` (the default), no write restrictions are applied.

> **Note:** This is a tool-level policy. It does not block `execute("echo bad > /etc/passwd")`. Use the Kubernetes pod `securityContext` (e.g. `readOnlyRootFilesystem`) for system-level protection.

### Virtual filesystem

When `virtual_mode=True`, all file-operation paths (`read`, `write`, `edit`, `ls`, `grep`, `glob`, uploads, downloads) are resolved under `root_dir` (default `/workspace`). Path traversal (`..`, `~`) is rejected.

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    virtual_mode=True,
    root_dir="/workspace",  # default when virtual_mode=True
)

# Agent sees virtual paths — resolved under /workspace automatically:
#   write("/src/main.py", ...)  →  writes to /workspace/src/main.py
#   read("/src/main.py")        →  reads from /workspace/src/main.py
#   upload_files([("/data/input.csv", content)])  →  /workspace/data/input.csv
```

In virtual mode, `download_files()` uses the native SDK HTTP transfer (`SandboxClient.read()`) for better performance. Uploads continue to use shell commands because the SDK `write()` method only preserves the file basename.

When combined with `allow_prefixes`, the policy check runs against the **resolved** path:

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    virtual_mode=True,
    root_dir="/workspace",
    allow_prefixes=["/workspace/"],
)
# Virtual path "/src/main.py" resolves to "/workspace/src/main.py" — allowed
# Virtual path "../../etc/passwd" — rejected (path traversal)
```

### Horizontal scaling and sticky sessions

When deploying a service that uses `KubernetesSandbox` behind a load balancer with multiple replicas, requests from the same user or session must be routed to the **same service instance**. The sandbox state (pod, port-forward) is held in-process, so different instances cannot share a sandbox.

**Configure sticky sessions** using one of these approaches:

Kubernetes Service with session affinity:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-agent-service
spec:
  sessionAffinity: ClientIP
  sessionAffinityConfig:
    clientIP:
      timeoutSeconds: 3600
  selector:
    app: my-agent
  ports:
    - port: 80
      targetPort: 8080
```

NGINX Ingress with cookie affinity:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-agent-ingress
  annotations:
    nginx.ingress.kubernetes.io/affinity: "cookie"
    nginx.ingress.kubernetes.io/session-cookie-name: "AGENT_SESSION"
    nginx.ingress.kubernetes.io/session-cookie-max-age: "3600"
spec:
  rules:
    - host: agent.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: my-agent-service
                port:
                  number: 80
```

### Preserving sandboxes across restarts

Set `skip_cleanup=True` to prevent sandbox pod destruction when `stop()` is called. The Kubernetes `SandboxClaim` is preserved so the sandbox pod continues running. Use `sandbox_id` for stable identification across service restarts.

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    skip_cleanup=True,
    sandbox_id="user-session-123",
)
```

The sandbox pod must be cleaned up externally (e.g. Kubernetes TTL controller, CronJob, or manual deletion).

> **Note:** Full sandbox reconnection (multiple service instances sharing the same Kubernetes pod) requires upstream SDK support for deterministic claim names. Currently each `start()` call creates a new `SandboxClaim`.

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
| `allow_prefixes` | `list[str] \| None` | `None` | Restrict `write`/`edit` to these path prefixes |
| `root_dir` | `str \| None` | `None` | Root directory for virtual filesystem mode |
| `virtual_mode` | `bool` | `False` | Resolve all paths under `root_dir` |
| `sandbox_id` | `str \| None` | `None` | Stable identifier (overrides auto-generated ID) |
| `skip_cleanup` | `bool` | `False` | Preserve `SandboxClaim` on `stop()` |

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
```

### Integration tests with Kind

Integration tests require a Kubernetes cluster. The repository includes scripts and manifests to set up a [Kind](https://kind.sigs.k8s.io/) cluster with everything needed.

**Prerequisites**: `kind`, `kubectl`, `docker`

```bash
# Create the Kind cluster and deploy agent-sandbox components
./scripts/kind-setup.sh

# Run integration tests
uv run pytest tests/integration/ -v -m integration

# Tear down when done
./scripts/kind-teardown.sh
```

The setup script will:

1. Create a Kind cluster named `langchain-k8s`
2. Install the agent-sandbox controller and extension CRDs (v0.1.1)
3. Enable the extensions controller
4. Deploy the sandbox router
6. Apply the `python-sandbox-template` SandboxTemplate

```
k8s/
├── sandbox-router.yaml            # Router Deployment + Service
└── sandbox-template.yaml          # SandboxTemplate for Python runtime
```

## License

MIT
