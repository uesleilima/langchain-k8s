"""Kubernetes Agent Sandbox backend for LangChain Deep Agents."""

from __future__ import annotations

import base64
import logging
import posixpath
import shlex
import threading
import uuid
from typing import TYPE_CHECKING, Any

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileOperationError,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

from langchain_k8s.proxy import patch_k8s_proxy_config

if TYPE_CHECKING:
    from k8s_agent_sandbox import SandboxClient
    from k8s_agent_sandbox.sandbox import Sandbox

# Apply the NO_PROXY monkey-patch once at import time so that all
# kubernetes client instances created in this process honour NO_PROXY.
patch_k8s_proxy_config()

logger = logging.getLogger(__name__)

_DEFAULT_MAX_OUTPUT_SIZE = 1_048_576  # 1 MB
_DEFAULT_COMMAND_TIMEOUT = 300  # 5 minutes
_DEFAULT_ROOT_DIR = "/tmp"


class KubernetesSandbox(BaseSandbox):
    """LangChain sandbox backend for kubernetes-sigs/agent-sandbox.

    Wraps the ``k8s-agent-sandbox`` Python SDK to implement the
    ``BaseSandbox`` contract.  All standard filesystem tools (``read``,
    ``write``, ``edit``, ``ls``, ``grep``, ``glob``) are provided by
    ``BaseSandbox`` via the ``execute()`` primitive.

    Two constructor modes are supported:

    Ecosystem-standard mode (recommended)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Pass a pre-created ``k8s_agent_sandbox.Sandbox`` handle via the
    ``sandbox`` parameter.  This follows the standard LangChain Deep
    Agents sandbox backend pattern.  Lifecycle management (create /
    delete) stays with the caller::

        from k8s_agent_sandbox import SandboxClient
        from langchain_k8s import KubernetesSandbox

        client = SandboxClient(connection_config=...)
        handle = client.create_sandbox(template="python-sandbox-template")
        backend = KubernetesSandbox(sandbox=handle)

        result = backend.execute("echo hello")
        # ... use backend ...
        client.delete_sandbox(handle.claim_name)

    Config-based mode (convenience / backward-compatible)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Pass ``template_name`` and connection parameters.  The sandbox is
    created lazily on first use and destroyed on ``stop()``.  This mode
    supports ``start()``/``stop()`` lifecycle methods::

        backend = KubernetesSandbox(
            template_name="python-sandbox-template",
            namespace="agent-sandbox-system",
        )
        backend.start()
        result = backend.execute("echo hello")
        backend.stop()

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
    ``root_dir`` (default ``/tmp``).  Path traversal (``..``, ``~``)
    is rejected.  This provides containment semantics similar to
    ``FilesystemBackend(virtual_mode=True)``.

    Enterprise deployment — horizontally scaled services
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    When deploying a service that uses ``KubernetesSandbox`` behind a load
    balancer with multiple replicas, ensure requests from the same user or
    session are routed to the **same service instance**.  Standard approaches:

    * **Kubernetes Service** — set ``sessionAffinity: ClientIP``.
    * **Ingress / Gateway** — configure sticky sessions via annotations,
      for example ``nginx.ingress.kubernetes.io/affinity: "cookie"``.

    Thread-scoped graph factory (production)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Use ``create_kubernetes_sandbox()`` with a graph factory for
    thread-scoped sandboxes::

        from k8s_agent_sandbox import SandboxClient
        from langchain_k8s import create_kubernetes_sandbox

        client = SandboxClient(connection_config=...)

        async def agent(config: RunnableConfig):
            thread_id = config["configurable"]["thread_id"]
            backend = create_kubernetes_sandbox(
                client=client,
                claim_name=f"sandbox-{thread_id}",
                template_name="python-sandbox-template",
                namespace="agent-sandbox-system",
                labels={"thread_id": thread_id},
            )
            return create_deep_agent(model=..., backend=backend)
    """

    def __init__(
        self,
        *,
        sandbox: Sandbox | None = None,
        template_name: str | None = None,
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
        skip_cleanup: bool = False,
        claim_name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Initialise the backend.

        Either ``sandbox`` (ecosystem-standard) or ``template_name``
        (config-based) must be provided, but not both ``sandbox`` and
        ``claim_name`` simultaneously.

        Args:
            sandbox: Pre-created ``k8s_agent_sandbox.Sandbox`` handle.
                When provided, the backend wraps this handle directly
                and lifecycle management is the caller's responsibility.
                This is the standard pattern used by all LangChain Deep
                Agents sandbox backends.
            template_name: Name of the ``SandboxTemplate`` CRD to use.
                Required when ``sandbox`` is not provided.
            namespace: Kubernetes namespace for the sandbox resources.
            gateway_name: Gateway resource name (production mode).
            gateway_namespace: Kubernetes namespace of the Gateway resource.
            api_url: Direct URL to the sandbox router (advanced mode).
            server_port: Port the sandbox runtime listens on.
            reuse_sandbox: If ``True`` (default) one sandbox is reused across
                calls.  If ``False`` a new sandbox is created on each
                ``start()`` and destroyed on ``stop()``.
                Only used in config-based mode.
            max_output_size: Truncate ``execute()`` output beyond this many
                bytes.
            command_timeout: Default timeout in seconds for ``run()`` calls.
                Can be overridden per-call via the ``timeout`` parameter
                on ``execute()``.
            allow_prefixes: List of path prefixes where ``write()`` and
                ``edit()`` operations are allowed.  ``None`` (default) means
                no restrictions.  When set, only paths starting with one of
                these prefixes are writable; all others return an error.
                The check runs against the **resolved** path (after virtual
                mode resolution, if enabled).

                .. important::
                   This is a **tool-level policy** enforced by the sandbox
                   backend *before* the command reaches the container.  It
                   does **not** grant filesystem permissions inside the
                   container.  The target directories must also be writable
                   by the container's OS user (see ``root_dir`` note below).

            root_dir: Root directory for virtual filesystem mode.  Only
                meaningful when ``virtual_mode=True``.  Defaults to
                ``"/tmp"``, which is always writable inside the container.

                .. important::
                   ``root_dir`` must point to a directory that the
                   **container process can write to**.  ``/tmp`` (the
                   default) is always writable.  Other common options are
                   ``/home/<user>`` or a volume-mounted path.  Custom
                   directories like ``/workspace`` require an explicit
                   writable volume mount in the ``SandboxTemplate`` pod
                   spec.  When ``write()`` or ``edit()`` fails with
                   ``PermissionError`` despite ``allow_prefixes`` allowing
                   the path, the container's filesystem permissions are the
                   likely cause.

            virtual_mode: When ``True``, all file-operation paths are resolved
                under ``root_dir``.  Path traversal (``..``, ``~``) is
                blocked.
            skip_cleanup: When ``True``, ``stop()`` releases local resources
                (port-forward, tracing) but does **not** delete the Kubernetes
                ``SandboxClaim``.  The sandbox pod continues running and must
                be cleaned up externally.  Only used in config-based mode.
            claim_name: Kubernetes ``SandboxClaim`` name of an existing
                sandbox to reconnect to instead of creating a new one.
                When set, ``start()`` calls ``SandboxClient.get_sandbox()``
                to re-attach.  Cannot be combined with ``sandbox``.
            labels: Kubernetes labels applied to the ``SandboxClaim`` at
                creation time.  Ignored when ``claim_name`` is set
                (reconnecting to an existing sandbox).  Useful for
                identifying sandboxes by session, user, or environment.
        """
        if sandbox is not None and claim_name is not None:
            msg = "Cannot specify both 'sandbox' and 'claim_name'"
            raise ValueError(msg)
        if sandbox is None and template_name is None and claim_name is None:
            msg = "Either 'sandbox' or 'template_name' must be provided"
            raise ValueError(msg)

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
        self._skip_cleanup = skip_cleanup
        self._claim_name = claim_name
        self._labels = labels
        self._owns_lifecycle = sandbox is None

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
        self._lock = threading.Lock()

        if sandbox is not None:
            self._sandbox: Sandbox | None = sandbox
            self._id: str = str(sandbox.claim_name) if sandbox.claim_name else str(uuid.uuid4())
            self._started = True
        else:
            self._sandbox = None
            self._id = str(uuid.uuid4())
            self._started = False

        logger.debug(
            "KubernetesSandbox created: id=%s template=%s namespace=%s reuse=%s mode=%s"
            " allow_prefixes=%s root_dir=%s virtual_mode=%s skip_cleanup=%s"
            " claim_name=%s labels=%s owns_lifecycle=%s",
            self._id,
            self._template_name,
            self._namespace,
            self._reuse_sandbox,
            "handle" if sandbox is not None else ("gateway" if gateway_name else ("api_url" if api_url else "tunnel")),
            self._allow_prefixes,
            self._root_dir,
            self._virtual_mode,
            self._skip_cleanup,
            self._claim_name,
            self._labels,
            self._owns_lifecycle,
        )

    # -- BaseSandbox abstract property -----------------------------------------

    @property
    def id(self) -> str:  # noqa: A003
        """Unique identifier for this sandbox backend instance.

        Returns the Kubernetes sandbox claim name if a sandbox is running,
        otherwise a stable UUID generated at construction time.

        Returns:
            A string identifier.  After ``start()`` this is the
            ``SandboxClaim`` name; before ``start()`` it is an
            auto-generated UUID.
        """
        if self._sandbox is not None and self._sandbox.claim_name is not None:
            return str(self._sandbox.claim_name)
        return self._id

    @property
    def claim_name(self) -> str | None:
        """Kubernetes ``SandboxClaim`` name of the running sandbox.

        Returns ``None`` before ``start()`` is called (unless a
        ``claim_name`` was passed to the constructor for reconnection).
        Persist this value to reconnect after a process restart.

        Returns:
            The claim name string, or ``None`` if no sandbox has been
            created yet and no ``claim_name`` was provided at
            construction time.
        """
        if self._sandbox is not None and self._sandbox.claim_name is not None:
            return str(self._sandbox.claim_name)
        return self._claim_name

    # -- Lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Eagerly create (or reconnect to) the sandbox pod.

        In config-based mode this creates the Kubernetes ``SandboxClaim``
        and waits for the sandbox pod to become ready.  When
        ``claim_name`` was provided, it reconnects to an existing pod
        instead.

        The call is idempotent: calling ``start()`` on an already-started
        backend is a no-op.

        Raises:
            k8s_agent_sandbox.exceptions.SandboxNotFoundError:
                If ``claim_name`` was specified but no matching
                ``SandboxClaim`` exists in the cluster.
            kubernetes.client.ApiException:
                On Kubernetes API errors (e.g. RBAC, namespace not found).
        """
        self._ensure_sandbox()

    def stop(self) -> None:
        """Destroy the current sandbox and release local resources.

        In config-based mode the Kubernetes ``SandboxClaim`` is deleted
        (unless ``skip_cleanup=True``).  In handle mode only the local
        connection is closed — the caller owns the Kubernetes resource
        lifecycle.

        The call is idempotent: calling ``stop()`` on an already-stopped
        backend is a no-op.
        """
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

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute a shell command inside the sandbox.

        The command is wrapped in ``sh -c '...'`` so full POSIX shell
        semantics (pipes, redirects, ``&&``) work as expected.  The
        sandbox is created lazily on the first call.  In persistent mode
        (``reuse_sandbox=True``) a connection error triggers one automatic
        reconnect attempt before the exception propagates.

        Args:
            command: Full shell command string to execute.  May contain
                pipes, redirects, and chaining operators.
            timeout: Maximum time in seconds to wait for the command to
                complete.  If ``None``, uses the backend's default
                ``command_timeout``.

        Returns:
            An :class:`~deepagents.backends.protocol.ExecuteResponse`
            containing ``output`` (combined stdout + stderr),
            ``exit_code``, and a ``truncated`` flag indicating whether
            the output was clipped to ``max_output_size``.

        Raises:
            Exception: Propagated from the underlying SDK if the command
                cannot be dispatched (e.g. network error after retry
                exhaustion).
        """
        effective_timeout = timeout if timeout is not None else self._command_timeout
        logger.debug("execute: sandbox=%s command=%r timeout=%s", self.id, command, effective_timeout)
        self._ensure_sandbox()
        assert self._sandbox is not None  # ensured by _ensure_sandbox

        try:
            resp = self._run(command, timeout=effective_timeout)
        except Exception as exc:
            if self._reuse_sandbox and self._owns_lifecycle:
                logger.warning(
                    "execute: sandbox=%s connection lost (%s) — reconnecting",
                    self.id,
                    exc,
                )
                self._destroy_sandbox()
                self._ensure_sandbox()
                assert self._sandbox is not None
                resp = self._run(command, timeout=effective_timeout)
            else:
                logger.debug(
                    "execute: sandbox=%s command failed (not retrying): %s",
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
        """Create or overwrite a file inside the sandbox.

        The path is first resolved through virtual-mode (if enabled) and
        then checked against the ``allow_prefixes`` policy.  If either
        check fails, a :class:`~deepagents.backends.protocol.WriteResult`
        with a descriptive ``error`` string is returned — no command is
        sent to the container.

        Args:
            file_path: Absolute or virtual path of the file to write.
                Parent directories are created automatically.
            content: UTF-8 text content to write.

        Returns:
            A :class:`~deepagents.backends.protocol.WriteResult`.
            On success ``error`` is ``None``; on failure it contains a
            human-readable message (e.g. path policy violation,
            permission denied inside the container).
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
        result = super().write(resolved, content)
        if result.error and "PermissionError" in result.error:
            logger.warning(
                "write: container permission denied for path=%r (resolved=%r). "
                "The sandbox policy (allow_prefixes) permits this path, but the "
                "container's OS user cannot write to it. Ensure the target "
                "directory is writable in the SandboxTemplate pod spec "
                "(e.g. use a writable volume mount or a directory like /tmp).",
                file_path,
                resolved,
            )
        return result

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        """Edit a file by replacing an exact string.

        The path is resolved through virtual-mode (if enabled) and
        checked against the ``allow_prefixes`` policy before the
        replacement is executed inside the container.

        Args:
            file_path: Absolute or virtual path of the file to edit.
            old_string: The exact substring to search for.
            new_string: The replacement text.
            replace_all: If ``True`` replace every occurrence of
                *old_string*; if ``False`` (default) replace only the
                first occurrence.

        Returns:
            An :class:`~deepagents.backends.protocol.EditResult`.
            On success ``error`` is ``None``; on failure it contains a
            human-readable message.
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
        result = super().edit(resolved, old_string, new_string, replace_all)
        if result.error and "PermissionError" in result.error:
            logger.warning(
                "edit: container permission denied for path=%r (resolved=%r). "
                "The sandbox policy (allow_prefixes) permits this path, but the "
                "container's OS user cannot write to it. Ensure the target "
                "directory is writable in the SandboxTemplate pod spec "
                "(e.g. use a writable volume mount or a directory like /tmp).",
                file_path,
                resolved,
            )
        return result

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Read text content from a file inside the sandbox.

        Args:
            file_path: Absolute or virtual path of the file to read.
            offset: 0-based line offset to start reading from.
            limit: Maximum number of lines to return.

        Returns:
            A :class:`~deepagents.backends.protocol.ReadResult` with the
            file content.  On failure ``error`` describes the reason
            (e.g. ``"file_not_found"``).
        """
        try:
            resolved = self._resolve_virtual_path(file_path)
        except ValueError as exc:
            return ReadResult(error=str(exc))
        return super().read(resolved, offset, limit)

    def ls(self, path: str) -> LsResult:
        """List directory contents inside the sandbox.

        Args:
            path: Absolute or virtual directory path to list.

        Returns:
            An :class:`~deepagents.backends.protocol.LsResult` containing
            the directory entries.  On failure ``error`` describes the
            reason.
        """
        try:
            resolved = self._resolve_virtual_path(path)
        except ValueError as exc:
            return LsResult(error=str(exc))
        return super().ls(resolved)

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        """Find files matching a glob pattern inside the sandbox.

        Args:
            pattern: Shell-style glob pattern (e.g. ``"*.py"``,
                ``"**/*.json"``).
            path: Base directory to search from.  Defaults to ``"/"``.

        Returns:
            A :class:`~deepagents.backends.protocol.GlobResult` with
            matching file paths.  On failure ``error`` describes the
            reason.
        """
        try:
            resolved = self._resolve_virtual_path(path)
        except ValueError as exc:
            return GlobResult(error=str(exc))
        return super().glob(pattern, resolved)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Search for a text pattern inside sandbox files.

        When ``virtual_mode`` is enabled and *path* is not specified, the
        search is automatically scoped to ``root_dir``.

        Args:
            pattern: Text or regex pattern to search for.
            path: Directory or file path to search within.  ``None``
                searches the entire filesystem (or ``root_dir`` in
                virtual mode).
            glob: Optional glob filter to restrict which files are
                searched (e.g. ``"*.py"``).

        Returns:
            A :class:`~deepagents.backends.protocol.GrepResult` with
            matching lines and file locations.  On failure ``error``
            describes the reason.
        """
        if path is not None:
            try:
                path = self._resolve_virtual_path(path)
            except ValueError as exc:
                return GrepResult(error=str(exc))
        elif self._virtual_mode and self._root_dir is not None:
            path = self._root_dir
        return super().grep(pattern, path, glob)

    # -- File transfer ---------------------------------------------------------

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Upload one or more files into the sandbox.

        Each file is transferred by encoding its content as base64 and
        piping it through the sandbox shell.  This approach is used
        instead of the native SDK ``write()`` endpoint, which only
        supports a fixed upload directory and ignores full paths.

        Parent directories are created automatically.  Virtual-mode path
        resolution is applied when enabled.

        Args:
            files: A list of ``(path, content)`` tuples where *path* is
                an absolute (or virtual) POSIX path inside the container
                and *content* is the raw file bytes.

        Returns:
            A list of :class:`~deepagents.backends.protocol.FileUploadResponse`
            objects, one per input file, in the same order.  Each
            response contains the ``path`` and an ``error`` that is
            ``None`` on success or a
            :data:`~deepagents.backends.protocol.FileOperationError`
            literal on failure.
        """
        logger.debug(
            "upload_files: sandbox=%s file_count=%d",
            self.id,
            len(files),
        )
        self._ensure_sandbox()
        assert self._sandbox is not None

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
        """Download one or more files from the sandbox.

        Files are read by running ``base64 <path>`` inside the container
        and decoding the output.  This shell-based approach is used
        because the ``k8s-agent-sandbox`` runtime restricts its native
        ``/download`` endpoint to the ``/app`` directory, making it
        incompatible with arbitrary absolute paths.

        Virtual-mode path resolution is applied when enabled.

        Args:
            paths: A list of absolute (or virtual) POSIX paths to
                download.

        Returns:
            A list of :class:`~deepagents.backends.protocol.FileDownloadResponse`
            objects, one per input path, in the same order.  Each
            response contains the ``path``, the raw ``content`` bytes
            (or ``None`` on failure), and an ``error`` that is ``None``
            on success.
        """
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
        assert self._sandbox is not None

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
        assert self._sandbox is not None

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
                assert self._sandbox is not None
                content = self._sandbox.files.read(resolved)
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
        """Check whether *file_path* passes the ``allow_prefixes`` policy.

        Args:
            file_path: Resolved absolute path to check.

        Returns:
            ``None`` if the path is allowed (or no policy is configured);
            a human-readable error message string otherwise.
        """
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

        The method is idempotent: if *path* already starts with ``root_dir``
        it is validated but not re-prefixed.  This avoids double-resolution
        when ``super().write()`` internally calls ``self.upload_files()``.
        """
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
                msg = f"Path {path!r} resolves outside root directory {self._root_dir!r}"
                raise ValueError(msg)
            return normalized

        vpath = path if path.startswith("/") else "/" + path
        resolved = self._root_dir.rstrip("/") + vpath

        normalized = posixpath.normpath(resolved)
        if not normalized.startswith(root_normalized + "/") and normalized != root_normalized:
            msg = f"Path {path!r} resolves outside root directory {self._root_dir!r}"
            raise ValueError(msg)

        return normalized

    def _run(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute a user command wrapped in ``sh -c`` and return the response.

        The sandbox runtime's ``/execute`` endpoint runs commands without a
        shell, so pipes, redirects, and chaining operators would be
        interpreted literally.  Wrapping in ``sh -c`` ensures full POSIX
        shell semantics.

        Args:
            command: Shell command string.
            timeout: Per-command timeout in seconds.

        Returns:
            An :class:`~deepagents.backends.protocol.ExecuteResponse`.
        """
        resp = self._run_raw(command, timeout=timeout)
        return resp

    def _run_raw(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Low-level execute via SDK: wraps in ``sh -c`` and maps result."""
        assert self._sandbox is not None
        effective_timeout = timeout if timeout is not None else self._command_timeout
        shell_cmd = f"sh -c {shlex.quote(command)}"
        logger.debug("_run_raw: sandbox=%s shell_cmd=%r", self.id, shell_cmd)
        result = self._sandbox.commands.run(shell_cmd, timeout=effective_timeout)

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
        """Create or reconnect the sandbox.  Thread-safe.

        When ``claim_name`` was provided at construction time the SDK
        ``get_sandbox()`` method is used to re-attach to an existing
        SandboxClaim instead of creating a new one.
        """
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            self._client = self._create_client()
            if self._claim_name is not None:
                self._sandbox = self._client.get_sandbox(
                    claim_name=self._claim_name,
                    namespace=self._namespace,
                )
                logger.info("Sandbox reconnected: %s (claim=%s)", self.id, self._claim_name)
            else:
                assert self._template_name is not None  # validated in __init__
                self._sandbox = self._client.create_sandbox(
                    template=self._template_name,
                    namespace=self._namespace,
                    labels=self._labels,
                )
                logger.info("Sandbox started: %s", self.id)
            self._started = True

    def _destroy_sandbox(self) -> None:
        """Destroy the current sandbox.  Thread-safe and idempotent.

        In handle mode (``sandbox`` was passed to the constructor) only the
        local connection is closed — the caller owns the Kubernetes
        resource lifecycle.

        In config-based mode, when ``skip_cleanup=True`` the Kubernetes
        ``SandboxClaim`` is preserved so the sandbox pod keeps running.
        Local resources (port-forward, tracing) are still released.
        """
        with self._lock:
            if not self._started:
                return

            if self._sandbox is not None and not self._owns_lifecycle:
                # Handle mode: only close the local connection.
                saved_claim = self._sandbox.claim_name
                try:
                    self._sandbox._close_connection()
                except Exception as exc:
                    logger.warning("Error during sandbox disconnect: %s", exc)
                logger.info(
                    "Sandbox disconnected (handle mode, SandboxClaim %s preserved)",
                    saved_claim,
                )
                self._sandbox = None
            elif self._sandbox is not None and self._client is not None:
                if self._skip_cleanup:
                    saved_claim = self._sandbox.claim_name
                    try:
                        self._sandbox._close_connection()
                    except Exception as exc:
                        logger.warning("Error during sandbox disconnect: %s", exc)
                    logger.info(
                        "Sandbox disconnected (skip_cleanup=True, SandboxClaim %s preserved)",
                        saved_claim,
                    )
                else:
                    try:
                        self._client.delete_sandbox(
                            self._sandbox.claim_name,
                            self._namespace,
                        )
                    except Exception as exc:
                        logger.warning("Error during sandbox cleanup: %s", exc)
                    logger.info("Sandbox stopped")
                self._sandbox = None
                self._client = None
            self._started = False

    def _create_client(self) -> SandboxClient:
        """Build a new ``SandboxClient`` from stored configuration."""
        from k8s_agent_sandbox import SandboxClient as _SandboxClient
        from k8s_agent_sandbox.models import (
            SandboxDirectConnectionConfig,
            SandboxGatewayConnectionConfig,
            SandboxLocalTunnelConnectionConfig,
        )

        if self._api_url is not None:
            connection_config = SandboxDirectConnectionConfig(
                api_url=self._api_url,
                server_port=self._server_port,
            )
        elif self._gateway_name is not None:
            connection_config = SandboxGatewayConnectionConfig(
                gateway_name=self._gateway_name,
                gateway_namespace=self._gateway_namespace,
                server_port=self._server_port,
            )
        else:
            connection_config = SandboxLocalTunnelConnectionConfig(
                server_port=self._server_port,
            )

        logger.debug("_create_client: connection_config=%s", connection_config)
        return _SandboxClient(connection_config=connection_config)


def create_kubernetes_sandbox(
    *,
    client: SandboxClient,
    claim_name: str,
    template_name: str,
    namespace: str = "default",
    labels: dict[str, str] | None = None,
    sandbox_ready_timeout: int = 180,
    **kwargs: Any,
) -> KubernetesSandbox:
    """Get-or-create a :class:`KubernetesSandbox` by ``claim_name``.

    Encapsulates the get-or-create pattern recommended by the LangChain
    Deep Agents production guide.  If a sandbox with the given
    ``claim_name`` already exists it is reused; otherwise a new one is
    created from ``template_name``.

    The ``claim_name`` is used as the Kubernetes ``SandboxClaim`` resource
    name, making lookups deterministic.  This is necessary because the
    SDK's ``create_sandbox()`` auto-generates claim names, which would
    break the get-or-create pattern.

    The returned backend wraps the sandbox handle directly (ecosystem-
    standard mode).  Lifecycle management stays with the caller — call
    ``client.delete_sandbox(claim_name, namespace)`` when done.

    Usage in a thread-scoped graph factory::

        from k8s_agent_sandbox import SandboxClient
        from langchain_k8s import create_kubernetes_sandbox

        client = SandboxClient(connection_config=...)

        async def agent(config: RunnableConfig):
            thread_id = config["configurable"]["thread_id"]
            backend = create_kubernetes_sandbox(
                client=client,
                claim_name=f"sandbox-{thread_id}",
                template_name="python-sandbox-template",
                namespace="agent-sandbox-system",
                labels={"thread_id": thread_id},
            )
            return create_deep_agent(model=..., backend=backend)

    Args:
        client: A ``SandboxClient`` instance for managing Kubernetes
            sandbox resources.
        claim_name: Kubernetes ``SandboxClaim`` resource name to look up
            or create.  Must be a valid DNS subdomain name (lowercase
            letters, digits, and hyphens; max 63 characters).
        template_name: ``SandboxTemplate`` CRD name used when creating a
            new sandbox.
        namespace: Kubernetes namespace for the sandbox resources.
        labels: Labels applied to the ``SandboxClaim`` at creation time.
            Ignored when reconnecting to an existing sandbox.
        sandbox_ready_timeout: Maximum seconds to wait for a newly created
            sandbox to become ready.  Defaults to 180.
        **kwargs: Additional keyword arguments forwarded to the
            :class:`KubernetesSandbox` constructor (e.g.
            ``allow_prefixes``, ``virtual_mode``, ``root_dir``).

    Returns:
        A :class:`KubernetesSandbox` wrapping the resolved or newly
        created sandbox handle.  The backend is in
        ecosystem-standard mode — lifecycle management stays with
        the caller.

    Raises:
        kubernetes.client.ApiException: On Kubernetes API errors
            (e.g. RBAC, namespace not found, template missing).
        TimeoutError: If the newly created sandbox does not become
            ready within *sandbox_ready_timeout* seconds.
    """
    from k8s_agent_sandbox.exceptions import SandboxNotFoundError as _SandboxNotFoundError
    from kubernetes import client as _k8s_client

    # Fast check: does the SandboxClaim resource exist?  A direct GET
    # returns immediately (~20 ms) instead of the 30 s watch timeout
    # that client.get_sandbox() uses internally via resolve_sandbox_name.
    _claim_exists = True
    try:
        client.k8s_helper.custom_objects_api.get_namespaced_custom_object(
            group="extensions.agents.x-k8s.io",
            version="v1alpha1",
            namespace=namespace,
            plural="sandboxclaims",
            name=claim_name,
        )
    except _k8s_client.ApiException as _exc:
        if _exc.status == 404:
            _claim_exists = False
        else:
            raise

    if _claim_exists:
        try:
            sandbox_handle = client.get_sandbox(claim_name=claim_name, namespace=namespace)
        except _SandboxNotFoundError:
            _claim_exists = False

    if not _claim_exists:
        # Create a SandboxClaim with the user-specified name so it can
        # be found by claim_name on subsequent calls.  The SDK's
        # create_sandbox() auto-generates claim names, so we create the
        # claim resource directly.
        #
        # Handle 409 Conflict for concurrent callers: if another process
        # created the claim between our GET and this POST, fall back to
        # attaching to the existing claim.
        _we_created = False
        try:
            client.k8s_helper.create_sandbox_claim(
                claim_name,
                template_name,
                namespace,
                labels=labels,
            )
            _we_created = True
        except _k8s_client.ApiException as _exc:
            if _exc.status != 409:
                raise

        if _we_created:
            try:
                sandbox_id = client.k8s_helper.resolve_sandbox_name(
                    claim_name,
                    namespace,
                    timeout=sandbox_ready_timeout,
                )
                client.k8s_helper.wait_for_sandbox_ready(
                    sandbox_id,
                    namespace,
                    timeout=sandbox_ready_timeout,
                )
            except Exception:
                client.k8s_helper.delete_sandbox_claim(claim_name, namespace)
                raise

        sandbox_handle = client.get_sandbox(
            claim_name=claim_name,
            namespace=namespace,
        )
    return KubernetesSandbox(sandbox=sandbox_handle, namespace=namespace, **kwargs)


def _validate_path(path: str) -> FileOperationError | None:
    """Return an error literal if *path* is syntactically invalid, else ``None``.

    Args:
        path: The file path to validate.  Must be a non-empty string
            starting with ``"/"``.

    Returns:
        ``"invalid_path"`` if the path is empty or does not start with
        ``"/"``; ``None`` otherwise.
    """
    if not path or not path.startswith("/"):
        return "invalid_path"
    return None


def _classify_error(output: str) -> FileOperationError:
    """Map shell stderr text to a :data:`~deepagents.backends.protocol.FileOperationError`.

    Scans *output* (case-insensitively) for common POSIX error messages
    and returns the corresponding typed error literal.

    Args:
        output: Combined stdout/stderr from a failed shell command.

    Returns:
        One of ``"permission_denied"``, ``"is_directory"``, or
        ``"file_not_found"`` (the default fallback).
    """
    lower = output.lower()
    if "permission denied" in lower:
        return "permission_denied"
    if "is a directory" in lower:
        return "is_directory"
    if "no such file or directory" in lower:
        return "file_not_found"
    return "file_not_found"
