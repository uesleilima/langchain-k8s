# Operations: Deployment

This document covers local development setup, production cluster setup, and example Kubernetes manifests.

**Source:** `k8s/sandbox-template.yaml`, `k8s/sandbox-router.yaml`, `scripts/kind-setup.sh`

## Local Development Setup

### Prerequisites

- `docker` installed
- `kubectl` installed
- `kind` (Kubernetes in Docker) installed

### Quick Setup with Kind

The repository includes a `kind-setup.sh` script that:

1. Creates a local Kind cluster named `agent-sandbox`
2. Installs the agent-sandbox controller
3. Deploys example manifests

```bash
# From repository root
./scripts/kind-setup.sh

# Verify cluster is running
kubectl cluster-info
kubectl get nodes
```

**What it creates:**

- Kind cluster: `agent-sandbox`
- Namespace: `agent-sandbox-system` (controller)
- Namespace: `default` (for your sandboxes)
- SandboxTemplate: `python-sandbox-template`
- Gateway API (if supported): `sandbox-gateway`

### Testing Locally

Once the cluster is running:

```python
# In your Python script
from langchain_k8s import KubernetesSandbox

backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="default",
)

with backend:
    result = backend.execute("echo hello from Kind cluster")
    print(result.output)
```

### Cleanup

```bash
./scripts/kind-teardown.sh
```

## Production Cluster Setup

### Prerequisites

1. **Running Kubernetes cluster** (1.20+)
2. **agent-sandbox controller installed**

   ```bash
   # Install the controller (from kubernetes-sigs/agent-sandbox repo)
   kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v0.4.6/controller.yaml
   ```

3. **Namespace for sandboxes** (default: `agent-sandbox-system`)

   ```bash
   kubectl create namespace agent-sandbox-system
   ```

### Install SandboxTemplate

Apply a `SandboxTemplate` CRD that defines the pod spec for your sandboxes.

**Example: Python runtime**

```yaml
apiVersion: agents.x-k8s.io/v1alpha1
kind: SandboxTemplate
metadata:
  name: python-sandbox-template
  namespace: agent-sandbox-system
spec:
  template:
    spec:
      containers:
        - name: sandbox
          image: python:3.12-slim
          resources:
            requests:
              memory: "128Mi"
              cpu: "100m"
            limits:
              memory: "512Mi"
              cpu: "500m"
          volumeMounts:
            - name: workspace
              mountPath: /workspace
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            readOnlyRootFilesystem: true
      volumes:
        - name: workspace
          emptyDir: {}
      securityContext:
        fsGroup: 1000
```

**Example: Node.js runtime**

```yaml
apiVersion: agents.x-k8s.io/v1alpha1
kind: SandboxTemplate
metadata:
  name: node-sandbox-template
  namespace: agent-sandbox-system
spec:
  template:
    spec:
      containers:
        - name: sandbox
          image: node:20-slim
          resources:
            requests:
              memory: "256Mi"
              cpu: "200m"
            limits:
              memory: "1Gi"
              cpu: "1000m"
          volumeMounts:
            - name: workspace
              mountPath: /workspace
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            readOnlyRootFilesystem: true
      volumes:
        - name: workspace
          emptyDir: {}
      securityContext:
        fsGroup: 1000
```

Save as `sandbox-template.yaml` and apply:

```bash
kubectl apply -f sandbox-template.yaml
```

### Deploy Agent Service

Deploy your agent service in a separate deployment, configured with sticky sessions:

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
      serviceAccountName: agent-sa
      containers:
        - name: agent
          image: my-agent:latest
          env:
            - name: SANDBOX_NAMESPACE
              value: agent-sandbox-system
            - name: SANDBOX_TEMPLATE
              value: python-sandbox-template
          ports:
            - containerPort: 8000
          resources:
            requests:
              memory: "256Mi"
              cpu: "100m"
            limits:
              memory: "1Gi"
              cpu: "500m"
---
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
      targetPort: 8000
```

### RBAC Setup

The agent service needs permissions to create and manage sandboxes:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agent-sa
  namespace: default
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: agent-role
rules:
  - apiGroups: ["agents.x-k8s.io"]
    resources: ["sandboxes", "sandboxclaims"]
    verbs: ["create", "delete", "get", "list", "patch", "update", "watch"]
  - apiGroups: ["agents.x-k8s.io"]
    resources: ["sandboxtemplates"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["create", "delete", "get", "list"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["pods/portforward"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: agent-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: agent-role
subjects:
  - kind: ServiceAccount
    name: agent-sa
    namespace: default
```

## Connection Modes in Production

### Gateway API Mode

If your cluster has Gateway API installed:

```yaml
apiVersion: gateway.networking.k8s.io/v1beta1
kind: Gateway
metadata:
  name: sandbox-gateway
  namespace: agent-sandbox-system
spec:
  gatewayClassName: cilium # or your Gateway implementation
  listeners:
    - name: sandbox-api
      protocol: HTTP
      port: 9090
```

Apply and use in your agent:

```python
from langchain_k8s import KubernetesSandbox

backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    gateway_name="sandbox-gateway",
)
```

### InCluster Mode

If your agent service runs inside the cluster:

```python
from langchain_k8s import KubernetesSandbox
from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    connection_config=SandboxInClusterConnectionConfig(),
)
```

## Monitoring & Observability

### Check Sandboxes

```bash
# List all sandboxes
kubectl get sandboxes -n agent-sandbox-system

# Get details
kubectl describe sandbox <name> -n agent-sandbox-system

# View logs
kubectl logs -n agent-sandbox-system -l app=<pod-name>
```

### Resource Usage

```bash
# Pod resource usage
kubectl top pods -n agent-sandbox-system

# Node resource usage
kubectl top nodes
```

### Events

```bash
# Recent events
kubectl get events -n agent-sandbox-system --sort-by='.lastTimestamp'
```

## Scaling Considerations

### Cold-Start Latency

By default, pods are created on-demand (cold-start ~5-10 seconds). To reduce latency:

1. **Use warm pools** (controller feature):

   ```python
   from k8s_agent_sandbox.models import WarmPool

   backend = KubernetesSandbox(
       template_name="python-sandbox-template",
       namespace="agent-sandbox-system",
       warmpool=WarmPool(pool_size=10, idle_timeout=300),
   )
   ```

2. **Pre-create sandboxes** for predictable load

### Resource Limits

Set appropriate resource requests/limits in your `SandboxTemplate`:

```yaml
resources:
  requests:
    memory: "128Mi" # Minimum guaranteed
    cpu: "100m"
  limits:
    memory: "512Mi" # Maximum allowed
    cpu: "500m"
```

**Tuning:**

- Too low limits → OOMKilled, slow execution
- Too high limits → resource waste, scheduling delays

### Sticky Sessions for Horizontal Scaling

When scaling to multiple replicas, ensure sticky sessions are configured:

```yaml
# Kubernetes Service
sessionAffinity: ClientIP
sessionAffinityConfig:
  clientIP:
    timeoutSeconds: 3600

# OR Ingress with cookie affinity
nginx.ingress.kubernetes.io/affinity: cookie
```

See [Architecture: Enterprise](../architecture/enterprise.md) for details.

## Example: Complete Production Setup

Minimal production setup with agent service + gateway:

1. **Create namespace**

   ```bash
   kubectl create namespace agent-sandbox-system
   ```

2. **Install controller**

   ```bash
   kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v0.4.6/controller.yaml
   ```

3. **Create SandboxTemplate**

   ```bash
   kubectl apply -f - <<EOF
   apiVersion: agents.x-k8s.io/v1alpha1
   kind: SandboxTemplate
   metadata:
     name: python-sandbox-template
     namespace: agent-sandbox-system
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
   EOF
   ```

4. **Create agent service + RBAC**

   ```bash
   kubectl apply -f agent-deployment.yaml
   ```

5. **Verify**
   ```bash
   kubectl get pods -n default
   kubectl get pods -n agent-sandbox-system
   ```

## Troubleshooting

### "No SandboxTemplate found"

```bash
# Check templates
kubectl get sandboxtemplates -n agent-sandbox-system

# If empty, apply the template
kubectl apply -f sandbox-template.yaml
```

### "Connection refused"

```bash
# Check controller is running
kubectl get pods -n agent-sandbox-system
kubectl logs -n agent-sandbox-system -l app=agent-sandbox-controller

# Check service exists
kubectl get svc -n agent-sandbox-system
```

### "Permission denied"

```bash
# Check RBAC is configured
kubectl get clusterrolebindings | grep agent

# Check service account
kubectl get sa -n default agent-sa
kubectl get rolebindings -n default
```

### Pod stuck in Pending

```bash
# Check node resources
kubectl describe node

# Check events
kubectl describe pod <pod-name> -n agent-sandbox-system

# May need to increase cluster size or adjust resource limits
```

## Further Reading

- [Workflows: Connection](../workflows/connection.md) — Connection modes and configuration
- [Architecture: Enterprise](../architecture/enterprise.md) — Sticky sessions, scaling
- [Operations: Testing](testing.md) — Local integration tests with Kind
- `scripts/kind-setup.sh` — Actual setup script source code
- `k8s/sandbox-template.yaml` — Example manifest in repository
