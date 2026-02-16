"""Kubernetes Agent Sandbox backend for LangChain Deep Agents."""

from __future__ import annotations

import logging
import threading
import uuid
from typing import TYPE_CHECKING

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileOperationError,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

if TYPE_CHECKING:
    from agentic_sandbox import SandboxClient

logger = logging.getLogger(__name__)

_DEFAULT_MAX_OUTPUT_SIZE = 1_048_576  # 1 MB
_DEFAULT_COMMAND_TIMEOUT = 300  # 5 minutes


class KubernetesSandbox(BaseSandbox):
    """LangChain sandbox backend for kubernetes-sigs/agent-sandbox.

    Wraps the ``k8s-agent-sandbox`` Python SDK (``agentic_sandbox.SandboxClient``)
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

        self._client: SandboxClient | None = None
        self._id: str = str(uuid.uuid4())
        self._lock = threading.Lock()
        self._started = False

    # -- BaseSandbox abstract property -----------------------------------------

    @property
    def id(self) -> str:  # noqa: A003
        """Unique identifier for this sandbox backend instance.

        Returns the Kubernetes sandbox claim name if a sandbox is running,
        otherwise a stable UUID generated at construction time.
        """
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
        self._ensure_sandbox()
        assert self._client is not None  # ensured by _ensure_sandbox

        try:
            return self._run(command)
        except Exception:
            if self._reuse_sandbox:
                logger.warning("Sandbox connection lost — reconnecting")
                self._destroy_sandbox()
                self._ensure_sandbox()
                assert self._client is not None
                return self._run(command)
            raise

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Upload files into the sandbox using the native SDK ``write()`` API."""
        self._ensure_sandbox()
        assert self._client is not None

        results: list[FileUploadResponse] = []
        for path, content in files:
            error = _validate_path(path)
            if error is not None:
                results.append(FileUploadResponse(path=path, error=error))
                continue
            try:
                self._client.write(path, content)
                results.append(FileUploadResponse(path=path, error=None))
            except PermissionError:
                results.append(FileUploadResponse(path=path, error="permission_denied"))
            except IsADirectoryError:
                results.append(FileUploadResponse(path=path, error="is_directory"))
            except Exception as exc:
                logger.warning("upload_files failed for %s: %s", path, exc)
                results.append(FileUploadResponse(path=path, error="invalid_path"))
        return results

    def download_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Download files from the sandbox using the native SDK ``read()`` API."""
        self._ensure_sandbox()
        assert self._client is not None

        results: list[FileDownloadResponse] = []
        for path in paths:
            error = _validate_path(path)
            if error is not None:
                results.append(FileDownloadResponse(path=path, content=None, error=error))
                continue
            try:
                content = self._client.read(path)
                results.append(FileDownloadResponse(path=path, content=content, error=None))
            except FileNotFoundError:
                results.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
            except PermissionError:
                results.append(FileDownloadResponse(path=path, content=None, error="permission_denied"))
            except IsADirectoryError:
                results.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
            except Exception as exc:
                logger.warning("download_files failed for %s: %s", path, exc)
                results.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
        return results

    # -- Internals -------------------------------------------------------------

    def _run(self, command: str) -> ExecuteResponse:
        """Execute via SDK and map to ``ExecuteResponse``."""
        assert self._client is not None
        result = self._client.run(command, timeout=self._command_timeout)

        parts: list[str] = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(result.stderr)
        output = "\n".join(parts)

        truncated = len(output) > self._max_output_size
        if truncated:
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
        """Destroy the current sandbox.  Thread-safe and idempotent."""
        with self._lock:
            if not self._started:
                return
            if self._client is not None:
                try:
                    self._client.__exit__(None, None, None)
                except Exception as exc:
                    logger.warning("Error during sandbox cleanup: %s", exc)
                self._client = None
            self._started = False
            logger.info("Sandbox stopped")

    def _create_client(self) -> SandboxClient:
        """Build a new ``SandboxClient`` from stored configuration."""
        from agentic_sandbox import SandboxClient as _SandboxClient

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
        return _SandboxClient(**kwargs)


def _validate_path(path: str) -> FileOperationError | None:
    """Return an error literal if *path* is syntactically invalid, else ``None``."""
    if not path or not path.startswith("/"):
        return "invalid_path"
    return None
