"""Kubernetes Agent Sandbox backend for LangChain Deep Agents."""

from __future__ import annotations

import base64
import logging
import posixpath
import shlex
import threading
import uuid
from typing import TYPE_CHECKING

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileInfo,
    FileOperationError,
    FileUploadResponse,
    GrepMatch,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

from langchain_k8s.proxy import patch_k8s_proxy_config

if TYPE_CHECKING:
    from k8s_agent_sandbox import SandboxClient

# Apply the NO_PROXY monkey-patch once at import time so that all
# kubernetes client instances created in this process honour NO_PROXY.
patch_k8s_proxy_config()

logger = logging.getLogger(__name__)

_DEFAULT_MAX_OUTPUT_SIZE = 1_048_576  # 1 MB
_DEFAULT_COMMAND_TIMEOUT = 300  # 5 minutes
_DEFAULT_ROOT_DIR = "/workspace"


class KubernetesSandbox(BaseSandbox):
    """LangChain sandbox backend for kubernetes-sigs/agent-sandbox.

    Wraps the ``k8s-agent-sandbox`` Python SDK (``k8s_agent_sandbox.SandboxClient``)
    to implement the ``BaseSandbox`` contract.  All standard filesystem tools
    (``read``, ``write``, ``edit``, ``ls``, ``grep``, ``glob``) are provided by
    ``BaseSandbox`` via the ``execute()`` primitive.

    Two lifecycle strategies are available via the ``reuse_sandbox`` flag:

    * **Persistent** (``reuse_sandbox=True``, default) — one sandbox pod is
      created lazily on the first ``execute()`` call and reused across all
      subsequent calls.  Fast for cached, long-lived agents.
    * **Ephemeral** (``reuse_sandbox=False``) — a fresh sandbox pod is created
      for every ``start()`` / ``stop()`` cycle.  Maximum isolation between
      invocations at the cost of cold-start latency.

    Supports all three SDK connection modes:

    * **Production** — set ``gateway_name`` to discover a cluster Gateway IP.
    * **Development** — omit ``gateway_name`` and ``api_url`` for automatic
      ``kubectl port-forward`` (one tunnel per sandbox).
    * **Advanced / Internal** — set ``api_url`` to connect to a pre-existing
      port-forward or in-cluster router.

    Path access policy
    ~~~~~~~~~~~~~~~~~~

    The ``allow_prefixes`` parameter enforces a tool-level write policy.
    When set, ``write()`` and ``edit()`` operations are only permitted for
    paths that start with one of the given prefixes.  All other paths return
    an error result without executing a command.

    By default (``allow_prefixes=None``) no restrictions are applied.

    .. note::

       This is a **tool-level** policy only.  It does NOT block
       ``execute("echo bad > /etc/passwd")``.  Use the Kubernetes pod
       ``securityContext`` (e.g. ``readOnlyRootFilesystem``) for system-level
       protection.

    Virtual filesystem
    ~~~~~~~~~~~~~~~~~~

    When ``virtual_mode=True`` all file-operation paths are resolved under
    ``root_dir`` (default ``/workspace``).  Path traversal (``..``, ``~``)
    is rejected.  This provides containment semantics similar to
    ``FilesystemBackend(virtual_mode=True)``.

    In virtual mode ``download_files()`` uses the native SDK HTTP transfer
    (``SandboxClient.read()``) instead of base64-encoded shell commands.
    Uploads continue to use shell commands because the SDK ``write()``
    method only preserves the file basename.

    Enterprise deployment — horizontally scaled services
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    When deploying a service that uses ``KubernetesSandbox`` behind a load
    balancer with multiple replicas, ensure requests from the same user or
    session are routed to the **same service instance**.  Standard approaches:

    * **Kubernetes Service** — set ``sessionAffinity: ClientIP``.
    * **Ingress / Gateway** — configure sticky sessions via annotations,
      for example ``nginx.ingress.kubernetes.io/affinity: "cookie"``.

    To prevent sandbox destruction when a service instance restarts or scales
    down, set ``skip_cleanup=True``::

        backend = KubernetesSandbox(
            template_name="python-sandbox-template",
            namespace="agent-sandbox-system",
            skip_cleanup=True,
            sandbox_id="user-session-123",
        )

    The sandbox pod continues running after ``stop()`` and must be cleaned up
    externally (e.g. Kubernetes TTL controller, CronJob, or manual deletion).

    .. note::

       Full sandbox reconnection (multiple service instances sharing the same
       Kubernetes pod) requires upstream SDK support for deterministic claim
       names.  Currently each ``start()`` call creates a **new** SandboxClaim.

    Example — persistent sandbox with context manager::

        from langchain_k8s import KubernetesSandbox

        with KubernetesSandbox(
            template_name="python-sandbox-template",
            namespace="agent-sandbox-system",
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend, ...)
            result = agent.invoke({"messages": [...]})
            # ... more invocations reuse the same pod ...

    Example — ephemeral sandbox with explicit lifecycle::

        backend = KubernetesSandbox(
            template_name="python-sandbox-template",
            namespace="agent-sandbox-system",
            reuse_sandbox=False,
        )
        backend.start()
        try:
            result = backend.execute("echo hello")
        finally:
            backend.stop()
    """

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
        allow_prefixes: list[str] | None = None,
        root_dir: str | None = None,
        virtual_mode: bool = False,
        sandbox_id: str | None = None,
        skip_cleanup: bool = False,
    ) -> None:
        """Initialise the backend.  No sandbox is created until first use.

        Args:
            template_name: Name of the ``SandboxTemplate`` CRD to use.
            namespace: Kubernetes namespace for the sandbox resources.
            gateway_name: Gateway resource name (production mode).
            gateway_namespace: Kubernetes namespace of the Gateway resource.
            api_url: Direct URL to the sandbox router (advanced mode).
            server_port: Port the sandbox runtime listens on.
            reuse_sandbox: If ``True`` (default) one sandbox is reused across
                calls.  If ``False`` a new sandbox is created on each
                ``start()`` and destroyed on ``stop()``.
            max_output_size: Truncate ``execute()`` output beyond this many
                bytes.
            command_timeout: Default timeout in seconds for ``run()`` calls.
            allow_prefixes: List of path prefixes where ``write()`` and
                ``edit()`` operations are allowed.  ``None`` (default) means
                no restrictions.  When set, only paths starting with one of
                these prefixes are writable; all others return an error.
                The check runs against the **resolved** path (after virtual
                mode resolution, if enabled).
            root_dir: Root directory for virtual filesystem mode.  Only
                meaningful when ``virtual_mode=True``.  Defaults to
                ``"/workspace"``.
            virtual_mode: When ``True``, all file-operation paths are resolved
                under ``root_dir``.  Path traversal (``..``, ``~``) is
                blocked.  ``download_files()`` uses the native SDK HTTP
                transfer instead of base64-encoded shell commands.
            sandbox_id: Optional stable identifier for this sandbox instance.
                When set, the ``id`` property returns this value instead of the
                Kubernetes claim name or auto-generated UUID.  Useful for
                logging and correlation in multi-instance deployments.
            skip_cleanup: When ``True``, ``stop()`` releases local resources
                (port-forward, tracing) but does **not** delete the Kubernetes
                ``SandboxClaim``.  The sandbox pod continues running and must
                be cleaned up externally.
        """
        self._template_name = template_name
        self._namespace = namespace
        self._gateway_name = gateway_name
        self._gateway_namespace = gateway_namespace
        self._api_url = api_url
        self._server_port = server_port
        self._reuse_sandbox = reuse_sandbox
        self._max_output_size = max_output_size
        self._command_timeout = command_timeout
        self._virtual_mode = virtual_mode
        self._sandbox_id = sandbox_id
        self._skip_cleanup = skip_cleanup

        if allow_prefixes is not None:
            self._allow_prefixes: tuple[str, ...] | None = tuple(
                p if p.endswith("/") else p + "/" for p in allow_prefixes
            )
        else:
            self._allow_prefixes = None

        if virtual_mode:
            self._root_dir: str | None = root_dir if root_dir is not None else _DEFAULT_ROOT_DIR
        else:
            self._root_dir = root_dir

        self._client: SandboxClient | None = None
        self._id: str = str(uuid.uuid4())
        self._lock = threading.Lock()
        self._started = False

        logger.debug(
            "KubernetesSandbox created: id=%s template=%s namespace=%s reuse=%s mode=%s"
            " allow_prefixes=%s root_dir=%s virtual_mode=%s skip_cleanup=%s",
            self._id,
            self._template_name,
            self._namespace,
            self._reuse_sandbox,
            "gateway" if gateway_name else ("api_url" if api_url else "tunnel"),
            self._allow_prefixes,
            self._root_dir,
            self._virtual_mode,
            self._skip_cleanup,
        )

    # -- BaseSandbox abstract property -----------------------------------------

    @property
    def id(self) -> str:  # noqa: A003
        """Unique identifier for this sandbox backend instance.

        Returns ``sandbox_id`` if set, otherwise the Kubernetes sandbox claim
        name if a sandbox is running, otherwise a stable UUID generated at
        construction time.
        """
        if self._sandbox_id is not None:
            return self._sandbox_id
        if self._client is not None and self._client.claim_name is not None:
            return str(self._client.claim_name)
        return self._id

    # -- Lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Eagerly create the sandbox.  Idempotent if already started."""
        self._ensure_sandbox()

    def stop(self) -> None:
        """Destroy the current sandbox and release resources."""
        self._destroy_sandbox()

    def __enter__(self) -> KubernetesSandbox:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        self.stop()

    # -- BaseSandbox abstract methods ------------------------------------------

    def execute(self, command: str) -> ExecuteResponse:
        """Execute a shell command inside the sandbox.

        The sandbox is created lazily on the first call.  In persistent mode
        (``reuse_sandbox=True``) a connection error triggers one automatic
        reconnect attempt.
        """
        logger.debug("execute: sandbox=%s command=%r", self.id, command)
        self._ensure_sandbox()
        assert self._client is not None  # ensured by _ensure_sandbox

        try:
            resp = self._run(command)
        except Exception as exc:
            if self._reuse_sandbox:
                logger.warning(
                    "execute: sandbox=%s connection lost (%s) — reconnecting",
                    self.id,
                    exc,
                )
                self._destroy_sandbox()
                self._ensure_sandbox()
                assert self._client is not None
                resp = self._run(command)
            else:
                logger.debug(
                    "execute: sandbox=%s command failed (reuse_sandbox=False, not retrying): %s",
                    self.id,
                    exc,
                )
                raise

        logger.debug(
            "execute: sandbox=%s exit_code=%d output_len=%d truncated=%s",
            self.id,
            resp.exit_code,
            len(resp.output),
            resp.truncated,
        )
        return resp

    # -- File operations with virtual-mode resolution & allow_prefixes ---------

    def write(self, file_path: str, content: str) -> WriteResult:
        """Create a new file, subject to virtual-mode resolution and
        ``allow_prefixes`` policy.
        """
        try:
            resolved = self._resolve_virtual_path(file_path)
        except ValueError as exc:
            logger.debug("write: path resolution failed path=%r reason=%s", file_path, exc)
            return WriteResult(error=str(exc))
        allow_error = self._check_allow_prefix(resolved)
        if allow_error is not None:
            logger.debug("write: denied path=%r reason=%s", file_path, allow_error)
            return WriteResult(error=allow_error)
        return super().write(resolved, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        """Edit a file by string replacement, subject to virtual-mode
        resolution and ``allow_prefixes`` policy.
        """
        try:
            resolved = self._resolve_virtual_path(file_path)
        except ValueError as exc:
            logger.debug("edit: path resolution failed path=%r reason=%s", file_path, exc)
            return EditResult(error=str(exc))
        allow_error = self._check_allow_prefix(resolved)
        if allow_error is not None:
            logger.debug("edit: denied path=%r reason=%s", file_path, allow_error)
            return EditResult(error=allow_error)
        return super().edit(resolved, old_string, new_string, replace_all)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read file content with virtual-mode path resolution."""
        try:
            resolved = self._resolve_virtual_path(file_path)
        except ValueError as exc:
            return f"Error: {exc}"
        return super().read(resolved, offset, limit)

    def ls_info(self, path: str) -> list[FileInfo]:
        """List directory contents with virtual-mode path resolution."""
        try:
            resolved = self._resolve_virtual_path(path)
        except ValueError:
            return []
        return super().ls_info(resolved)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """Glob file matching with virtual-mode path resolution."""
        try:
            resolved = self._resolve_virtual_path(path)
        except ValueError:
            return []
        return super().glob_info(pattern, resolved)

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        """Search file content with virtual-mode path resolution."""
        if path is not None:
            try:
                path = self._resolve_virtual_path(path)
            except ValueError as exc:
                return str(exc)
        elif self._virtual_mode and self._root_dir is not None:
            path = self._root_dir
        return super().grep_raw(pattern, path, glob)

    # -- File transfer ---------------------------------------------------------

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Upload files into the sandbox via base64-encoded shell commands.

        The native SDK ``write()`` endpoint only places files in a fixed upload
        directory and ignores the full path.  To write to arbitrary absolute
        paths we encode the content as base64 and pipe it through the shell.
        """
        logger.debug(
            "upload_files: sandbox=%s file_count=%d",
            self.id,
            len(files),
        )
        self._ensure_sandbox()
        assert self._client is not None

        results: list[FileUploadResponse] = []
        for path, content in files:
            try:
                resolved = self._resolve_virtual_path(path)
            except ValueError:
                results.append(FileUploadResponse(path=path, error="invalid_path"))
                continue
            error = _validate_path(resolved)
            if error is not None:
                logger.debug("upload_files: invalid path %r", path)
                results.append(FileUploadResponse(path=path, error=error))
                continue
            try:
                b64 = base64.b64encode(content).decode("ascii")
                escaped_path = shlex.quote(resolved)
                cmd = f"mkdir -p $(dirname {escaped_path}) && printf '%s' '{b64}' | base64 -d > {escaped_path}"
                resp = self._run_raw(cmd)
                if resp.exit_code != 0:
                    file_error = _classify_error(resp.output)
                    logger.debug(
                        "upload_files: sandbox=%s path=%s failed (%s)",
                        self.id,
                        path,
                        file_error,
                    )
                    results.append(FileUploadResponse(path=path, error=file_error))
                else:
                    logger.debug(
                        "upload_files: sandbox=%s path=%s size=%d OK",
                        self.id,
                        path,
                        len(content),
                    )
                    results.append(FileUploadResponse(path=path, error=None))
            except Exception as exc:
                logger.warning("upload_files: sandbox=%s path=%s exception: %s", self.id, path, exc)
                results.append(FileUploadResponse(path=path, error="invalid_path"))
        return results

    def download_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Download files from the sandbox.

        When ``virtual_mode=True`` the native SDK HTTP transfer is used.
        Otherwise files are read via base64-encoded shell commands.
        """
        if self._virtual_mode:
            return self._download_files_native(paths)
        return self._download_files_shell(paths)

    def _download_files_shell(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Download files via base64-encoded shell commands."""
        logger.debug(
            "download_files: sandbox=%s file_count=%d mode=shell",
            self.id,
            len(paths),
        )
        self._ensure_sandbox()
        assert self._client is not None

        results: list[FileDownloadResponse] = []
        for path in paths:
            try:
                resolved = self._resolve_virtual_path(path)
            except ValueError:
                results.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
                continue
            error = _validate_path(resolved)
            if error is not None:
                logger.debug("download_files: invalid path %r", path)
                results.append(FileDownloadResponse(path=path, content=None, error=error))
                continue
            try:
                escaped_path = shlex.quote(resolved)
                resp = self._run_raw(f"base64 {escaped_path}")
                if resp.exit_code != 0:
                    file_error = _classify_error(resp.output)
                    logger.debug(
                        "download_files: sandbox=%s path=%s failed (%s)",
                        self.id,
                        path,
                        file_error,
                    )
                    results.append(FileDownloadResponse(path=path, content=None, error=file_error))
                else:
                    content = base64.b64decode(resp.output.strip())
                    logger.debug(
                        "download_files: sandbox=%s path=%s size=%d OK",
                        self.id,
                        path,
                        len(content),
                    )
                    results.append(FileDownloadResponse(path=path, content=content, error=None))
            except Exception as exc:
                logger.warning("download_files: sandbox=%s path=%s exception: %s", self.id, path, exc)
                results.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
        return results

    def _download_files_native(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Download files via the native SDK HTTP transfer."""
        logger.debug(
            "download_files: sandbox=%s file_count=%d mode=native",
            self.id,
            len(paths),
        )
        self._ensure_sandbox()
        assert self._client is not None

        results: list[FileDownloadResponse] = []
        for path in paths:
            try:
                resolved = self._resolve_virtual_path(path)
            except ValueError:
                results.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
                continue
            error = _validate_path(resolved)
            if error is not None:
                logger.debug("download_files: invalid path %r", path)
                results.append(FileDownloadResponse(path=path, content=None, error=error))
                continue
            try:
                content = self._client.read(resolved)
                logger.debug(
                    "download_files: sandbox=%s path=%s size=%d OK (native)",
                    self.id,
                    path,
                    len(content),
                )
                results.append(FileDownloadResponse(path=path, content=content, error=None))
            except Exception as exc:
                logger.warning("download_files: sandbox=%s path=%s exception: %s", self.id, path, exc)
                error_str = str(exc).lower()
                if "permission" in error_str or "403" in error_str:
                    file_error: FileOperationError = "permission_denied"
                else:
                    file_error = "file_not_found"
                results.append(FileDownloadResponse(path=path, content=None, error=file_error))
        return results

    # -- Internals -------------------------------------------------------------

    def _check_allow_prefix(self, file_path: str) -> str | None:
        """Return an error message if *file_path* is not under any allowed prefix, else ``None``."""
        if self._allow_prefixes is None:
            return None
        for prefix in self._allow_prefixes:
            if file_path.startswith(prefix):
                return None
        return f"Path {file_path!r} is not under any allowed prefix: {self._allow_prefixes}"

    def _resolve_virtual_path(self, path: str) -> str:
        """Resolve a virtual path to an absolute path under ``root_dir``.

        When ``virtual_mode`` is disabled the path is returned unchanged.
        When enabled, path traversal (``..``, ``~``) is rejected and the
        path is anchored under ``root_dir``.
        """
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

    def _run(self, command: str) -> ExecuteResponse:
        """Execute a user command wrapped in ``sh -c`` and map to ``ExecuteResponse``.

        The sandbox runtime's ``/execute`` endpoint runs commands directly
        without a shell, so pipes (``|``), redirects (``>``), and chaining
        (``&&``) would be interpreted literally.  Wrapping in ``sh -c``
        ensures full POSIX shell semantics.
        """
        resp = self._run_raw(command)
        return resp

    def _run_raw(self, command: str) -> ExecuteResponse:
        """Low-level execute via SDK: wraps in ``sh -c`` and maps result."""
        assert self._client is not None
        shell_cmd = f"sh -c {shlex.quote(command)}"
        logger.debug("_run_raw: sandbox=%s shell_cmd=%r", self.id, shell_cmd)
        result = self._client.run(shell_cmd, timeout=self._command_timeout)

        parts: list[str] = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(result.stderr)
        output = "\n".join(parts)

        truncated = len(output) > self._max_output_size
        if truncated:
            logger.debug(
                "_run_raw: sandbox=%s output truncated from %d to %d bytes",
                self.id,
                len(output),
                self._max_output_size,
            )
            output = output[: self._max_output_size]

        return ExecuteResponse(
            output=output,
            exit_code=result.exit_code,
            truncated=truncated,
        )

    def _ensure_sandbox(self) -> None:
        """Create the sandbox if not already running.  Thread-safe."""
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            self._client = self._create_client()
            self._client.__enter__()
            self._started = True
            logger.info("Sandbox started: %s", self.id)

    def _destroy_sandbox(self) -> None:
        """Destroy the current sandbox.  Thread-safe and idempotent.

        When ``skip_cleanup=True`` the Kubernetes ``SandboxClaim`` is
        preserved so the sandbox pod keeps running.  Local resources
        (port-forward, tracing) are still released.
        """
        with self._lock:
            if not self._started:
                return
            if self._client is not None:
                if self._skip_cleanup:
                    # Temporarily clear claim_name so the SDK's __exit__
                    # skips CRD deletion while still cleaning up local
                    # resources (port-forward, tracing).
                    saved_claim = self._client.claim_name
                    self._client.claim_name = None
                    try:
                        self._client.__exit__(None, None, None)
                    except Exception as exc:
                        logger.warning("Error during sandbox disconnect: %s", exc)
                    finally:
                        self._client.claim_name = saved_claim
                    logger.info(
                        "Sandbox disconnected (skip_cleanup=True, SandboxClaim %s preserved)",
                        saved_claim,
                    )
                else:
                    try:
                        self._client.__exit__(None, None, None)
                    except Exception as exc:
                        logger.warning("Error during sandbox cleanup: %s", exc)
                    logger.info("Sandbox stopped")
                self._client = None
            self._started = False

    def _create_client(self) -> SandboxClient:
        """Build a new ``SandboxClient`` from stored configuration."""
        from k8s_agent_sandbox import SandboxClient as _SandboxClient

        kwargs: dict[str, object] = {
            "template_name": self._template_name,
            "namespace": self._namespace,
            "server_port": self._server_port,
        }
        if self._gateway_name is not None:
            kwargs["gateway_name"] = self._gateway_name
            kwargs["gateway_namespace"] = self._gateway_namespace
        if self._api_url is not None:
            kwargs["api_url"] = self._api_url
        logger.debug("_create_client: kwargs=%s", kwargs)
        return _SandboxClient(**kwargs)


def _validate_path(path: str) -> FileOperationError | None:
    """Return an error literal if *path* is syntactically invalid, else ``None``."""
    if not path or not path.startswith("/"):
        return "invalid_path"
    return None


def _classify_error(output: str) -> FileOperationError:
    """Map stderr/output text from a failed shell command to a ``FileOperationError``."""
    lower = output.lower()
    if "permission denied" in lower:
        return "permission_denied"
    if "is a directory" in lower:
        return "is_directory"
    if "no such file or directory" in lower:
        return "file_not_found"
    # Default — could be a missing parent directory or other path issue.
    return "file_not_found"
