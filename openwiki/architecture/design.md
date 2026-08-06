# Architecture: Design

## Overview

`KubernetesSandbox` is a lightweight wrapper around the `k8s-agent-sandbox` SDK's `SandboxClient` that implements the LangChain `BaseSandbox` / `SandboxBackendProtocol` contract. It lets LangChain agents execute shell commands and perform file operations inside Kubernetes pods.

**Key design goals:**

- Lazy initialization (never contact the cluster in `__init__`)
- Thread-safe (support multi-threaded agent environments)
- Flexible lifecycle (persistent pods vs ephemeral pods)
- Transparent error handling (map SDK exceptions to `FileOperationError` types)
- Filesystem-relative operations (virtual paths, write policies)

## Source Code Map

| File                            | Purpose                                                                 |
| ------------------------------- | ----------------------------------------------------------------------- |
| `src/langchain_k8s/sandbox.py`  | Main `KubernetesSandbox` class (2,000+ lines)                           |
| `src/langchain_k8s/__init__.py` | Public API: exports `KubernetesSandbox` and `create_kubernetes_sandbox` |
| `src/langchain_k8s/proxy.py`    | Kubernetes client NO_PROXY monkey-patch (infrastructure only)           |
| `src/langchain_k8s/_version.py` | Version constant                                                        |
| `tests/conftest.py`             | Shared mock client factory and fixtures                                 |
| `tests/unit/test_sandbox.py`    | Comprehensive unit tests (~2,000 lines)                                 |
| `specs/plans/foundation.md`     | Original architectural decisions and rationale                          |

## Class Hierarchy

```
deepagents.backends.sandbox.BaseSandbox
└── langchain_k8s.KubernetesSandbox
    ├── implements: execute(), start(), stop(), __enter__, __exit__
    ├── overrides: read(), write(), edit(), ls_info(), glob_info(), grep_raw()
    └── provides: upload_files(), download_files() (custom implementations)
```

`KubernetesSandbox` extends `BaseSandbox`, which provides default implementations of all LangChain sandbox tools (read, write, edit, ls, glob, grep, upload, download). These default implementations call the abstract `execute()` method to run shell commands.

`KubernetesSandbox` overrides certain methods to:

- Support virtual paths (`virtual_mode=True`)
- Enforce write policies (`allow_prefixes`)
- Handle base64 file I/O more efficiently

## Constructor Design

```python
class KubernetesSandbox(BaseSandbox):
    def __init__(
        self,
        *,
        # Ecosystem-standard mode: pass a pre-created Sandbox handle
        sandbox: Sandbox | None = None,
        # OR config-based mode: pass template name + connection params
        template_name: str | None = None,
        namespace: str = "default",
        gateway_name: str | None = None,
        api_url: str | None = None,
        connection_config: SandboxConnectionConfig | None = None,
        # Lifecycle strategy
        reuse_sandbox: bool = True,
        skip_cleanup: bool = False,
        # Execution
        max_output_size: int = 1_048_576,  # 1 MB
        command_timeout: int | None = 300,  # 5 minutes
        warmpool: WarmPool | None = None,
        # Enterprise features
        allow_prefixes: list[str] | None = None,
        virtual_mode: bool = False,
        root_dir: str | None = None,
    ) -> None:
```

**Key principles:**

- Never contacts the cluster in `__init__` (lazy initialization)
- Stores all configuration for later use
- Initializes threading lock for thread-safe state mutations
- Two constructor modes: ecosystem-standard (pass `sandbox=handle`) or config-based (pass `template_name`)

## Two Constructor Modes

### 1. Ecosystem-Standard Mode (Recommended for Production)

Pass a pre-created `Sandbox` handle from `SandboxClient`:

```python
from k8s_agent_sandbox import SandboxClient
from langchain_k8s import KubernetesSandbox

client = SandboxClient(connection_config=...)
handle = client.create_sandbox(template="python-sandbox-template")
backend = KubernetesSandbox(sandbox=handle)

result = backend.execute("echo hello")
client.delete_sandbox(handle.claim_name)  # caller manages lifecycle
```

**Advantages:**

- Caller controls sandbox lifecycle
- Fits standard `BaseSandbox` patterns
- No lazy initialization complexity
- Easy to reuse the same sandbox across multiple backends

**When to use:**

- Production deployments where you want explicit lifecycle control
- Thread-scoped graph factories (see [Workflows: Lifecycle](../workflows/lifecycle.md))

### 2. Config-Based Mode (Convenience)

Pass `template_name` and connection parameters:

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
)

with backend:
    result = backend.execute("echo hello")
# Automatically cleaned up
```

**Advantages:**

- Simple one-liner setup
- Built-in `start()`/`stop()` lifecycle
- Context manager support
- Backward compatible with older code

**How it works:**

1. Constructor stores template name and connection parameters
2. On first `execute()` call, `_ensure_sandbox()` creates the pod via `SandboxClient`
3. `stop()` or context manager exit deletes the pod

**When to use:**

- Quick prototyping or development
- Simple scripts where you don't need explicit lifecycle control

## Lifecycle Strategies

### Persistent Sandbox (`reuse_sandbox=True`, default)

**One pod is created lazily and reused across all `execute()` calls.**

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    reuse_sandbox=True,  # default
)

# First execute() creates the pod
result1 = backend.execute("echo hello")
# Same pod reused for subsequent calls
result2 = backend.execute("ls -la")
result3 = backend.execute("cat /tmp/data.txt")

backend.stop()  # pod is deleted
```

**Characteristics:**

- Low latency (no cold-start overhead per call)
- Filesystem state persists between calls (shared workspace)
- Auto-reconnects if pod dies (retry logic)
- Must explicitly call `stop()` or use context manager
- Good for long-lived agent sessions

**Thread-safety:**

- Uses `threading.Lock` to gate `_ensure_sandbox()` — only one thread creates the pod
- Once created, all threads can safely call `execute()` on the same pod
- Auto-reconnect logic: if connection fails, lock gates a single retry

### Ephemeral Sandbox (`reuse_sandbox=False`)

**A fresh pod is created for each `start()`/`stop()` cycle.**

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    reuse_sandbox=False,
)

# Fresh pod A created and destroyed after this call
result1 = backend.execute("echo hello")
# Pod A destroyed

# Fresh pod B created for next call
result2 = backend.execute("ls -la")
# Pod B destroyed
```

**Characteristics:**

- Maximum isolation between calls
- No state leakage between invocations
- Higher latency (cold-start per call)
- Cleaner resource lifecycle (each call owns its pod)

## Error Handling

### Error Classification

`KubernetesSandbox` maps stderr patterns to `FileOperationError` types from the `deepagents` protocol:

- **`FileNotFoundError`**: Path doesn't exist (`No such file or directory`, `cannot access`)
- **`IsADirectoryError`**: Path is a directory when a file expected
- **`NotADirectoryError`**: Path is not a directory
- **`PermissionError`**: Permission denied (`Permission denied`)
- **`ValueError`**: Invalid operation (write policy violation, path traversal, etc.)
- **`TimeoutError`**: Command timed out

Method: `_classify_error(output: str) -> FileOperationError | None`

### Reconnection Logic

In persistent mode (`reuse_sandbox=True`), `execute()` implements auto-reconnect:

```python
try:
    result = self._sandbox.commands.run(...)
except ConnectionError:
    # Pod may have died; try once more
    if not self._reconnect_attempted:
        self._reconnect_attempted = True
        self._ensure_sandbox(force=True)  # force recreate
        result = self._sandbox.commands.run(...)
    else:
        raise
```

This ensures brief pod outages don't kill the agent session.

## File Operations

### Upload Files

```python
backend.upload_files([
    ("/workspace/script.py", b"print('hello')\n"),
    ("/workspace/data.csv", b"a,b,c\n1,2,3\n"),
])
```

**Implementation:**

- Encodes each file as base64
- Constructs a shell command to decode and write: `echo '<base64>' | base64 -d > /path`
- Executes via `execute()` to send to the pod

**Why base64?**

- Avoids binary blob limitations in the SDK
- Works with shell commands (POSIX-friendly)
- No need for special binary protocols

### Download Files

```python
results = backend.download_files(["/workspace/output.txt"])
print(results[0].content)  # bytes
```

**Implementation:**

- Constructs a shell command: `base64 /path` (outputs base64)
- Executes, captures base64 output
- Decodes base64 to bytes
- Returns `FileDownloadResponse` objects

**Why base64?**

- Safer than shell output redirection
- Handles binary files correctly
- No need to rely on `/tmp` mount points

## Path Operations

### Virtual Filesystem (`virtual_mode=True`)

When enabled, all paths are resolved under `root_dir`:

```python
backend = KubernetesSandbox(
    template_name="...",
    namespace="...",
    virtual_mode=True,
    root_dir="/workspace",
)

# Virtual path "/src/main.py" resolves to "/workspace/src/main.py"
backend.write("/src/main.py", b"...")
# Actual file: /workspace/src/main.py

# Path traversal is blocked
backend.read("../../../etc/passwd")  # raises ValueError
```

**Resolution logic** (`_resolve_virtual_path(path: str) -> str`):

1. Check for `..` and `~` (reject if found)
2. Normalize to absolute (prepend `/` if needed)
3. Concatenate with `root_dir`
4. Apply `posixpath.normpath()` to clean up

**Used by:**

- `read()`, `write()`, `edit()`
- `ls_info()`, `glob_info()`, `grep_raw()`
- `upload_files()`, `download_files()`

### Write Policy (`allow_prefixes`)

When set, only paths under specified prefixes are writable:

```python
backend = KubernetesSandbox(
    template_name="...",
    namespace="...",
    allow_prefixes=["/workspace/", "/tmp/"],
)

backend.write("/workspace/file.txt", b"OK")  # allowed
backend.write("/tmp/file.txt", b"OK")       # allowed
backend.write("/etc/passwd", b"DENIED")     # raises ValueError
```

**Logic** (`_check_allow_prefix(path: str) -> str | None`):

1. If `allow_prefixes is None`, no restrictions (default)
2. If any allowed prefix matches the start of the path, allow it
3. Otherwise, return an error message (no command is executed)

**Scope:**

- Only affects `write()` and `edit()` operations
- Does NOT block `execute("echo bad > /etc/passwd")` (tool-level, not OS-level)
- For system-level protection, use Kubernetes pod `securityContext`

## Thread Safety

`KubernetesSandbox` is thread-safe for concurrent `execute()` calls in persistent mode:

- **Initialization lock** (`self._init_lock`): Gates `_ensure_sandbox()` — only one thread creates the pod
- **Reconnect flag** (`self._reconnect_attempted`): Prevents double-reconnection
- **Sandbox handle** (`self._sandbox`): Once created, all threads safely use it (the SDK handles thread-safe communication to the pod)

**Safe usage:**

```python
backend = KubernetesSandbox(...)
backend.start()

# Multiple threads can call execute() concurrently
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(backend.execute, f"echo {i}") for i in range(10)]
    results = [f.result() for f in futures]

backend.stop()
```

**Unsafe usage:**

```python
backend = KubernetesSandbox(...)

# Calling start() and stop() concurrently from different threads
# is not supported — use a single thread for lifecycle management
```

## Command Execution

### Wrapping Commands in `sh -c`

All commands are wrapped in `sh -c '...'` before sending to the pod:

```python
backend.execute("echo hello && echo world")
# Becomes: sh -c 'echo hello && echo world'
```

**Why?**

- Ensures pipes (`|`), redirects (`>`), and operators (`&&`, `||`) work correctly
- Provides full POSIX shell semantics
- Consistent across different shell implementations

### Command Timeout

Commands are wrapped with a timeout (default 300 seconds / 5 minutes):

```python
backend.execute("long_running_task.py", timeout=600)
# Timeout: 600 seconds / 10 minutes
```

**Default timeout**: 300 seconds (configurable via `command_timeout` parameter)

### Output Truncation

Large command output is truncated to `max_output_size` (default 1 MB):

```python
backend = KubernetesSandbox(
    ...,
    max_output_size=1_048_576,  # 1 MB
)
```

## Sandbox Warm Pools

When using the `k8s-agent-sandbox` controller's warm pool feature, pass a `WarmPool` object:

```python
from k8s_agent_sandbox.models import WarmPool

backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    warmpool=WarmPool(pool_size=5, idle_timeout=300),
)
```

**What it does:**

- Controller maintains a pool of pre-warmed pods
- When you request a sandbox, you get one from the pool (faster)
- Idle pods are destroyed after timeout
- Reduces cold-start latency for high-throughput scenarios

## Testing the Implementation

See [Operations: Testing](../operations/testing.md) for:

- Unit test patterns with mocked SDK
- Integration test setup
- How to run the test suite

## Further Reading

- [Workflows: Lifecycle](../workflows/lifecycle.md) — Persistent vs ephemeral sandboxes, thread-scoped patterns
- [Workflows: Connection](../workflows/connection.md) — Connection modes and configuration
- [Architecture: Enterprise](../enterprise.md) — Path policies and virtual filesystem details
- `specs/plans/foundation.md` — Original design rationale and decisions
