# Architecture: Design

## Overview

`KubernetesSandbox` is a lightweight wrapper around the `k8s-agent-sandbox` SDK's `SandboxClient` that implements the LangChain `BaseSandbox` / `SandboxBackendProtocol` contract. It lets LangChain agents execute shell commands and perform file operations inside Kubernetes pods.

**Key design goals:**

- Lazy initialization (never contact the cluster in `__init__`)
- Thread-safe (support multi-threaded agent environments)
- Flexible lifecycle (long-lived pods, or torn down and reconnected by `claim_name`)
- Transparent error handling (map SDK exceptions to `FileOperationError` types)
- Filesystem-relative operations (virtual paths, write policies)

## Source Code Map

| File                            | Purpose                                                                 |
| ------------------------------- | ----------------------------------------------------------------------- |
| `src/langchain_k8s/sandbox.py`  | `KubernetesSandbox` class and the `create_kubernetes_sandbox` factory (~1,300 lines) |
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
    ├── overrides: read(), write(), edit(), ls(), glob(), grep()
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

### Default: one lazily-created pod, with auto-reconnect (`reuse_sandbox=True`)

**One pod is created lazily and reused across all `execute()` calls.** Pod reuse is how config-based mode always works; the flag additionally enables reconnect-and-retry on connection failure.

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

### Disabling auto-reconnect (`reuse_sandbox=False`)

**`reuse_sandbox` controls error recovery, not pod lifetime.** There is no per-invocation pod mode.

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    reuse_sandbox=False,
)

# One pod, created lazily, reused for both calls — same as the default.
result1 = backend.execute("echo hello")
result2 = backend.execute("ls -la")

# The difference: if the connection to the pod drops, this raises
# instead of silently provisioning a replacement and retrying.
```

**Characteristics:**

- Connection failures surface to the caller rather than being papered over
- No hidden pod churn, so latency and resource use stay predictable
- Appropriate when the caller has its own retry or supervision strategy

`self._reuse_sandbox` is read at exactly one place in the source (`sandbox.py:467`), inside the `except` branch of `execute()`.

## Error Handling

### Error Classification

`FileOperationError` in the `deepagents` protocol is a **string literal type**, not an exception class. `_classify_error` maps stderr patterns to exactly three values:

- **`"permission_denied"`** — stderr contains `Permission denied`
- **`"is_directory"`** — the path is a directory where a file was expected
- **`"file_not_found"`** — the fallback for everything else

Method: `_classify_error(output: str) -> FileOperationError` (`sandbox.py:1264`) — it always returns a value, never `None`.

Path validation is separate: `_validate_path` (`sandbox.py:1248`) returns the literal `"invalid_path"` for empty or relative paths, before any command is dispatched.

### Reconnection Logic

When `reuse_sandbox=True` (the default) **and** the backend owns the lifecycle (config-based mode), `execute()` retries once:

```python
try:
    resp = self._run(command, timeout=effective_timeout)
except Exception as exc:
    if self._reuse_sandbox and self._owns_lifecycle:
        self._destroy_sandbox()   # tear down the dead sandbox
        self._ensure_sandbox()    # provision a replacement
        resp = self._run(command, timeout=effective_timeout)
    else:
        raise
```

There is no `_reconnect_attempted` flag and `_ensure_sandbox()` takes no `force` argument — teardown is what makes the subsequent `_ensure_sandbox()` provision a new sandbox, because it clears `_started`. The retry is not recursive, so a second consecutive failure propagates.

This ensures brief pod outages don't kill the agent session. Note that it is disabled in handle mode regardless of `reuse_sandbox`, since the caller owns the Kubernetes resources.

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

1. Short-circuit if the path already sits under `root_dir` — validate, but do not re-prefix
2. Check for `..` and `~` (reject if found)
3. Normalize to absolute (prepend `/` if needed)
4. Concatenate with `root_dir`
5. Apply `posixpath.normpath()`, then re-check that the result is still contained under `root_dir`

Step 1 is load-bearing, not an optimisation: `BaseSandbox.write()` internally calls `self.upload_files()`, which resolves again. Without the short-circuit, `/src/main.py` would become `/tmp/tmp/src/main.py`.

**Used by:**

- `read()`, `write()`, `edit()`
- `ls()`, `glob()`, `grep()`
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

- **Initialization lock** (`self._lock`): Gates `_ensure_sandbox()` and `_destroy_sandbox()` — only one thread creates or tears down the pod. `_ensure_sandbox` double-checks `self._started` outside and inside the lock
- **Sandbox handle** (`self._sandbox`): Once created, all threads safely use it (the SDK handles thread-safe communication to the pod). `execute()` itself is lock-free once the handle exists

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

`warmpool` is a `str | None` — the **name** of a `SandboxWarmPool` resource that already exists in the cluster. Pool size and idle behaviour are properties of that resource, not constructor arguments.

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    warmpool="python-sandbox-pool",
)
```

**What it does:**

- The controller maintains a pool of pre-warmed pods, per the `SandboxWarmPool` spec
- When you request a sandbox, one is adopted from the pool instead of being provisioned cold
- Reduces cold-start latency for high-throughput scenarios

Config-based mode only, and ignored when reconnecting by `claim_name`. The argument is forwarded to the SDK only when non-`None`, so older SDK versions don't choke on the unknown kwarg.

## Testing the Implementation

See [Operations: Testing](../operations/testing.md) for:

- Unit test patterns with mocked SDK
- Integration test setup
- How to run the test suite

## Further Reading

- [Workflows: Lifecycle](../workflows/lifecycle.md) — Pod lifetime, auto-reconnect, thread-scoped patterns
- [Workflows: Connection](../workflows/connection.md) — Connection modes and configuration
- [Architecture: Enterprise](../enterprise.md) — Path policies and virtual filesystem details
- `specs/plans/foundation.md` — Original design rationale and decisions
