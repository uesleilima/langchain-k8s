> [!IMPORTANT]
> **This repository is moving to [Backbase/langchain-k8s](https://github.com/Backbase/langchain-k8s).**
> Further implementation and development will happen there. Please use the Backbase repository for issues, pull requests, and new work.

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
- **Disposable or durable** — tear a pod down at the end of a task, or keep it alive and reconnect to it across turns
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

- A Kubernetes cluster with the [agent-sandbox controller](https://github.com/kubernetes-sigs/agent-sandbox) **v1.0.0 or newer** installed
- A `SandboxTemplate` resource defining the pod spec for your sandboxes
- A `SandboxWarmPool` resource referencing that template — this is what sandboxes are claimed from, and it is mandatory (use `replicas: 0` for pure on-demand cold start)
- `kubectl` configured with cluster access

### Upgrading to 0.6.0 (breaking)

agent-sandbox v0.5.0 graduated its CRDs to `v1beta1`, and a `v1beta1`
`SandboxClaim` has **no template reference at all** — `spec.warmPoolRef` is
required instead. A claim now points at a `SandboxWarmPool`, and the pool is
what points at the `SandboxTemplate`. This package follows that model:

| Before (0.5.x) | Now (0.6.0) |
| --- | --- |
| `KubernetesSandbox(template_name=...)` | `KubernetesSandbox(warmpool_name=...)` |
| `create_kubernetes_sandbox(template_name=...)` | `create_kubernetes_sandbox(warmpool_name=...)` |
| separate `warmpool=` argument | removed — `warmpool_name` *is* the pool |

There is no deprecated alias: passing `template_name=` now raises
`TypeError`. To migrate, create a `SandboxWarmPool` pointing at your existing
template and pass its name. `SandboxInClusterConnectionConfig(use_pod_ip=...)`
is also gone; the pod IP is now preferred automatically with a cluster-DNS
fallback.

### Upgrading to 0.7.0

The dependency floor is now `deepagents>=0.7.21` and `k8s-agent-sandbox>=1.0.4`,
and the cluster needs an agent-sandbox controller **v1.0.0 or newer**. v1.0.0
removed the `v1alpha1` API. A cluster that still lists `v1alpha1` in any
agent-sandbox CRD's `status.storedVersions` cannot jump straight to v1.0 —
upgrade to v0.5.x and finish the storage migration first. See the
[v1alpha1 to v1beta1 migration guide](https://agent-sandbox.sigs.k8s.io/docs/getting_started/api-migration-guide/).

Two behaviour changes come from deepagents 0.7:

- `write()` overwrites a file that already exists (and creates missing parent
  directories). Previously a second write to the same path failed.
- `delete()` is part of the backend contract. `allow_prefixes` covers it the
  same way it covers `write()` and `edit()`, including on the async path.

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
    warmpool="python-sandbox-pool",
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

For simpler setups, pass `warmpool_name` and connection parameters directly. The sandbox is created lazily on first use and destroyed on `stop()`:

```python
from langchain_k8s import KubernetesSandbox

backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
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
    warmpool_name="python-sandbox-pool",
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
    warmpool="python-sandbox-pool",
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

| Mode            | Configuration                                          | Use case                                         |
| --------------- | ------------------------------------------------------ | ------------------------------------------------ |
| **Production**  | `gateway_name="my-gateway"`                            | Cluster with Gateway API                         |
| **Development** | _(default — no gateway, no api_url)_                   | Auto `kubectl port-forward`                      |
| **Advanced**    | `api_url="http://localhost:8080"`                      | Pre-existing port-forward or in-cluster          |
| **InCluster**   | `connection_config=SandboxInClusterConnectionConfig()` | Agent running inside the same Kubernetes cluster |

```python
# Production — cluster Gateway
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
    gateway_name="sandbox-gateway",
)

# Development — automatic port-forward
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
)

# Advanced — existing port-forward or in-cluster routing
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
    api_url="http://localhost:8080",
)

# InCluster — agent pod connecting directly to sandbox pods
from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
    # Prefers the pod IP from the Sandbox status, falls back to cluster DNS.
    connection_config=SandboxInClusterConnectionConfig(),
)
```

### sandboxd

`SandboxdBackend` is the backend for a sandboxd pod. `execute` still runs over gRPC (`commands.run` on port 9090). `read`, `write`, `edit`, `ls`, `grep`, `glob`, `delete`, `upload_files` and `download_files` use sandboxd's REST filesystem API (`/v1/files` on port 8080), so they do not need `python3` inside the container. The stock `sandboxd:latest-main` image is enough.

```bash
pip install 'langchain-k8s[sandboxd]'
```

```python
from langchain_k8s import SandboxdBackend

backend = SandboxdBackend(
    warmpool_name="sandboxd-pool",
    namespace="agent-sandbox-system",
)
backend.write("/workspace/src/a.py", "print('hi')\n")
```

The constructor defaults `connection_config` to `SandboxdPodTunnelConnectionConfig`. `api_url` and `gateway_name` are rejected: the sandbox router cannot proxy gRPC. To attach a handle that a sandboxd client already created, pass `sandbox=handle`. `create_kubernetes_sandbox` still returns a `KubernetesSandbox`.

Agent paths are real absolute paths, and every file tool confines them to `sandboxd_root` (default `/workspace`, the daemon's `--root-dir`). A path outside that root comes back as `invalid_path`. With `virtual_mode` on, set `root_dir` under `/workspace`.

`FilesystemMiddleware` records oversized `execute` output at `/large_tool_results/<id>` and later `read_file`s that pointer. Both the shell write and the later read are rerooted to `/workspace/large_tool_results/<id>`, so the pointer opens. Set `enable_capture_offload=True` for that path; `SandboxdBackend` stays a `BaseSandbox`, which is what the middleware checks before it offloads.

`grep` and `glob` walk the tree over REST: one `list` per directory, and `grep` reads each candidate file. That is one HTTP call per directory (and per file, for `grep`), not a single in-pod `python3` scan. Files larger than `grep_max_file_bytes` (default 1 MiB) and files that are not UTF-8 are skipped.

`scripts/kind-setup.sh` applies `k8s/sandboxd-template.yaml` and `k8s/sandboxd-warmpool.yaml`. `tests/integration/test_sandboxd_kind.py` covers the native file tools and skips when the pool is absent.

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
        warmpool_name="python-sandbox-pool",
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

### Config-based lifecycle

In config-based mode the sandbox pod is created lazily on first use and reused for every subsequent call. Filesystem state persists for the life of the backend. `stop()` deletes the `SandboxClaim` — unless you pass `skip_cleanup=True`, which leaves the pod running so you can reconnect to it later by `claim_name`.

```python
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
)
```

To get a fresh pod, create a fresh backend — or call `stop()` then `start()`, which deletes the claim and provisions a new one.

### `reuse_sandbox` — automatic reconnection

```python
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
    reuse_sandbox=False,
)
```

`reuse_sandbox` controls **error recovery, not pod lifetime**. With the default `True`, a failure inside `execute()` causes the backend to drop the dead sandbox, provision a replacement, and retry the command once before giving up. Setting it to `False` disables that retry, so connection errors propagate to the caller immediately.

It has no effect on `start()`/`stop()`, and it does not create a pod per invocation. Auto-reconnect is also inactive in handle mode, where the caller owns the lifecycle.

### Large command output

`enable_capture_offload=True` makes oversized `execute` output stay in the sandbox. The tool result then carries a short preview and a `read_file` pointer under `/large_tool_results/<tool_call_id>`, instead of the full output. The default is off, so output is returned inline and clipped at `max_output_size`.

Keep `max_output_size` above the middleware's inline budget (it derives that budget from `tool_token_limit_before_evict`, 20,000 tokens by default). If the preview itself is truncated, the offload result cannot be parsed and the agent sees the raw wrapper output.

The capture file is written by `execute`, so `allow_prefixes` does not cover it. `/large_tool_results` — or `<root_dir>/large_tool_results` when `virtual_mode` is on — must be writable in the container, the same way `/workspace` must be.

---

## Enterprise features

### Path access policy

Restrict which directories agents can write to using `allow_prefixes`. When set, only `write()`, `edit()` and `delete()` operations targeting paths under the specified prefixes are permitted. All other paths return an error without executing a command. The same check runs for the async variants (`awrite`, `aedit`, `adelete`).

```python
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
    allow_prefixes=["/tmp/"],
)
```

When `allow_prefixes` is `None` (the default), no write restrictions are applied.

> **Note:** `allow_prefixes` is a tool-level policy — it controls which paths `write()`, `edit()` and `delete()` accept, but does not block shell commands like `execute("echo bad > /etc/passwd")` or `execute("rm -rf /")`. Use the Kubernetes pod `securityContext` (e.g. `readOnlyRootFilesystem`) for system-level protection. The allowed directories must also be **writable inside the container** — see [Container permissions vs. sandbox policy](#container-permissions-vs-sandbox-policy).

### Virtual filesystem

When `virtual_mode=True`, all file-operation paths (`read`, `write`, `edit`, `delete`, `ls`, `grep`, `glob`, uploads, downloads, and their async counterparts) are resolved under `root_dir` (default `/tmp`). Path traversal (`..`, `~`) is rejected. `glob()` and `grep()` with no path search `root_dir` rather than the filesystem root.

```python
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
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
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
    virtual_mode=True,
    root_dir="/tmp",
    allow_prefixes=["/tmp/"],
)
# Virtual path "/src/main.py" resolves to "/tmp/src/main.py" — allowed
# Virtual path "../../etc/passwd" — rejected (path traversal)
```

### Container permissions vs. sandbox policy

`allow_prefixes` and `virtual_mode` are **tool-level policies** enforced by `KubernetesSandbox` _before_ any command reaches the container. They do **not** grant filesystem permissions inside the container itself.

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

| Directory      | Notes                                        |
| -------------- | -------------------------------------------- |
| `/tmp`         | Always writable; good default for `root_dir` |
| `/home/<user>` | Writable if the container runs as that user  |

**Making a custom directory writable** in your `SandboxTemplate`:

```yaml
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: python-sandbox-template
  namespace: agent-sandbox-system
spec:
  podTemplate:
    spec:
      containers:
        - name: python-runtime
          image: registry.k8s.io/agent-sandbox/python-runtime-sandbox:v1.0.4
          volumeMounts:
            - name: workspace
              mountPath: /workspace
      volumes:
        - name: workspace
          emptyDir: {}
```

With this configuration, `/workspace` is backed by an `emptyDir` volume and is writable regardless of the container's root filesystem permissions. You can then safely use `root_dir="/workspace"` and `allow_prefixes=["/workspace/"]`.

### Durable workspaces with `volume_claim_templates`

`emptyDir` disappears with the pod. For a workspace that survives a pod
restart, pass `volume_claim_templates` instead — the claim then requests a
`PersistentVolumeClaim` per sandbox:

```python
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
    root_dir="/workspace",
    virtual_mode=True,
    volume_claim_templates=[
        {
            "metadata": {"name": "workspace"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "1Gi"}},
            },
        }
    ],
)
```

Two requirements that are easy to miss:

- The **`SandboxTemplate` pod spec must still declare a matching `volumeMounts` entry** (name `workspace`, `mountPath: /workspace`). The claim template supplies the volume; only the template can mount it.
- The cluster needs a working `StorageClass`. Kind ships `local-path` with `WaitForFirstConsumer`, so the PVC stays `Pending` until the sandbox pod is scheduled — expect the first start to be slower than with `emptyDir`.

### Pod labels and annotations

`labels` land on the `SandboxClaim` object. To label the **pod** instead —
so the values are visible to node-level tooling and readable from inside the
container through the Downward API — use `pod_labels` / `pod_annotations`,
which map to `spec.additionalPodMetadata`:

```python
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
    labels={"session": "s1"},            # -> SandboxClaim object
    pod_labels={"team": "platform"},     # -> the running Pod
    pod_annotations={"owner": "agents"},
)
```

These are validated client-side for RFC 1123 syntax only. The controller
additionally rejects reserved and restricted-domain keys, which fails the
claim quickly with an `InvalidMetadata` reason rather than timing out.

> **Note:** the SDK always stamps `agents.x-k8s.io/created-by: python-client`
> on claims it creates, so a claim's label set is never exactly what you
> passed in. Account for it in any label selector you write.

### Environment variables

`env` injects variables into the sandbox container through `spec.env`:

```python
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
    env={"LANG": "C.UTF-8"},
)
```

Setting `env` forces a cold start from the warm-pool template. A pre-warmed pod is not adopted, so start-up is slower. The `SandboxTemplate` must set `envVarsInjectionPolicy: Allowed` (the CRD default is `Disallowed`); otherwise the claim fails immediately with `EnvVarsInjectionRejected`. An invalid variable name fails the same way rather than waiting out the ready timeout. `env` is creation-time only, and `create_kubernetes_sandbox` takes it as its own argument — passing it through `**kwargs` would be stored and ignored.

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
    warmpool_name="python-sandbox-pool",
    namespace="agent-sandbox-system",
    skip_cleanup=True,
)
backend.start()
redis.set("sandbox:user-123", backend.claim_name)  # persist for reconnection
```

```python
# After restart — reconnect to the same pod
backend = KubernetesSandbox(
    warmpool_name="python-sandbox-pool",
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
    warmpool_name="python-sandbox-pool",
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

| Parameter                | Type                              | Default     | Description                                                                                                                            |
| ------------------------ | --------------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `sandbox`                | `Sandbox \| None`                 | `None`      | Pre-created `k8s_agent_sandbox.Sandbox` handle (ecosystem-standard mode)                                                               |
| `warmpool_name`          | `str \| None`                     | `None`      | `SandboxWarmPool` CRD name to claim a sandbox from. Required when `sandbox` is not provided                                            |
| `namespace`              | `str`                             | `"default"` | Kubernetes namespace                                                                                                                   |
| `gateway_name`           | `str \| None`                     | `None`      | Gateway name (production mode, config-based only)                                                                                      |
| `gateway_namespace`      | `str`                             | `"default"` | Gateway namespace                                                                                                                      |
| `api_url`                | `str \| None`                     | `None`      | Direct router URL (advanced mode, config-based only)                                                                                   |
| `server_port`            | `int`                             | `8888`      | Sandbox runtime port                                                                                                                   |
| `reuse_sandbox`          | `bool`                            | `True`      | Auto-reconnect and retry once when `execute()` hits a connection error (config-based only). Does not affect pod lifetime               |
| `max_output_size`        | `int`                             | `1048576`   | Max output bytes before truncation                                                                                                     |
| `command_timeout`        | `int`                             | `300`       | Default command timeout in seconds. Can be overridden per-call via `execute(timeout=...)`                                              |
| `enable_capture_offload` | `bool`                            | `False`     | Leave large `execute` output in the sandbox and return a preview plus a `read_file` pointer                                            |
| `allow_prefixes`         | `list[str] \| None`               | `None`      | Restrict `write`/`edit`/`delete` to these path prefixes                                                                                |
| `root_dir`               | `str \| None`                     | `None`      | Root directory for virtual filesystem mode. Defaults to `/tmp` when `virtual_mode=True`                                                |
| `virtual_mode`           | `bool`                            | `False`     | Resolve all paths under `root_dir`                                                                                                     |
| `skip_cleanup`           | `bool`                            | `False`     | Preserve `SandboxClaim` on `stop()` (config-based only)                                                                                |
| `claim_name`             | `str \| None`                     | `None`      | Reconnect to existing sandbox by claim name. Cannot be combined with `sandbox`                                                         |
| `labels`                 | `dict[str, str] \| None`          | `None`      | Kubernetes labels applied to the `SandboxClaim` object                                                                                 |
| `connection_config`      | `SandboxConnectionConfig \| None` | `None`      | Pre-built SDK connection config. Overrides `api_url`/`gateway_name`. Supports `SandboxInClusterConnectionConfig` for in-cluster agents |
| `shutdown_after_seconds` | `int \| None`                     | `None`      | Auto-delete `SandboxClaim` this many seconds after the sandbox finishes (config-based only)                                            |
| `pod_labels`             | `dict[str, str] \| None`          | `None`      | Labels stamped onto the sandbox **Pod** via `spec.additionalPodMetadata` (config-based only)                                           |
| `pod_annotations`        | `dict[str, str] \| None`          | `None`      | Annotations stamped onto the sandbox **Pod** via `spec.additionalPodMetadata` (config-based only)                                      |
| `volume_claim_templates` | `list[dict] \| None`              | `None`      | PVC templates for durable sandbox storage (config-based only)                                                                          |
| `env`                    | `dict[str, str] \| None`          | `None`      | Container env vars via `spec.env`. Forces a cold start (config-based only)                                                             |
| `router_namespace`       | `str`                             | `"agent-sandbox-system"` | Namespace of `sandbox-router-svc` for automatic port-forward (development mode only)                                      |

</details>

<details>
<summary><strong><code>create_kubernetes_sandbox()</code> factory</strong></summary>

| Parameter                | Type                     | Default      | Description                                                              |
| ------------------------ | ------------------------ | ------------ | ------------------------------------------------------------------------ |
| `client`                 | `SandboxClient`          | _(required)_ | `k8s_agent_sandbox.SandboxClient` instance                               |
| `claim_name`             | `str`                    | _(required)_ | `SandboxClaim` name to look up or create                                 |
| `warmpool_name`          | `str`                    | _(required)_ | `SandboxWarmPool` CRD name the new claim points at                       |
| `namespace`              | `str`                    | `"default"`  | Kubernetes namespace                                                     |
| `labels`                 | `dict[str, str] \| None` | `None`       | Labels applied to the `SandboxClaim` object at creation time             |
| `pod_labels`             | `dict[str, str] \| None` | `None`       | Labels stamped onto the sandbox **Pod**                                  |
| `pod_annotations`        | `dict[str, str] \| None` | `None`       | Annotations stamped onto the sandbox **Pod**                             |
| `volume_claim_templates` | `list[dict] \| None`     | `None`       | PVC templates for durable sandbox storage                                |
| `shutdown_after_seconds` | `int \| None`            | `None`       | TTL after which the controller deletes the `SandboxClaim`                |
| `env`                    | `dict[str, str] \| None` | `None`       | Container env vars via `spec.env`. Forces a cold start; ignored on reconnect |
| `sandbox_ready_timeout`  | `int`                    | `180`        | Max seconds to wait for a newly created sandbox to become ready          |
| `**kwargs`               |                          |              | Forwarded to `KubernetesSandbox` (e.g. `allow_prefixes`, `virtual_mode`) |

Every parameter that has to reach the cluster is declared explicitly here
rather than left to `**kwargs`. The factory always returns a backend in
ecosystem-standard mode, so creation-time constructor arguments arriving via
`**kwargs` would be stored and silently ignored.

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
2. Install the agent-sandbox controller and extension CRDs from the all-in-one release asset (v1.0.4, overridable via `AGENT_SANDBOX_VERSION`)
3. Deploy the sandbox router
4. Apply the `python-sandbox-template` SandboxTemplate
5. Apply the `python-sandbox-pool` SandboxWarmPool (`replicas: 0` by default; set `WARMPOOL_REPLICAS` to pre-warm pods instead)
6. Apply the `sandboxd-template` SandboxTemplate and `sandboxd-pool` SandboxWarmPool

```
k8s/
├── sandbox-router.yaml            # Router Deployment + Service
├── sandbox-template.yaml          # SandboxTemplate for Python runtime
├── sandbox-warmpool.yaml          # SandboxWarmPool claims reference
├── sandboxd-template.yaml         # SandboxTemplate for the sandboxd runtime
└── sandboxd-warmpool.yaml         # SandboxWarmPool for sandboxd
```

---

## License

MIT — see [LICENSE](LICENSE) for details.
