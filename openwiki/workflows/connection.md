# Workflows: Connection Modes

This document explains how `KubernetesSandbox` connects to the Kubernetes cluster and sandbox pods. There are four connection modes, each suited for different deployment scenarios.

**Source:** `src/langchain_k8s/sandbox.py`, `README.md` (connection modes section)

## Connection Mode Overview

| Mode            | Configuration                        | Use Case                     | Network                     |
| --------------- | ------------------------------------ | ---------------------------- | --------------------------- |
| **Production**  | `gateway_name="my-gateway"`          | Cluster with Gateway API     | Via Gateway API (routable)  |
| **Development** | _(default)_                          | Local development, testing   | Auto `kubectl port-forward` |
| **Advanced**    | `api_url="http://localhost:8080"`    | Pre-existing tunnel/forward  | Direct API endpoint         |
| **InCluster**   | `SandboxInClusterConnectionConfig()` | Agent running inside cluster | Cluster DNS or pod IP       |

## 1. Production Mode: Gateway API

### When to Use

- Cluster has [Gateway API](https://gateway-api.sigs.k8s.io/) installed
- Agent service is in production and exposed via Gateway
- You want routable, network-layer connectivity

### Configuration

```python
from langchain_k8s import KubernetesSandbox

backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    gateway_name="sandbox-gateway",  # Name of your Gateway resource
)
```

### How It Works

1. `k8s-agent-sandbox` controller creates a Kubernetes `Service` for the sandbox pod
2. Controller registers the service with the Gateway API
3. Agent connects to sandbox via the Gateway's routable address
4. Traffic is load-balanced by the Gateway

### Example Manifest

See `k8s/sandbox-router.yaml` in the repository for a complete Gateway API example:

```yaml
apiVersion: gateway.networking.k8s.io/v1beta1
kind: Gateway
metadata:
  name: sandbox-gateway
spec:
  gatewayClassName: cilium # or your Gateway class
  listeners:
    - name: http
      protocol: HTTP
      port: 80
```

## 2. Development Mode: Auto Port-Forward

### When to Use

- Local development (laptop, CI environment, etc.)
- `kubectl` is configured with cluster access
- Simple setup; don't want to manage port-forwards manually

### Configuration

```python
from langchain_k8s import KubernetesSandbox

backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    # gateway_name=None  (default)
    # api_url=None       (default)
)
```

**That's it!** No `gateway_name`, no `api_url`, no `connection_config` → automatic port-forward.

### How It Works

1. `KubernetesSandbox` creates a `SandboxClient` with default `SandboxLocalTunnelConnectionConfig()`
2. On first `execute()` call (or explicit `start()`), `_ensure_sandbox()` creates the pod
3. Controller automatically tunnels the pod's gRPC API to `localhost:RANDOM_PORT`
4. Agent connects to `localhost:RANDOM_PORT`
5. When sandbox is deleted, tunnel is cleaned up

### Port Assignment

Ports are allocated dynamically by the controller. You don't need to manage ports.

### Logs

Watch logs to see the auto-allocated port:

```bash
kubectl logs -n agent-sandbox-system -l app=agent-sandbox-controller -f
# Look for: "Tunnel allocated to localhost:12345"
```

## 3. Advanced Mode: Direct API URL

### When to Use

- You have an existing port-forward or tunnel
- You're running in a container that has direct access to a sandbox pod's API
- You need fine-grained control over the connection

### Configuration

```python
from langchain_k8s import KubernetesSandbox

backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    api_url="http://localhost:8080",  # Your tunnel endpoint
)
```

### Example: Manual Port-Forward

If you've manually set up a port-forward:

```bash
# In one terminal
kubectl port-forward -n agent-sandbox-system \
  pod/my-sandbox-pod 8080:9090
```

```python
# In your agent code
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    api_url="http://localhost:8080",
)
```

### Example: In-Container Access

If your agent service runs in the same Kubernetes cluster:

```python
# Inside the agent pod
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    api_url="http://sandbox-service.agent-sandbox-system.svc.cluster.local:9090",
)
```

## 4. InCluster Mode: Direct Cluster DNS

### When to Use

- Agent runs **inside the same Kubernetes cluster** as the sandboxes
- You want the lowest latency (no external tunnel)
- You have cluster DNS configured

### Configuration

```python
from langchain_k8s import KubernetesSandbox
from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

# Option A: Cluster DNS (default)
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    connection_config=SandboxInClusterConnectionConfig(),
)

# Option B: Pod IP (lower latency, requires pod security)
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    connection_config=SandboxInClusterConnectionConfig(use_pod_ip=True),
)
```

### Cluster DNS vs. Pod IP

**Cluster DNS (default):**

- Routes through Kubernetes DNS and Service networking
- More resilient (DNS caching, load balancing)
- Slightly higher latency (~milliseconds)
- Works with any pod security policy

**Pod IP (`use_pod_ip=True`):**

- Direct pod-to-pod connection (no DNS lookup)
- Lowest latency
- Requires pods can communicate directly (no network policies blocking)
- Breaks if pod is recreated (IP changes)

**Recommendation:** Use cluster DNS by default unless you have strict latency requirements.

### Deployment Pattern

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-agent-service
spec:
  replicas: 3
  selector:
    matchLabels:
      app: my-agent
  template:
    metadata:
      labels:
        app: my-agent
    spec:
      containers:
        - name: agent
          image: my-agent:latest
          env:
            - name: SANDBOX_NAMESPACE
              value: agent-sandbox-system
            - name: SANDBOX_TEMPLATE
              value: python-sandbox-template
          # Service account with permission to create sandboxes
          serviceAccountName: agent-sa
      serviceAccountName: agent-sa
```

```python
# In your agent code (running inside the pod)
import os
from langchain_k8s import KubernetesSandbox
from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

backend = KubernetesSandbox(
    template_name=os.getenv("SANDBOX_TEMPLATE"),
    namespace=os.getenv("SANDBOX_NAMESPACE"),
    connection_config=SandboxInClusterConnectionConfig(),
)
```

## Choosing a Connection Mode

### Quick Decision Tree

```
Is your agent running inside the Kubernetes cluster?
├─ Yes → Use InCluster mode (SandboxInClusterConnectionConfig)
└─ No
    ├─ Do you have a Gateway API installed?
    │  ├─ Yes → Use Production mode (gateway_name=...)
    │  └─ No
    │      ├─ Are you in local development?
    │      │  ├─ Yes → Use Development mode (default)
    │      │  └─ No → Use Advanced mode (api_url=...)
```

### Deployment Scenarios

**Local Development:**

```python
# Laptop, Kind cluster, no special setup needed
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
)
```

**CI/CD Pipeline (GitHub Actions, etc.):**

```python
# CI runner spins up Kind, test agent connects
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
)
```

**Production (Agent Service on Same Cluster):**

```python
from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    connection_config=SandboxInClusterConnectionConfig(),
)
```

**Production (Agent Service on Different Cluster):**

```python
# If clusters are networked and can route to each other
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    gateway_name="sandbox-gateway",
)
```

**Production with Manual Tunneling:**

```python
# For exotic network topologies
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    api_url="http://your-tunnel-endpoint:port",
)
```

## Troubleshooting Connection Issues

### "Connection refused"

```python
# Check that:
# 1. Pod is running: kubectl get pods -n agent-sandbox-system
# 2. Service exists: kubectl get svc -n agent-sandbox-system
# 3. Port-forward works manually: kubectl port-forward pod/<name> 8080:9090
```

### "Timeout waiting for sandbox"

```python
# Sandbox may be slow to start. Increase timeout:
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    command_timeout=600,  # 10 minutes
)
```

### "DNS not found"

**In InCluster mode:**

```bash
# Check cluster DNS is working
kubectl run -it --rm debug --image=busybox -- nslookup kubernetes.default
```

### "Connection refused" in InCluster mode

```bash
# Check pod-to-pod connectivity
kubectl exec -it <agent-pod> -- \
  nc -zv sandbox-service.agent-sandbox-system.svc.cluster.local 9090
```

## Further Reading

- [Workflows: Lifecycle](lifecycle.md) — Pod lifetime, auto-reconnect, thread-scoped patterns
- [Operations: Deployment](../operations/deployment.md) — Setup manifests for local and production
- [Architecture: Design](../architecture/design.md) — Constructor modes and lifecycle
