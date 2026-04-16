# Plan: Enterprise Features for KubernetesSandbox (v2)

## Context

The KubernetesSandbox was extended with three enterprise features: policy hooks, virtual filesystem mode, and lifecycle management for horizontally-scaled deployments. After initial implementation, two design changes were made:

1. **`deny_prefixes` → `allow_prefixes`**: An allowlist approach instead of a denylist. When `allow_prefixes` is set, ONLY paths with those prefixes are permitted. When unset (`None`), everything is allowed (backward compatible).
2. **`virtual_mode` needs `root_dir`**: Following `FilesystemBackend`'s pattern, virtual mode requires a root directory to anchor paths. All file operation methods resolve virtual paths under `root_dir` before delegating to `super()`.

Feature 3 (`skip_cleanup`) was correctly implemented from the start and unchanged.

---

## Feature 1: Replace `deny_prefixes` with `allow_prefixes`

### Design

- **Removed**: `_DEFAULT_DENY_PREFIXES` constant, `deny_prefixes` parameter, `_check_deny_prefix()` method
- **Added**: `allow_prefixes: list[str] | None = None` parameter
  - `None` (default) → no restrictions, everything allowed (backward compatible)
  - `["/workspace/", "/tmp/"]` → ONLY these prefixes are writable
- **Renamed**: `_check_deny_prefix()` → `_check_allow_prefix()` with inverted logic
- **Same scope**: Only `write()` and `edit()` are gated (tool-level policy). `execute()` is not affected.

### Implementation in `src/langchain_k8s/sandbox.py`

1. Removed `_DEFAULT_DENY_PREFIXES` constant
2. Replaced constructor parameter `deny_prefixes` with `allow_prefixes: list[str] | None = None`
3. In constructor body: normalize `allow_prefixes` (add trailing `/` if missing), store as `self._allow_prefixes: tuple[str, ...] | None`
   - `None` → `self._allow_prefixes = None` (no policy)
   - `["/workspace"]` → `self._allow_prefixes = ("/workspace/",)`
4. Replaced `_check_deny_prefix()` with `_check_allow_prefix()`:

   ```python
   def _check_allow_prefix(self, file_path: str) -> str | None:
       if self._allow_prefixes is None:
           return None  # no restrictions
       for prefix in self._allow_prefixes:
           if file_path.startswith(prefix):
               return None  # allowed
       return f"Path {file_path!r} is not under any allowed prefix: {self._allow_prefixes}"
   ```

5. Updated `write()` and `edit()` overrides to call `_check_allow_prefix()` **after** `_resolve_virtual_path()` so the check runs against the resolved absolute path (not the virtual path). This means with `root_dir="/workspace"` and `allow_prefixes=["/workspace/"]`, a virtual path `/src/main.py` resolves to `/workspace/src/main.py` and passes the allow check.

### Tests: `TestAllowPrefixes` (~9 tests)

- `allow_prefixes=None` (default) allows writes everywhere
- `allow_prefixes=["/workspace/", "/tmp/"]` allows writes under those prefixes
- `allow_prefixes=["/workspace/"]` blocks writes to `/etc/`, `/tmp/`, etc.
- Edit follows same policy as write
- Prefix normalization adds trailing `/`
- Stored as `tuple | None`

---

## Feature 2: `virtual_mode` + `root_dir` — Full Virtual Filesystem

### Design Rationale

Following `FilesystemBackend`'s pattern, `virtual_mode=True` anchors all paths under a `root_dir`. This enables:

- Path containment: agents cannot escape `root_dir`
- Path traversal blocking: `..` and `~` rejected
- Clean virtual path semantics for `CompositeBackend` compatibility
- Native SDK downloads via `client.read(resolved_path)` since we know the actual path

**How BaseSandbox methods embed paths** (determines override strategy):

| Method | Path mechanism | Safe to modify path before `super()`? |
|--------|---------------|---------------------------------------|
| `write()` | base64-encoded JSON via stdin | ✅ Yes |
| `edit()` | base64-encoded JSON via stdin | ✅ Yes |
| `read()` | Raw string interpolation: `file_path = '{file_path}'` | ✅ Yes (resolved path is a clean absolute path under root_dir, no metacharacters) |
| `ls_info()` | Raw string interpolation: `path = '{path}'` | ✅ Yes (same reasoning) |
| `glob_info()` | base64-encoded | ✅ Yes |
| `grep_raw()` | `shlex.quote()` | ✅ Yes |
| `upload_files()` | Custom implementation (already overridden) | ✅ Yes |
| `download_files()` | Custom implementation (already overridden) | ✅ Yes |

**Key insight**: For `read()` and `ls_info()`, which use raw string interpolation, the resolved path is always a clean absolute path like `/workspace/src/main.py` — no single quotes, no shell metacharacters. So calling `super().read(resolved_path)` is safe. The shell injection risk in BaseSandbox only arises from adversarial path content, which our `_resolve_virtual_path()` sanitizes.

### Implementation in `src/langchain_k8s/sandbox.py`

1. **Constructor**: Added `root_dir: str | None = None` and `virtual_mode: bool = False` parameters.
   - `root_dir` defaults to `None`. When `virtual_mode=True` and `root_dir` is `None`, defaults to `"/workspace"`.
   - Stored as `self._root_dir: str | None` and `self._virtual_mode: bool`

2. **New method `_resolve_virtual_path()`**:

   ```python
   def _resolve_virtual_path(self, path: str) -> str:
       if not self._virtual_mode or self._root_dir is None:
           return path
       if ".." in path or path.startswith("~"):
           msg = f"Path traversal not allowed: {path!r}"
           raise ValueError(msg)
       vpath = path if path.startswith("/") else "/" + path
       resolved = self._root_dir.rstrip("/") + vpath
       normalized = posixpath.normpath(resolved)
       root_normalized = posixpath.normpath(self._root_dir)
       if not normalized.startswith(root_normalized + "/") and normalized != root_normalized:
           msg = f"Path {path!r} resolves outside root directory {self._root_dir!r}"
           raise ValueError(msg)
       return normalized
   ```

3. **Overridden all path-accepting methods** to resolve before delegating:
   - `write()` — resolve + allow_prefixes check, then `super().write(resolved, content)`
   - `edit()` — resolve + allow_prefixes check, then `super().edit(resolved, ...)`
   - `read()` — resolve, then `super().read(resolved, offset, limit)`
   - `ls_info()` — resolve, then `super().ls_info(resolved)`
   - `glob_info()` — resolve path arg, then `super().glob_info(pattern, resolved)`
   - `grep_raw()` — resolve path if not None, default to `root_dir` when path=None in virtual mode
   - `upload_files()` — resolve each path in the loop
   - `download_files()` — resolve each path in both `_download_files_shell()` and `_download_files_native()`

### Tests: `TestVirtualMode` (~30 tests)

- Default: `virtual_mode=False`, paths unchanged
- Path resolution: `/src/main.py` → `/workspace/src/main.py`
- Relative path resolution: `src/main.py` → `/workspace/src/main.py`
- Traversal blocked: `../../etc/passwd` → ValueError
- Tilde blocked: `~/.bashrc` → ValueError
- `read()` resolves paths and returns error on traversal
- `write()` resolves paths (verified via base64 payload decoding) and returns error on traversal
- `edit()` resolves paths (verified via base64 payload decoding) and returns error on traversal
- `ls_info()` resolves paths and returns empty on traversal
- `glob_info()` resolves paths and returns empty on traversal
- `grep_raw()` resolves paths, defaults to root_dir when path=None, returns error on traversal
- `upload_files()` resolves paths and blocks traversal
- `download_files()` (shell mode) with `virtual_mode=False`
- `download_files()` (native mode) with `virtual_mode=True` passes resolved path to `client.read()`
- Native download error handling (file_not_found, permission_denied, traversal)
- Combined: `allow_prefixes` + `virtual_mode` checks resolved path

---

## Feature 3: Enterprise Lifecycle Management

### Implementation in `src/langchain_k8s/sandbox.py`

- `skip_cleanup: bool = False` — when `True`, `_destroy_sandbox()` calls `_close_connection()` to release local resources (port-forward, tracing) without deleting the Kubernetes `SandboxClaim`. The sandbox pod continues running.

### Tests: `TestSkipCleanup` (~6 tests)

- `skip_cleanup` default is `False`
- `skip_cleanup=True` calls `_close_connection()` instead of `delete_sandbox()`
- Normal stop calls `delete_sandbox()`
- Context manager respects `skip_cleanup`
- Tolerates `_close_connection()` errors

---

## Feature 4: Sandbox Reconnection & Labels

### Design Rationale

With SDK v0.3.10, `SandboxClient.get_sandbox(claim_name)` can re-attach to an existing sandbox. Combined with `skip_cleanup=True`, this enables full lifecycle management across agent process restarts:

1. Agent creates sandbox with `skip_cleanup=True`, persists `claim_name`
2. Agent process restarts (pod eviction, rolling update, crash)
3. New agent instance passes saved `claim_name` to reconnect without creating a new pod

Labels allow tagging sandboxes at creation for discovery and filtering.

### Implementation in `src/langchain_k8s/sandbox.py`

1. **`claim_name: str | None = None` parameter** — when set, `_ensure_sandbox()` calls `client.get_sandbox(claim_name, namespace)` instead of `client.create_sandbox(...)`. The SDK verifies the SandboxClaim exists in Kubernetes and re-establishes the connection (port-forward, tracing).

2. **`labels: dict[str, str] | None = None` parameter** — passed to `client.create_sandbox(labels=...)` at creation time. Ignored when `claim_name` is set (reconnecting). The SDK validates label name/value format.

3. **`claim_name` property (read-only)** — returns the live handle's `claim_name` if connected, the constructor's `claim_name` if set but not yet connected, or `None`. Callers persist this value to reconnect later.

### Tests: `TestClaimName` (~5 tests), `TestReconnect` (~6 tests), `TestLabels` (~5 tests)

**TestClaimName:**

- `claim_name` is `None` before start (no constructor value)
- Returns handle's claim_name after start
- Returns constructor value before start when set
- Live handle takes precedence over constructor value
- Returns `None` after stop (no constructor value)

**TestReconnect:**

- `claim_name` set → calls `get_sandbox()`, not `create_sandbox()`
- Execute works after reconnect
- Reconnect + `skip_cleanup` uses `_close_connection()` on stop
- `id` uses claim_name from live handle
- No `claim_name` → normal `create_sandbox()` path

**TestLabels:**

- Default is `None`
- Stored on construction
- Passed to `create_sandbox(labels=...)`
- `None` labels passed as `None`
- Labels ignored on reconnect (`get_sandbox` has no labels arg)

---

## Final Constructor Signature

```python
def __init__(
    self,
    *,
    template_name: str,
    namespace: str = "default",
    gateway_name: str | None = None,
    gateway_namespace: str = "default",
    api_url: str | None = None,
    server_port: int = 8888,
    reuse_sandbox: bool = True,
    max_output_size: int = _DEFAULT_MAX_OUTPUT_SIZE,
    command_timeout: int = _DEFAULT_COMMAND_TIMEOUT,
    allow_prefixes: list[str] | None = None,    # Feature 1
    root_dir: str | None = None,                 # Feature 2
    virtual_mode: bool = False,                   # Feature 2
    skip_cleanup: bool = False,                   # Feature 3
    claim_name: str | None = None,               # Feature 4
    labels: dict[str, str] | None = None,        # Feature 4
) -> None:
```

---

## Files Modified

| File | Changes |
|------|---------|
| `src/langchain_k8s/sandbox.py` | Replaced deny→allow, added root_dir + path resolution, overrode read/ls_info/glob_info/grep_raw, skip_cleanup, claim_name reconnect, labels, claim_name property |
| `tests/unit/test_sandbox.py` | Replaced `TestDenyPrefixes` → `TestAllowPrefixes`, updated `TestVirtualMode` for root_dir + path resolution, added `TestSkipCleanup`, `TestClaimName`, `TestReconnect`, `TestLabels` |
| `tests/integration/test_kind.py` | Added `TestReconnect` (reconnect to existing sandbox, claim_name property), `TestLabels` (labels applied to sandbox) |
| `tests/conftest.py` | Added `get_sandbox` mock to `make_mock_client()` |
| `README.md` | Added enterprise features section (allow_prefixes, virtual_mode, skip_cleanup, sticky sessions, reconnect, labels, updated config reference) |
| `specs/plans/enterprise-features.md` | This plan document |

---

## Verification

```bash
uv run pytest tests/unit/ -v           # 128 tests pass
uv run pytest tests/integration/ -v    # 60 tests pass
uv run ruff check src/ tests/          # No lint errors
uv run pyright src/                    # 0 errors (pre-existing warnings only)
```
