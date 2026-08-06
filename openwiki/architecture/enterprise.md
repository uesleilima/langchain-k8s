# Architecture: Enterprise Features

This document describes three enterprise-grade features that enable secure, scalable agent deployments: path access policies, virtual filesystem mode, and sticky session configuration for horizontal scaling.

**Source:** `src/langchain_k8s/sandbox.py` (policy checks and virtual path resolution), `specs/plans/enterprise-features.md` (design rationale)

## Path Access Policy (`allow_prefixes`)

### Purpose

Enforce a **tool-level write policy** that restricts which directories agents can write to. This is a defense-in-depth measure to prevent agent code from accidentally (or maliciously) writing to sensitive paths.

**Important:** This is a tool-level policy only. It does NOT prevent shell commands like `execute("echo bad > /etc/passwd")`. For system-level protection, use Kubernetes pod `securityContext` (e.g., `readOnlyRootFilesystem`).

### Configuration

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    allow_prefixes=["/workspace/", "/tmp/"],
)
```

**Behavior:**

- `allow_prefixes=None` (default): No restrictions; agents can write anywhere
- `allow_prefixes=["/workspace/", "/tmp/"]`: Only paths under these prefixes are writable
- Any path not under an allowed prefix returns an error **without executing a command**

### Example

```python
# With allow_prefixes=["/workspace/", "/tmp/"]

backend.write("/workspace/file.txt", b"OK")  # ✅ allowed
backend.write("/tmp/file.txt", b"OK")       # ✅ allowed
backend.write("/tmp/subdir/file.txt", b"OK") # ✅ allowed (under /tmp/)

backend.write("/etc/passwd", b"DENIED")     # ❌ rejected (error, no command runs)
backend.write("/home/user/file.txt", b"DENIED") # ❌ rejected
backend.edit("/var/log/app.log", ...)       # ❌ rejected (edit also checks policy)
```

### How It Works

1. **Prefix normalization** (in constructor):
   - Input: `["/workspace", "/tmp/"]`
   - Normalized: `("/workspace/", "/tmp/")` (all prefixes end with `/`)
   - Stored as `self._allow_prefixes: tuple[str, ...] | None`

2. **Policy check** (in `write()` and `edit()`):
   - After virtual path resolution (if enabled)
   - Check if the **resolved absolute path** starts with any allowed prefix
   - If no match, return an error response without executing a command

```python
def _check_allow_prefix(self, file_path: str) -> str | None:
    if self._allow_prefixes is None:
        return None  # no restrictions
    for prefix in self._allow_prefixes:
        if file_path.startswith(prefix):
            return None  # allowed
    return f"Path {file_path!r} is not under any allowed prefix: {self._allow_prefixes}"
```

### Common Writable Locations

If you don't set `allow_prefixes`, writable locations depend on the container's filesystem permissions:

| Directory      | Writable?       | Notes                                                |
| -------------- | --------------- | ---------------------------------------------------- |
| `/tmp`         | ✅ Yes          | Always writable; good default for `root_dir`         |
| `/home/<user>` | ✅ Yes          | If container runs as that user                       |
| `/workspace`   | ✅ Yes          | If mounted as `emptyDir` in the `SandboxTemplate`    |
| `/root`        | ❌ Typically no | Unless container runs as root                        |
| `/etc`         | ❌ No           | Root filesystem (read-only in production containers) |
| `/usr/local`   | ❌ Typically no | System directories                                   |

### Making a Custom Directory Writable

In your `SandboxTemplate`, mount an `emptyDir` volume:

```yaml
apiVersion: extensions.agents.x-k8s.io/v1alpha1
kind: SandboxTemplate
metadata:
  name: python-sandbox-template
  namespace: agent-sandbox-system
spec:
  podTemplate:
    spec:
      containers:
        - name: python-runtime
          image: registry.k8s.io/agent-sandbox/python-runtime-sandbox:v0.4.6
          volumeMounts:
            - name: workspace
              mountPath: /workspace
      volumes:
        - name: workspace
          emptyDir: {}
```

Now `/workspace` is writable regardless of the container's root filesystem permissions, and you can safely use:

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    root_dir="/workspace",
    allow_prefixes=["/workspace/"],
)
```

### Combining with Virtual Filesystem

When both `virtual_mode=True` and `allow_prefixes` are set, the policy check runs **after** virtual path resolution:

```python
backend = KubernetesSandbox(
    template_name="...",
    namespace="...",
    virtual_mode=True,
    root_dir="/workspace",
    allow_prefixes=["/workspace/"],
)

# Virtual path "/src/main.py"
# → Resolves to: /workspace/src/main.py
# → Check: starts with /workspace/? Yes ✅
backend.write("/src/main.py", b"...")  # ✅ allowed
```

## Virtual Filesystem (`virtual_mode`)

### Purpose

Provide **path containment** semantics similar to `FilesystemBackend(virtual_mode=True)`. All file-operation paths are resolved under a `root_dir`, preventing agents from escaping via `..` or `~`.

**Use case:** Multi-tenant deployments where agents must be confined to isolated directories.

### Configuration

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    virtual_mode=True,
    root_dir="/workspace",
)
```

**Defaults:**

- `virtual_mode=False` (disabled by default)
- `root_dir=None` — when `virtual_mode=True` and `root_dir` is `None`, defaults to `/tmp`

### Example

```python
# With virtual_mode=True, root_dir="/workspace"

backend.write("/src/main.py", b"...")
# Virtual path "/src/main.py" → Resolved path: /workspace/src/main.py

backend.read("/data/input.csv")
# Virtual path "/data/input.csv" → Resolved path: /workspace/data/input.csv

backend.upload_files([("/config/app.yaml", content)])
# Virtual path "/config/app.yaml" → Resolved path: /workspace/config/app.yaml
```

### Path Resolution Algorithm

The `_resolve_virtual_path(path: str) -> str` method:

1. **If virtual mode is disabled**, return path unchanged
2. **Check for traversal attempts**: If path contains `..` or starts with `~`, raise `ValueError`
3. **If already resolved** (the path already sits under `root_dir`), normalize, re-check containment, and return — do **not** re-prefix
4. **Normalize to absolute**: If path doesn't start with `/`, prepend `/`
5. **Append to root_dir**: `resolved = root_dir + normalized_path`
6. **Clean with normpath**, then **re-check containment**: reject anything that normalizes to outside `root_dir`

```python
def _resolve_virtual_path(self, path: str) -> str:
    if not self._virtual_mode or self._root_dir is None:
        return path

    if ".." in path or path.startswith("~"):
        msg = f"Path traversal not allowed: {path!r}"
        raise ValueError(msg)

    root_normalized = posixpath.normpath(self._root_dir)

    # Already resolved — validate but don't re-prefix.
    if path.startswith(root_normalized + "/") or path == root_normalized:
        normalized = posixpath.normpath(path)
        if not normalized.startswith(root_normalized + "/") and normalized != root_normalized:
            raise ValueError(f"Path {path!r} resolves outside root directory {self._root_dir!r}")
        return normalized

    vpath = path if path.startswith("/") else "/" + path
    resolved = self._root_dir.rstrip("/") + vpath

    normalized = posixpath.normpath(resolved)
    if not normalized.startswith(root_normalized + "/") and normalized != root_normalized:
        raise ValueError(f"Path {path!r} resolves outside root directory {self._root_dir!r}")

    return normalized
```

**Step 3 is load-bearing, not an optimisation.** `BaseSandbox.write()` internally calls `self.upload_files()`, and both layers resolve. Without the short-circuit, `/src/main.py` silently becomes `/workspace/workspace/src/main.py`. Any new override that resolves and then delegates to `super()` inherits this constraint.

### Blocked Traversal Attempts

```python
backend = KubernetesSandbox(
    ...,
    virtual_mode=True,
    root_dir="/workspace",
)

backend.read("../../../etc/passwd")  # ❌ result.error: "Path traversal not allowed: ..."
backend.read("~/.ssh/id_rsa")        # ❌ result.error: "Path traversal not allowed: ..."
backend.write("./etc/passwd", "..")  # ✅ OK (resolves to /workspace/etc/passwd)
```

`_resolve_virtual_path` raises `ValueError`, but the public file-operation methods catch it and return it in the result's `error` field — they do not propagate the exception to the caller.

### Affected Methods

Virtual path resolution applies to all file-operation methods:

- `read(path)` — Read file content
- `write(path, content)` — Write file
- `edit(path, ...)` — Edit file
- `ls(path)` — List directory
- `glob(pattern, path)` — Glob pattern
- `grep(pattern, ...)` — Grep files
- `upload_files([(path, content), ...])` — Upload files
- `download_files([path, ...])` — Download files

**Note:** `execute(command)` is NOT affected by virtual mode. Commands run as-is in the pod.

### Combined with `allow_prefixes`

When both are set, the order of operations is:

1. **Resolve virtual path** (if enabled)
2. **Check allow prefix** (if enabled)

Example:

```python
backend = KubernetesSandbox(
    ...,
    virtual_mode=True,
    root_dir="/workspace",
    allow_prefixes=["/workspace/"],
)

# Step 1: Resolve "/src/main.py" → "/workspace/src/main.py"
# Step 2: Check "/workspace/src/main.py" starts with "/workspace/"? Yes ✅
backend.write("/src/main.py", b"...")  # ✅ allowed
```

## Horizontal Scaling & Sticky Sessions

### Problem

When deploying a service that uses `KubernetesSandbox` behind a load balancer with multiple replicas, sandbox state (pod, port-forward, filesystem) is held **in-process**. Different service instances cannot share a sandbox.

```
Load Balancer
    ├── Service Pod A (KubernetesSandbox instance + sandbox state)
    ├── Service Pod B (KubernetesSandbox instance + sandbox state)
    └── Service Pod C (KubernetesSandbox instance + sandbox state)

Request 1: /service?user=alice → Pod A (creates sandbox + port-forward)
Request 2: /service?user=alice → Pod B (different instance, no sandbox state!)
```

Without sticky sessions, subsequent requests from the same user hit different pods and lose sandbox state.

### Solution: Sticky Sessions

Ensure requests from the same user/session are routed to the **same service instance**.

#### Option 1: Kubernetes Service with Session Affinity

```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-agent-service
spec:
  sessionAffinity: ClientIP
  sessionAffinityConfig:
    clientIP:
      timeoutSeconds: 3600 # 1 hour
  selector:
    app: my-agent
  ports:
    - port: 80
      targetPort: 8080
```

**How it works:**

- `sessionAffinity: ClientIP` — Kubernetes routes all requests from the same client IP to the same Pod
- `timeoutSeconds` — Session affinity expires after this idle time

**Limitations:**

- Client IP is what matters (not user ID)
- All users behind a corporate proxy share one IP (wrong grouping)
- Not suitable for browser-based clients (IPs change)

#### Option 2: Ingress with Cookie-Based Affinity

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-agent-ingress
  annotations:
    nginx.ingress.kubernetes.io/affinity: cookie
    nginx.ingress.kubernetes.io/affinity-mode: persistent
spec:
  ingressClassName: nginx
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /agent
            pathType: Prefix
            backend:
              service:
                name: my-agent-service
                port:
                  number: 80
```

**How it works:**

- Nginx Ingress sets a cookie on the first request
- Subsequent requests with the same cookie route to the same Pod
- Cookie is persistent across sessions

**Advantages:**

- Works with browser clients
- Per-user session affinity (cookie = session)
- Survives IP changes (e.g., mobile roaming)

#### Option 3: Application-Level Session Management

If you control the client, include a sticky session ID in requests:

```python
# Client side
response = requests.post(
    "https://api.example.com/agent/chat",
    json={"message": "..."},
    headers={"X-Session-ID": "user-abc-123"},
)
```

```python
# Server side (e.g., LangServe or similar)
from fastapi import FastAPI, Header

app = FastAPI()

@app.post("/agent/chat")
async def chat(message: str, x_session_id: str = Header(...)):
    # Use x_session_id to route to consistent backend service instance
    # (via load balancer affinity, consistent hashing, etc.)
    ...
```

### Best Practice

For production agent services:

1. **Use Ingress with cookie affinity** (most robust)
2. **Set appropriate timeout** (match your session lifetime, e.g., 3600s for 1-hour sessions)
3. **Consider warm pools** in `k8s-agent-sandbox` controller to reduce cold-start per user

## Further Reading

- [Workflows: Lifecycle](../workflows/lifecycle.md) — Thread-scoped sandboxes for production
- [Operations: Deployment](../operations/deployment.md) — Example manifests
- `specs/plans/enterprise-features.md` — Detailed design rationale
