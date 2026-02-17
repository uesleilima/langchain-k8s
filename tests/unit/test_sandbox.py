"""Unit tests for KubernetesSandbox with mocked SandboxClient."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from deepagents.backends.protocol import ExecuteResponse

from langchain_k8s import KubernetesSandbox
from tests.conftest import FakeExecutionResult, make_mock_client

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sandbox(**overrides: object) -> tuple[KubernetesSandbox, MagicMock]:
    """Create a KubernetesSandbox with a mocked SDK client.

    Returns (sandbox, mock_client).
    """
    mock_client = make_mock_client()
    defaults = {
        "template_name": "test-tpl",
        "namespace": "test-ns",
    }
    defaults.update(overrides)  # type: ignore[arg-type]
    with patch("agentic_sandbox.SandboxClient", return_value=mock_client):
        sb = KubernetesSandbox(**defaults)  # type: ignore[arg-type]
    return sb, mock_client


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_not_started_on_init(self) -> None:
        sb, _ = _make_sandbox()
        assert not sb._started
        assert sb._client is None

    def test_stores_config(self) -> None:
        sb, _ = _make_sandbox(
            template_name="my-tpl",
            namespace="my-ns",
            api_url="http://localhost:8080",
            server_port=9999,
            reuse_sandbox=False,
            max_output_size=512,
            command_timeout=30,
        )
        assert sb._template_name == "my-tpl"
        assert sb._namespace == "my-ns"
        assert sb._api_url == "http://localhost:8080"
        assert sb._server_port == 9999
        assert sb._reuse_sandbox is False
        assert sb._max_output_size == 512
        assert sb._command_timeout == 30


# ---------------------------------------------------------------------------
# id property
# ---------------------------------------------------------------------------


class TestId:
    def test_id_before_start_returns_uuid(self) -> None:
        sb, _ = _make_sandbox()
        id_val = sb.id
        assert isinstance(id_val, str)
        assert len(id_val) > 0

    def test_id_after_start_returns_claim_name(self) -> None:
        mock = make_mock_client(claim_name="my-claim-123")
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            assert sb.id == "my-claim-123"
            sb.stop()

    def test_id_stable_before_start(self) -> None:
        sb, _ = _make_sandbox()
        assert sb.id == sb.id  # same value both times


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_creates_client(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            assert sb._started
            assert sb._client is not None
            mock.__enter__.assert_called_once()
            sb.stop()

    def test_start_is_idempotent(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            sb.start()  # second call should be no-op
            mock.__enter__.assert_called_once()
            sb.stop()

    def test_stop_destroys_sandbox(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            sb.stop()
            assert not sb._started
            assert sb._client is None
            mock.__exit__.assert_called_once()

    def test_stop_is_idempotent(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            sb.stop()
            sb.stop()  # no error on second call
            mock.__exit__.assert_called_once()

    def test_context_manager(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            with KubernetesSandbox(template_name="t", namespace="n") as sb:
                assert sb._started
            assert not sb._started
            mock.__enter__.assert_called_once()
            mock.__exit__.assert_called_once()

    def test_stop_tolerates_exit_errors(self) -> None:
        mock = make_mock_client()
        mock.__exit__.side_effect = RuntimeError("cleanup boom")
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            sb.stop()  # should not raise
            assert not sb._started


# ---------------------------------------------------------------------------
# execute()
# ---------------------------------------------------------------------------


class TestExecute:
    def test_basic_execute(self) -> None:
        mock = make_mock_client(run_result=FakeExecutionResult(stdout="hello\n", exit_code=0))
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            resp = sb.execute("echo hello")
            assert isinstance(resp, ExecuteResponse)
            assert resp.output == "hello\n"
            assert resp.exit_code == 0
            assert resp.truncated is False
            sb.stop()

    def test_execute_combines_stdout_stderr(self) -> None:
        mock = make_mock_client(
            run_result=FakeExecutionResult(stdout="out", stderr="err", exit_code=1)
        )
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            resp = sb.execute("bad-cmd")
            assert "out" in resp.output
            assert "err" in resp.output
            assert resp.exit_code == 1
            sb.stop()

    def test_execute_only_stderr(self) -> None:
        mock = make_mock_client(
            run_result=FakeExecutionResult(stdout="", stderr="error msg", exit_code=2)
        )
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            resp = sb.execute("fail")
            assert resp.output == "error msg"
            sb.stop()

    def test_execute_truncation(self) -> None:
        big_output = "x" * 2000
        mock = make_mock_client(
            run_result=FakeExecutionResult(stdout=big_output, exit_code=0)
        )
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", max_output_size=100)
            resp = sb.execute("big")
            assert resp.truncated is True
            assert len(resp.output) == 100
            sb.stop()

    def test_execute_no_truncation_when_under_limit(self) -> None:
        mock = make_mock_client(
            run_result=FakeExecutionResult(stdout="short", exit_code=0)
        )
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", max_output_size=100)
            resp = sb.execute("short")
            assert resp.truncated is False
            sb.stop()

    def test_execute_lazy_starts_sandbox(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            assert not sb._started
            sb.execute("echo lazy")
            assert sb._started
            sb.stop()

    def test_execute_passes_timeout(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", command_timeout=42)
            sb.execute("echo hi")
            mock.run.assert_called_once_with("sh -c 'echo hi'", timeout=42)
            sb.stop()


# ---------------------------------------------------------------------------
# Auto-reconnect (reuse_sandbox=True)
# ---------------------------------------------------------------------------


class TestAutoReconnect:
    def test_reconnects_on_connection_error(self) -> None:
        mock = make_mock_client()
        call_count = 0

        def flaky_run(cmd: str, timeout: int = 60) -> FakeExecutionResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("sandbox died")
            return FakeExecutionResult(stdout="recovered", exit_code=0)

        mock.run = flaky_run
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", reuse_sandbox=True)
            resp = sb.execute("cmd")
            assert resp.output == "recovered"
            sb.stop()

    def test_no_reconnect_when_reuse_false(self) -> None:
        mock = make_mock_client()
        mock.run.side_effect = ConnectionError("sandbox died")
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", reuse_sandbox=False)
            with pytest.raises(ConnectionError, match="sandbox died"):
                sb.execute("cmd")
            sb.stop()


# ---------------------------------------------------------------------------
# upload_files()
# ---------------------------------------------------------------------------


class TestUploadFiles:
    def test_upload_single_file(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("/tmp/hello.txt", b"world")])
            assert len(results) == 1
            assert results[0].path == "/tmp/hello.txt"
            assert results[0].error is None
            # Verify run() was called with a base64 command
            cmd = mock.run.call_args[0][0]
            assert "base64 -d" in cmd
            assert "/tmp/hello.txt" in cmd
            sb.stop()

    def test_upload_multiple_files(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            files = [
                ("/a.txt", b"aaa"),
                ("/b.txt", b"bbb"),
                ("/c.txt", b"ccc"),
            ]
            results = sb.upload_files(files)
            assert len(results) == 3
            assert all(r.error is None for r in results)
            sb.stop()

    def test_upload_invalid_path(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("relative/path.txt", b"data")])
            assert results[0].error == "invalid_path"
            # run() should not have been called for the file (only for lazy init)
            sb.stop()

    def test_upload_empty_path(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("", b"data")])
            assert results[0].error == "invalid_path"
            sb.stop()

    def test_upload_permission_denied(self) -> None:
        mock = make_mock_client(
            run_result=FakeExecutionResult(
                stderr="sh: Permission denied", exit_code=1
            )
        )
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("/root/secret.txt", b"data")])
            assert results[0].error == "permission_denied"
            sb.stop()

    def test_upload_is_directory(self) -> None:
        mock = make_mock_client(
            run_result=FakeExecutionResult(
                stderr="Is a directory", exit_code=1
            )
        )
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("/tmp/", b"data")])
            assert results[0].error == "is_directory"
            sb.stop()

    def test_upload_partial_success(self) -> None:
        """First file succeeds, second fails."""
        mock = make_mock_client()
        call_count = 0

        def run_effect(cmd: str, timeout: int = 60) -> FakeExecutionResult:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return FakeExecutionResult(stderr="Permission denied", exit_code=1)
            return FakeExecutionResult(exit_code=0)

        mock.run = run_effect
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("/a.txt", b"ok"), ("/b.txt", b"fail")])
            assert results[0].error is None
            assert results[1].error == "permission_denied"
            sb.stop()


# ---------------------------------------------------------------------------
# download_files()
# ---------------------------------------------------------------------------


class TestDownloadFiles:
    def test_download_single_file(self) -> None:
        import base64 as b64

        encoded = b64.b64encode(b"file content here").decode("ascii")
        mock = make_mock_client(
            run_result=FakeExecutionResult(stdout=encoded + "\n", exit_code=0)
        )
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.download_files(["/tmp/test.txt"])
            assert len(results) == 1
            assert results[0].path == "/tmp/test.txt"
            assert results[0].content == b"file content here"
            assert results[0].error is None
            sb.stop()

    def test_download_file_not_found(self) -> None:
        mock = make_mock_client(
            run_result=FakeExecutionResult(
                stderr="base64: /nonexistent.txt: No such file or directory",
                exit_code=1,
            )
        )
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.download_files(["/nonexistent.txt"])
            assert results[0].content is None
            assert results[0].error == "file_not_found"
            sb.stop()

    def test_download_permission_denied(self) -> None:
        mock = make_mock_client(
            run_result=FakeExecutionResult(
                stderr="base64: /root/secret: Permission denied",
                exit_code=1,
            )
        )
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.download_files(["/root/secret"])
            assert results[0].error == "permission_denied"
            sb.stop()

    def test_download_is_directory(self) -> None:
        mock = make_mock_client(
            run_result=FakeExecutionResult(
                stderr="base64: /tmp/: Is a directory",
                exit_code=1,
            )
        )
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.download_files(["/tmp/"])
            assert results[0].error == "is_directory"
            sb.stop()

    def test_download_invalid_path(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.download_files(["relative.txt"])
            assert results[0].error == "invalid_path"
            assert results[0].content is None
            sb.stop()

    def test_download_multiple_files(self) -> None:
        import base64 as b64

        call_count = 0

        def run_effect(cmd: str, timeout: int = 60) -> FakeExecutionResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FakeExecutionResult(
                    stdout=b64.b64encode(b"aaa").decode() + "\n", exit_code=0
                )
            return FakeExecutionResult(
                stdout=b64.b64encode(b"bbb").decode() + "\n", exit_code=0
            )

        mock = make_mock_client()
        mock.run = run_effect
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.download_files(["/a.txt", "/b.txt"])
            assert results[0].content == b"aaa"
            assert results[1].content == b"bbb"
            sb.stop()

    def test_download_partial_success(self) -> None:
        import base64 as b64

        call_count = 0

        def run_effect(cmd: str, timeout: int = 60) -> FakeExecutionResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FakeExecutionResult(
                    stdout=b64.b64encode(b"ok").decode() + "\n", exit_code=0
                )
            return FakeExecutionResult(
                stderr="No such file or directory", exit_code=1
            )

        mock = make_mock_client()
        mock.run = run_effect
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.download_files(["/a.txt", "/missing.txt"])
            assert results[0].error is None
            assert results[1].error == "file_not_found"
            sb.stop()


# ---------------------------------------------------------------------------
# reuse_sandbox strategies
# ---------------------------------------------------------------------------


class TestReuseSandbox:
    def test_reuse_true_keeps_same_client(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", reuse_sandbox=True)
            sb.execute("echo 1")
            client_after_first = sb._client
            sb.execute("echo 2")
            assert sb._client is client_after_first
            sb.stop()

    def test_reuse_false_can_restart(self) -> None:
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", reuse_sandbox=False)
            sb.start()
            sb.stop()
            sb.start()  # should work fine after stop
            assert sb._started
            sb.stop()


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_execute_starts_once(self) -> None:
        mock = make_mock_client()
        enter_count = 0
        original_enter = mock.__enter__

        def counting_enter(*args: object) -> MagicMock:
            nonlocal enter_count
            enter_count += 1
            return original_enter(*args)

        mock.__enter__ = counting_enter

        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            errors: list[Exception] = []

            def worker() -> None:
                try:
                    sb.execute("echo thread")
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors
            assert enter_count == 1
            sb.stop()

    def test_sequential_execute_reuses_sandbox(self) -> None:
        """Multiple sequential execute() calls reuse the same sandbox."""
        mock = make_mock_client()
        call_count = 0

        def counting_run(cmd: str, timeout: int = 60) -> FakeExecutionResult:
            nonlocal call_count
            call_count += 1
            return FakeExecutionResult(stdout=f"call-{call_count}", exit_code=0)

        mock.run = counting_run

        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            r1 = sb.execute("echo 1")
            r2 = sb.execute("echo 2")
            r3 = sb.execute("echo 3")
            assert r1.output == "call-1"
            assert r2.output == "call-2"
            assert r3.output == "call-3"
            # __enter__ called only once
            mock.__enter__.assert_called_once()
            sb.stop()

    def test_concurrent_mixed_operations_start_once(self) -> None:
        """execute(), upload_files(), download_files() all lazy-init the same sandbox."""
        import base64 as b64

        mock = make_mock_client(
            run_result=FakeExecutionResult(
                stdout=b64.b64encode(b"data").decode() + "\n", exit_code=0
            )
        )
        enter_count = 0
        original_enter = mock.__enter__

        def counting_enter(*args: object) -> MagicMock:
            nonlocal enter_count
            enter_count += 1
            return original_enter(*args)

        mock.__enter__ = counting_enter

        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            errors: list[Exception] = []

            def exec_worker() -> None:
                try:
                    sb.execute("echo mixed")
                except Exception as e:
                    errors.append(e)

            def upload_worker() -> None:
                try:
                    sb.upload_files([("/tmp/test.txt", b"content")])
                except Exception as e:
                    errors.append(e)

            def download_worker() -> None:
                try:
                    sb.download_files(["/tmp/test.txt"])
                except Exception as e:
                    errors.append(e)

            threads = [
                threading.Thread(target=exec_worker),
                threading.Thread(target=exec_worker),
                threading.Thread(target=upload_worker),
                threading.Thread(target=upload_worker),
                threading.Thread(target=download_worker),
                threading.Thread(target=download_worker),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors
            assert enter_count == 1
            sb.stop()

    def test_concurrent_stop_is_safe(self) -> None:
        """Multiple threads calling stop() concurrently doesn't double-destroy."""
        mock = make_mock_client()
        with patch("agentic_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            errors: list[Exception] = []

            def stop_worker() -> None:
                try:
                    sb.stop()
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=stop_worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors
            assert not sb._started
            mock.__exit__.assert_called_once()


# ---------------------------------------------------------------------------
# Client creation kwargs
# ---------------------------------------------------------------------------


class TestClientCreation:
    def test_passes_gateway_name(self) -> None:
        with patch("agentic_sandbox.SandboxClient") as MockClient:
            MockClient.return_value = make_mock_client()
            sb = KubernetesSandbox(
                template_name="tpl",
                namespace="ns",
                gateway_name="my-gw",
                gateway_namespace="gw-ns",
            )
            sb.start()
            MockClient.assert_called_once()
            kwargs = MockClient.call_args[1]
            assert kwargs["gateway_name"] == "my-gw"
            assert kwargs["gateway_namespace"] == "gw-ns"
            sb.stop()

    def test_passes_api_url(self) -> None:
        with patch("agentic_sandbox.SandboxClient") as MockClient:
            MockClient.return_value = make_mock_client()
            sb = KubernetesSandbox(
                template_name="tpl",
                namespace="ns",
                api_url="http://localhost:8080",
            )
            sb.start()
            kwargs = MockClient.call_args[1]
            assert kwargs["api_url"] == "http://localhost:8080"
            assert "gateway_name" not in kwargs
            sb.stop()

    def test_passes_server_port(self) -> None:
        with patch("agentic_sandbox.SandboxClient") as MockClient:
            MockClient.return_value = make_mock_client()
            sb = KubernetesSandbox(
                template_name="tpl",
                namespace="ns",
                server_port=9999,
            )
            sb.start()
            kwargs = MockClient.call_args[1]
            assert kwargs["server_port"] == 9999
            sb.stop()
