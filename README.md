<p align="center">
  <img src="docs/icon.svg" alt="langchain-k8s logo" width="140" />
</p>

<h1 align="center">langchain-k8s</h1>

<p align="center">
  <strong>Kubernetes-native sandbox execution for LangChain Deep Agents</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/langchain-k8s/"><img src="https://img.shields.io/pypi/v/langchain-k8s?style=flat-square&color=326CE5&label=PyPI" alt="PyPI version"></a>
  <a href="https://pypi.org/project/langchain-k8s/"><img src="https://img.shields.io/pypi/pyversions/langchain-k8s?style=flat-square&color=F59E0B" alt="Python versions"></a>
  <a href="https://github.com/uesleilima/langchain-k8s/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/uesleilima/langchain-k8s/ci.yml?style=flat-square&label=CI" alt="CI status"></a>
  <a href="https://sonarcloud.io/summary/new_code?id=uesleilima_langchain-k8s"><img src="https://sonarcloud.io/api/project_badges/measure?project=uesleilima_langchain-k8s&metric=alert_status" alt="Quality Gate Status"></a>
  <a href="https://github.com/uesleilima/langchain-k8s/blob/main/LICENSE"><img src="https://img.shields.io/github/license/uesleilima/langchain-k8s?style=flat-square&color=22C55E" alt="License"></a>
</p>

<p align="center">
  Give your AI agents their own isolated Kubernetes pods to run shell commands and file operations — fully self-hosted, zero vendor lock-in.
</p>

---

Implements the [`BaseSandbox`](https://github.com/langchain-ai/deepagents) / `SandboxBackendProtocol` contract using [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) as the execution backend.

## Why langchain-k8s?

- **Isolated execution** — each agent gets its own pod with its own filesystem
- **Self-hosted** — runs on any Kubernetes cluster you control
- **Ephemeral or persistent** — fresh pods per task, or long-lived pods that survive across turns
- **Drop-in compatible** — implements the LangChain Deep Agents sandbox protocol
- **Enterprise-ready** — path policies, virtual filesystems, sticky sessions, reconnection

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

### Ecosystem-standard mode (recommended)

Pass a pre-created `k8s_agent_sandbox.Sandbox` handle — the standard LangChain Deep Agents sandbox backend pattern. Lifecycle management stays with the caller:

```python
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxLocalTunnelConnectionConfig
from langchain_anthropic import ChatAnthropic
from deepagents import create_deep_agent
from langchain_k8s import KubernetesSandbox

client = SandboxClient(
    connection_config=SandboxLocalTunnelConnectionConfig(),
)
handle = client.create_sandbox(
    template="python-sandbox-template",
    namespace="agent-sandbox-system",
)

backend = KubernetesSandbox(sandbox=handle)

agent = create_deep_agent(
    model=ChatAnthropic(model="claude-sonnet-4-20250514"),
    system_prompt="You are a Python coding assistant with sandbox access.",
    backend=backend,
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "Create a Python script that prints the Fibonacci sequence"}]}
)

client.delete_sandbox(handle.claim_name, "agent-sandbox-system")
```

### Config-based mode (convenience)

For simpler setups, pass `template_name` and connection parameters directly. The sandbox is created lazily on first use and destroyed on `stop()`:

```python
from langchain_k8s import KubernetesSandbox

backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
)

agent = create_deep_agent(model=model, backend=backend, ...)
result = agent.invoke({"messages": [...]})

backend.stop()
```

## Usage

### Context manager

```python
with KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
) as backend:
    agent = create_deep_agent(model=model, backend=backend, ...)
    result = agent.invoke({"messages": [...]})
# Sandbox pod is automatically cleaned up
```

### Direct execution

```python
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxLocalTunnelConnectionConfig
from langchain_k8s import KubernetesSandbox

client = SandboxClient(
    connection_config=SandboxLocalTunnelConnectionConfig(),
)
handle = client.create_sandbox(
    template="python-sandbox-template",
    namespace="agent-sandbox-system",
)

backend = KubernetesSandbox(sandbox=handle)

# Execute commands (with optional per-call timeout)
resp = backend.execute("echo 'Hello from K8s!'")
print(resp.output, resp.exit_code)

resp = backend.execute("python3 long_script.py", timeout=600)

# Upload files
backend.upload_files([("/workspace/script.py", b"print('hello')\n")])

# Download files
results = backend.download_files(["/workspace/script.py"])
print(results[0].content)

client.delete_sandbox(handle.claim_name, "agent-sandbox-system")
```

---

## Connection modes

| Mode | Configuration | Use case |
|------|--------------|----------|
| **Production** | `gateway_name="my-gateway"` | Cluster with Gateway API |
| **Development** | *(default — no gateway, no api_url)* | Auto `kubectl port-forward` |
| **Advanced** | `api_url="http://localhost:8080"` | Pre-existing port-forward or in-cluster |
| **InCluster** | `connection_config=SandboxInClusterConnectionConfig()` | Agent running inside the same Kubernetes cluster |

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

# InCluster — agent pod connecting directly to sandbox pods
from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    connection_config=SandboxInClusterConnectionConfig(),          # cluster DNS
    # connection_config=SandboxInClusterConnectionConfig(use_pod_ip=True),  # pod IP (lower latency)
)
```

## Sandbox lifecycle

### Thread-scoped (production)

Use `create_kubernetes_sandbox()` for thread-scoped sandboxes in production. It implements a get-or-create pattern: if a sandbox with the given `claim_name` already exists, it is reused; otherwise a new one is created.

Define a graph factory that provisions a sandbox per conversation thread:

```python
from langchain_anthropic import ChatAnthropic
from deepagents import create_deep_agent
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxGatewayConnectionConfig
from langchain_k8s import create_kubernetes_sandbox
from langchain_core.runnables import RunnableConfig

client = SandboxClient(
    connection_config=SandboxGatewayConnectionConfig(gateway_name="sandbox-gw"),
)

async def make_agent(config: RunnableConfig):
    """Graph factory — each thread_id gets its own sandbox."""
    thread_id = config["configurable"]["thread_id"]
    backend = create_kubernetes_sandbox(
        client=client,
        claim_name=f"sandbox-{thread_id}",
        template_name="python-sandbox-template",
        namespace="agent-sandbox-system",
        labels={"thread_id": thread_id},
    )
    return create_deep_agent(
        model=ChatAnthropic(model="claude-sonnet-4-20250514"),
        backend=backend,
    )
```

Each conversation thread gets its own sandbox. When the thread resumes, the existing sandbox is found by `claim_name` and reused with its filesystem state intact:

```python
# Turn 1 — user starts a new conversation, sandbox is created
config = {"configurable": {"thread_id": "user-session-abc"}}
agent = await make_agent(config)
result = agent.invoke(
    {"messages": [{"role": "user", "content": "Write a Python script that fetches weather data"}]}
)
# Agent writes files to the sandbox filesystem...

# Turn 2 — user continues the conversation, same sandbox is reused
agent = await make_agent(config)  # get-or-create finds the existing pod
result = agent.invoke(
    {"messages": [{"role": "user", "content": "Now add error handling to the script"}]}
)
# Agent can read/modify files from turn 1 — filesystem state persists

# Clean up when the conversation ends
client.delete_sandbox(f"sandbox-user-session-abc", "agent-sandbox-system")
```

### Persistent (config-based, default)

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    reuse_sandbox=True,  # default
)
```

One sandbox pod is created lazily and reused across all calls. Fast for cached, long-lived agents. Filesystem state persists between invocations. Auto-reconnects if the pod dies.

### Ephemeral (config-based)

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    reuse_sandbox=False,
)
```

A fresh sandbox is created for each `start()`/`stop()` cycle. Maximum isolation between invocations at the cost of cold-start latency.

---

## Enterprise features

### Path access policy

Restrict which directories agents can write to using `allow_prefixes`. When set, only `write()` and `edit()` operations targeting paths under the specified prefixes are permitted. All other paths return an error without executing a command.

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    allow_prefixes=["/tmp/"],
)
```

When `allow_prefixes` is `None` (the default), no write restrictions are applied.

> **Note:** `allow_prefixes` is a tool-level policy — it controls which paths `write()` and `edit()` accept, but does not block shell commands like `execute("echo bad > /etc/passwd")`. Use the Kubernetes pod `securityContext` (e.g. `readOnlyRootFilesystem`) for system-level protection. The allowed directories must also be **writable inside the container** — see [Container permissions vs. sandbox policy](#container-permissions-vs-sandbox-policy).

### Virtual filesystem

When `virtual_mode=True`, all file-operation paths (`read`, `write`, `edit`, `ls`, `grep`, `glob`, uploads, downloads) are resolved under `root_dir` (default `/tmp`). Path traversal (`..`, `~`) is rejected.

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    virtual_mode=True,
    root_dir="/tmp",
)

# Agent sees virtual paths — resolved under /tmp automatically:
#   write("/src/main.py", ...)  →  writes to /tmp/src/main.py
#   read("/src/main.py")        →  reads from /tmp/src/main.py
#   upload_files([("/data/input.csv", content)])  →  /tmp/data/input.csv
```

When combined with `allow_prefixes`, the policy check runs against the **resolved** path:

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    virtual_mode=True,
    root_dir="/tmp",
    allow_prefixes=["/tmp/"],
)
# Virtual path "/src/main.py" resolves to "/tmp/src/main.py" — allowed
# Virtual path "../../etc/passwd" — rejected (path traversal)
```

### Container permissions vs. sandbox policy

`allow_prefixes` and `virtual_mode` are **tool-level policies** enforced by `KubernetesSandbox` *before* any command reaches the container. They do **not** grant filesystem permissions inside the container itself.

For file operations to succeed, two conditions must be met:

1. **Sandbox policy allows the path** — `allow_prefixes` check passes (or is not set)
2. **Container OS user can write to the path** — the directory exists and is writable inside the container

If a `write()` or `edit()` call passes the sandbox policy but fails with `PermissionError`, the container's filesystem permissions are the cause. The sandbox logs a warning when this happens:

```
WARNING  langchain_k8s.sandbox: write: container permission denied for path='/src/main.py'
(resolved='/workspace/src/main.py'). The sandbox policy (allow_prefixes) permits this path,
but the container's OS user cannot write to it.
```

**Common writable locations** (no extra configuration needed):

| Directory | Notes |
|-----------|-------|
| `/tmp` | Always writable; good default for `root_dir` |
| `/home/<user>` | Writable if the container runs as that user |

**Making a custom directory writable** in your `SandboxTemplate`:

```yaml
apiVersion: agents.x-k8s.io/v1alpha1
kind: SandboxTemplate
metadata:
  name: python-sandbox-template
spec:
  template:
    spec:
      containers:
        - name: sandbox
          image: python:3.12-slim
          volumeMounts:
            - name: workspace
              mountPath: /workspace
      volumes:
        - name: workspace
          emptyDir: {}
```

With this configuration, `/workspace` is backed by an `emptyDir` volume and is writable regardless of the container's root filesystem permissions. You can then safely use `root_dir="/workspace"` and `allow_prefixes=["/workspace/"]`.

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

### Preserving sandboxes and reconnection

Set `skip_cleanup=True` to prevent sandbox pod destruction when `stop()` is called. The Kubernetes `SandboxClaim` is preserved so the sandbox pod continues running.

When an agent process restarts (pod eviction, rolling update, crash), it can reconnect to the still-running sandbox by passing the original `claim_name`. Persist the `claim_name` property after `start()` and use it on the next instantiation:

```python
# First run — create sandbox and persist claim name
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    skip_cleanup=True,
)
backend.start()
redis.set("sandbox:user-123", backend.claim_name)  # persist for reconnection
```

```python
# After restart — reconnect to the same pod
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    claim_name=redis.get("sandbox:user-123"),  # re-attach, no new pod
    skip_cleanup=True,
)
backend.start()  # re-establishes connection to existing pod
resp = backend.execute("cat /workspace/previous-work.py")  # state is preserved
```

The sandbox pod must be cleaned up externally when no longer needed (e.g. Kubernetes TTL controller, CronJob, or manual deletion).

### Sandbox labels

Tag sandboxes at creation with Kubernetes labels for discovery, filtering, and cleanup policies:

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    labels={
        "session": "abc-123",
        "agent-id": "code-reviewer",
        "team": "platform",
    },
    skip_cleanup=True,
)
```

Labels are applied to the `SandboxClaim` resource and can be used with `kubectl`:

```bash
# List sandboxes for a specific session
kubectl get sandboxclaims -l session=abc-123

# Clean up all sandboxes for a team
kubectl delete sandboxclaims -l team=platform
```

Labels are only applied at creation time. When reconnecting via `claim_name`, the existing labels on the `SandboxClaim` are preserved.

---

## Configuration reference

<details>
<summary><strong><code>KubernetesSandbox</code> constructor</strong></summary>

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `sandbox` | `Sandbox \| None` | `None` | Pre-created `k8s_agent_sandbox.Sandbox` handle (ecosystem-standard mode) |
| `template_name` | `str \| None` | `None` | `SandboxTemplate` CRD name. Required when `sandbox` is not provided |
| `namespace` | `str` | `"default"` | Kubernetes namespace |
| `gateway_name` | `str \| None` | `None` | Gateway name (production mode, config-based only) |
| `gateway_namespace` | `str` | `"default"` | Gateway namespace |
| `api_url` | `str \| None` | `None` | Direct router URL (advanced mode, config-based only) |
| `server_port` | `int` | `8888` | Sandbox runtime port |
| `reuse_sandbox` | `bool` | `True` | Reuse sandbox across calls (config-based only) |
| `max_output_size` | `int` | `1048576` | Max output bytes before truncation |
| `command_timeout` | `int` | `300` | Default command timeout in seconds. Can be overridden per-call via `execute(timeout=...)` |
| `allow_prefixes` | `list[str] \| None` | `None` | Restrict `write`/`edit` to these path prefixes |
| `root_dir` | `str \| None` | `None` | Root directory for virtual filesystem mode. Defaults to `/tmp` when `virtual_mode=True` |
| `virtual_mode` | `bool` | `False` | Resolve all paths under `root_dir` |
| `skip_cleanup` | `bool` | `False` | Preserve `SandboxClaim` on `stop()` (config-based only) |
| `claim_name` | `str \| None` | `None` | Reconnect to existing sandbox by claim name. Cannot be combined with `sandbox` |
| `labels` | `dict[str, str] \| None` | `None` | Kubernetes labels applied to `SandboxClaim` |
| `connection_config` | `SandboxConnectionConfig \| None` | `None` | Pre-built SDK connection config. Overrides `api_url`/`gateway_name`. Supports `SandboxInClusterConnectionConfig` for in-cluster agents |
| `shutdown_after_seconds` | `int \| None` | `None` | Auto-delete `SandboxClaim` this many seconds after the sandbox finishes (config-based only) |

</details>

<details>
<summary><strong><code>create_kubernetes_sandbox()</code> factory</strong></summary>

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `client` | `SandboxClient` | *(required)* | `k8s_agent_sandbox.SandboxClient` instance |
| `claim_name` | `str` | *(required)* | `SandboxClaim` name to look up or create |
| `template_name` | `str` | *(required)* | `SandboxTemplate` CRD name (used when creating) |
| `namespace` | `str` | `"default"` | Kubernetes namespace |
| `labels` | `dict[str, str] \| None` | `None` | Labels applied at creation time |
| `**kwargs` | | | Forwarded to `KubernetesSandbox` (e.g. `allow_prefixes`, `virtual_mode`) |

</details>

---

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
uv run pyright src/
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
2. Install the agent-sandbox controller and extension CRDs (v0.3.10)
3. Enable the extensions controller
4. Deploy the sandbox router
5. Apply the `python-sandbox-template` SandboxTemplate

```
k8s/
├── sandbox-router.yaml            # Router Deployment + Service
└── sandbox-template.yaml          # SandboxTemplate for Python runtime
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
