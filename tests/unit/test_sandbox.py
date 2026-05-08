"""Unit tests for KubernetesSandbox with mocked SandboxClient."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from deepagents.backends.protocol import ExecuteResponse

from langchain_k8s import KubernetesSandbox, create_kubernetes_sandbox
from tests.conftest import FakeExecutionResult, make_mock_client

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sandbox(**overrides: object) -> tuple[KubernetesSandbox, MagicMock, MagicMock]:
    """Create a KubernetesSandbox with a mocked SDK client.

    Returns (sandbox, client_mock, sandbox_handle_mock).
    """
    mock_client = make_mock_client()
    defaults = {
        "template_name": "test-tpl",
        "namespace": "test-ns",
    }
    defaults.update(overrides)  # type: ignore[arg-type]
    with patch("k8s_agent_sandbox.SandboxClient", return_value=mock_client):
        sb = KubernetesSandbox(**defaults)  # type: ignore[arg-type]
    return sb, mock_client, mock_client._mock_sandbox_handle


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_not_started_on_init(self) -> None:
        sb, _, _ = _make_sandbox()
        assert not sb._started
        assert sb._client is None
        assert sb._sandbox is None

    def test_stores_config(self) -> None:
        sb, _, _ = _make_sandbox(
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
        sb, _, _ = _make_sandbox()
        id_val = sb.id
        assert isinstance(id_val, str)
        assert len(id_val) > 0

    def test_id_after_start_returns_claim_name(self) -> None:
        mock = make_mock_client(claim_name="my-claim-123")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            assert sb.id == "my-claim-123"
            sb.stop()

    def test_id_stable_before_start(self) -> None:
        sb, _, _ = _make_sandbox()
        first = sb.id
        second = sb.id
        assert first == second  # same value both times


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_creates_client(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            assert sb._started
            assert sb._sandbox is not None
            mock.create_sandbox.assert_called_once()
            sb.stop()

    def test_start_is_idempotent(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            sb.start()  # second call should be no-op
            mock.create_sandbox.assert_called_once()
            sb.stop()

    def test_stop_destroys_sandbox(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            sb.stop()
            assert not sb._started
            assert sb._client is None
            mock.delete_sandbox.assert_called_once()

    def test_stop_is_idempotent(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            sb.stop()
            sb.stop()  # no error on second call
            mock.delete_sandbox.assert_called_once()

    def test_context_manager(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            with KubernetesSandbox(template_name="t", namespace="n") as sb:
                assert sb._started
            assert not sb._started
            mock.create_sandbox.assert_called_once()
            mock.delete_sandbox.assert_called_once()

    def test_stop_tolerates_exit_errors(self) -> None:
        mock = make_mock_client()
        mock.delete_sandbox.side_effect = RuntimeError("cleanup boom")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            resp = sb.execute("echo hello")
            assert isinstance(resp, ExecuteResponse)
            assert resp.output == "hello\n"
            assert resp.exit_code == 0
            assert resp.truncated is False
            sb.stop()

    def test_execute_combines_stdout_stderr(self) -> None:
        mock = make_mock_client(run_result=FakeExecutionResult(stdout="out", stderr="err", exit_code=1))
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            resp = sb.execute("bad-cmd")
            assert "out" in resp.output
            assert "err" in resp.output
            assert resp.exit_code == 1
            sb.stop()

    def test_execute_only_stderr(self) -> None:
        mock = make_mock_client(run_result=FakeExecutionResult(stdout="", stderr="error msg", exit_code=2))
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            resp = sb.execute("fail")
            assert resp.output == "error msg"
            sb.stop()

    def test_execute_truncation(self) -> None:
        big_output = "x" * 2000
        mock = make_mock_client(run_result=FakeExecutionResult(stdout=big_output, exit_code=0))
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", max_output_size=100)
            resp = sb.execute("big")
            assert resp.truncated is True
            assert len(resp.output) == 100
            sb.stop()

    def test_execute_no_truncation_when_under_limit(self) -> None:
        mock = make_mock_client(run_result=FakeExecutionResult(stdout="short", exit_code=0))
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", max_output_size=100)
            resp = sb.execute("short")
            assert resp.truncated is False
            sb.stop()

    def test_execute_lazy_starts_sandbox(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            assert not sb._started
            sb.execute("echo lazy")
            assert sb._started
            sb.stop()

    def test_execute_passes_timeout(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", command_timeout=42)
            sb.execute("echo hi")
            handle.commands.run.assert_called_once_with("sh -c 'echo hi'", timeout=42)
            sb.stop()


# ---------------------------------------------------------------------------
# Auto-reconnect (reuse_sandbox=True)
# ---------------------------------------------------------------------------


class TestAutoReconnect:
    def test_reconnects_on_connection_error(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        call_count = 0

        def flaky_run(cmd: str, timeout: int = 60) -> FakeExecutionResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("sandbox died")
            return FakeExecutionResult(stdout="recovered", exit_code=0)

        handle.commands.run = flaky_run
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", reuse_sandbox=True)
            resp = sb.execute("cmd")
            assert resp.output == "recovered"
            sb.stop()

    def test_no_reconnect_when_reuse_false(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        handle.commands.run.side_effect = ConnectionError("sandbox died")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("/tmp/hello.txt", b"world")])
            assert len(results) == 1
            assert results[0].path == "/tmp/hello.txt"
            assert results[0].error is None
            # Verify run() was called with a base64 command
            cmd = handle.commands.run.call_args[0][0]
            assert "base64 -d" in cmd
            assert "/tmp/hello.txt" in cmd
            sb.stop()

    def test_upload_multiple_files(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("relative/path.txt", b"data")])
            assert results[0].error == "invalid_path"
            # run() should not have been called for the file (only for lazy init)
            sb.stop()

    def test_upload_empty_path(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("", b"data")])
            assert results[0].error == "invalid_path"
            sb.stop()

    def test_upload_permission_denied(self) -> None:
        mock = make_mock_client(run_result=FakeExecutionResult(stderr="sh: Permission denied", exit_code=1))
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("/root/secret.txt", b"data")])
            assert results[0].error == "permission_denied"
            sb.stop()

    def test_upload_is_directory(self) -> None:
        mock = make_mock_client(run_result=FakeExecutionResult(stderr="Is a directory", exit_code=1))
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.upload_files([("/tmp/", b"data")])
            assert results[0].error == "is_directory"
            sb.stop()

    def test_upload_partial_success(self) -> None:
        """First file succeeds, second fails."""
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        call_count = 0

        def run_effect(cmd: str, timeout: int = 60) -> FakeExecutionResult:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return FakeExecutionResult(stderr="Permission denied", exit_code=1)
            return FakeExecutionResult(exit_code=0)

        handle.commands.run = run_effect
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        mock = make_mock_client(run_result=FakeExecutionResult(stdout=encoded + "\n", exit_code=0))
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            results = sb.download_files(["/tmp/"])
            assert results[0].error == "is_directory"
            sb.stop()

    def test_download_invalid_path(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
                return FakeExecutionResult(stdout=b64.b64encode(b"aaa").decode() + "\n", exit_code=0)
            return FakeExecutionResult(stdout=b64.b64encode(b"bbb").decode() + "\n", exit_code=0)

        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        handle.commands.run = run_effect
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
                return FakeExecutionResult(stdout=b64.b64encode(b"ok").decode() + "\n", exit_code=0)
            return FakeExecutionResult(stderr="No such file or directory", exit_code=1)

        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        handle.commands.run = run_effect
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", reuse_sandbox=True)
            sb.execute("echo 1")
            client_after_first = sb._client
            sb.execute("echo 2")
            assert sb._client is client_after_first
            sb.stop()

    def test_reuse_false_can_restart(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        original_enter = mock.create_sandbox

        def counting_enter(*args: object, **kwargs: object) -> MagicMock:
            nonlocal enter_count
            enter_count += 1
            return original_enter(*args, **kwargs)

        mock.create_sandbox = counting_enter

        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        handle = mock._mock_sandbox_handle
        call_count = 0

        def counting_run(cmd: str, timeout: int = 60) -> FakeExecutionResult:
            nonlocal call_count
            call_count += 1
            return FakeExecutionResult(stdout=f"call-{call_count}", exit_code=0)

        handle.commands.run = counting_run

        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            r1 = sb.execute("echo 1")
            r2 = sb.execute("echo 2")
            r3 = sb.execute("echo 3")
            assert r1.output == "call-1"
            assert r2.output == "call-2"
            assert r3.output == "call-3"
            # __enter__ called only once
            mock.create_sandbox.assert_called_once()
            sb.stop()

    def test_concurrent_mixed_operations_start_once(self) -> None:
        """execute(), upload_files(), download_files() all lazy-init the same sandbox."""
        import base64 as b64

        mock = make_mock_client(
            run_result=FakeExecutionResult(stdout=b64.b64encode(b"data").decode() + "\n", exit_code=0)
        )
        enter_count = 0
        original_enter = mock.create_sandbox

        def counting_enter(*args: object, **kwargs: object) -> MagicMock:
            nonlocal enter_count
            enter_count += 1
            return original_enter(*args, **kwargs)

        mock.create_sandbox = counting_enter

        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
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
            mock.delete_sandbox.assert_called_once()


# ---------------------------------------------------------------------------
# Client creation kwargs
# ---------------------------------------------------------------------------


class TestClientCreation:
    def test_passes_gateway_config(self) -> None:
        with patch("k8s_agent_sandbox.SandboxClient") as MockClient:
            MockClient.return_value = make_mock_client()
            sb = KubernetesSandbox(
                template_name="tpl",
                namespace="ns",
                gateway_name="my-gw",
                gateway_namespace="gw-ns",
            )
            sb.start()
            MockClient.assert_called_once()
            config = MockClient.call_args[1]["connection_config"]
            assert config.gateway_name == "my-gw"
            assert config.gateway_namespace == "gw-ns"
            sb.stop()

    def test_passes_api_url_config(self) -> None:
        with patch("k8s_agent_sandbox.SandboxClient") as MockClient:
            MockClient.return_value = make_mock_client()
            sb = KubernetesSandbox(
                template_name="tpl",
                namespace="ns",
                api_url="http://localhost:8080",
            )
            sb.start()
            config = MockClient.call_args[1]["connection_config"]
            assert config.api_url == "http://localhost:8080"
            assert not hasattr(config, "gateway_name")
            sb.stop()

    def test_passes_server_port(self) -> None:
        with patch("k8s_agent_sandbox.SandboxClient") as MockClient:
            MockClient.return_value = make_mock_client()
            sb = KubernetesSandbox(
                template_name="tpl",
                namespace="ns",
                server_port=9999,
            )
            sb.start()
            config = MockClient.call_args[1]["connection_config"]
            assert config.server_port == 9999
            sb.stop()


# ---------------------------------------------------------------------------
# allow_prefixes policy hook
# ---------------------------------------------------------------------------


class TestAllowPrefixes:
    def test_default_allow_prefixes_is_none(self) -> None:
        sb, _, _ = _make_sandbox()
        assert sb._allow_prefixes is None

    def test_allow_prefixes_none_allows_all_writes(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.write("/etc/test.txt", "content")
            handle.commands.run.assert_called()
            sb.stop()

    def test_allow_prefixes_blocks_path_outside_list(self) -> None:
        sb, _, mock = _make_sandbox(allow_prefixes=["/workspace/"])
        result = sb.write("/etc/passwd", "malicious content")
        assert result.error is not None
        assert "not under any allowed prefix" in result.error
        mock.commands.run.assert_not_called()

    def test_allow_prefixes_allows_path_under_prefix(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", allow_prefixes=["/workspace/"])
            sb.write("/workspace/main.py", "print('hi')")
            handle.commands.run.assert_called()
            sb.stop()

    def test_allow_prefixes_multiple_prefixes(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", allow_prefixes=["/workspace/", "/tmp/"])
            sb.write("/workspace/main.py", "content")
            handle.commands.run.assert_called()
            handle.commands.run.reset_mock()
            sb.write("/tmp/data.txt", "content")
            handle.commands.run.assert_called()
            # But /etc/ should be blocked.
            result = sb.write("/etc/test.txt", "content")
            assert result.error is not None
            assert "not under any allowed prefix" in result.error
            sb.stop()

    def test_edit_denied_by_allow_prefixes(self) -> None:
        sb, _, mock = _make_sandbox(allow_prefixes=["/workspace/"])
        result = sb.edit("/etc/hosts", "old", "new")
        assert result.error is not None
        assert "not under any allowed prefix" in result.error
        mock.commands.run.assert_not_called()

    def test_edit_allowed_by_allow_prefixes(self) -> None:
        # BaseSandbox.edit() expects the execute output to be a number (replacement count).
        mock = make_mock_client(run_result=FakeExecutionResult(stdout="1", exit_code=0))
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", allow_prefixes=["/workspace/"])
            sb.edit("/workspace/test.txt", "old", "new")
            handle.commands.run.assert_called()
            sb.stop()

    def test_allow_prefix_normalization_adds_trailing_slash(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", allow_prefixes=["/workspace"])
            # "/workspace" should be normalized to "/workspace/".
            sb.write("/workspace/file.txt", "data")
            handle.commands.run.assert_called()
            sb.stop()

    def test_allow_prefixes_stored_as_tuple(self) -> None:
        sb, _, _ = _make_sandbox(allow_prefixes=["/a/", "/b"])
        assert isinstance(sb._allow_prefixes, tuple)
        assert sb._allow_prefixes == ("/a/", "/b/")


# ---------------------------------------------------------------------------
# virtual_mode + root_dir
# ---------------------------------------------------------------------------


class TestVirtualMode:
    # -- Defaults and construction --

    def test_virtual_mode_default_is_false(self) -> None:
        sb, _, _ = _make_sandbox()
        assert sb._virtual_mode is False

    def test_root_dir_default_when_virtual_mode_true(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True)
        assert sb._root_dir == "/tmp"

    def test_root_dir_custom_when_virtual_mode_true(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/home/agent")
        assert sb._root_dir == "/home/agent"

    def test_root_dir_none_when_virtual_mode_false(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=False)
        assert sb._root_dir is None

    # -- Path resolution --

    def test_path_resolution_anchors_under_root_dir(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/workspace")
        resolved = sb._resolve_virtual_path("/src/main.py")
        assert resolved == "/workspace/src/main.py"

    def test_path_resolution_relative_path(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/workspace")
        resolved = sb._resolve_virtual_path("src/main.py")
        assert resolved == "/workspace/src/main.py"

    def test_path_traversal_blocked(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/workspace")
        with pytest.raises(ValueError, match="Path traversal not allowed"):
            sb._resolve_virtual_path("../../etc/passwd")

    def test_tilde_path_blocked(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/workspace")
        with pytest.raises(ValueError, match="Path traversal not allowed"):
            sb._resolve_virtual_path("~/.bashrc")

    def test_path_resolution_disabled_when_virtual_mode_false(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=False)
        resolved = sb._resolve_virtual_path("/etc/passwd")
        assert resolved == "/etc/passwd"

    # -- read() resolves paths --

    def test_read_resolves_path(self) -> None:
        import base64 as b64

        mock = make_mock_client(run_result=FakeExecutionResult(stdout="     1\tline1", exit_code=0))
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            sb.read("/src/main.py")
            cmd = handle.commands.run.call_args[0][0]
            # deepagents 0.5.x base64-encodes paths in shell commands
            encoded_path = b64.b64encode(b"/workspace/src/main.py").decode()
            assert encoded_path in cmd
            sb.stop()

    def test_read_returns_error_on_traversal(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/workspace")
        result = sb.read("../../etc/passwd")
        assert result.error is not None
        assert "Path traversal not allowed" in result.error

    # -- write() resolves paths --

    def test_write_resolves_path(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            sb.write("/src/main.py", "print('hi')")
            # write() resolves the path then delegates to upload_files via super().
            # The shell command should contain the resolved path exactly once.
            cmd = handle.commands.run.call_args[0][0]
            assert "/workspace/src/main.py" in cmd
            # Must NOT double-resolve (no /workspace/workspace/)
            assert "/workspace/workspace/" not in cmd
            sb.stop()

    def test_write_returns_error_on_traversal(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/workspace")
        result = sb.write("../../etc/passwd", "bad")
        assert result.error is not None
        assert "Path traversal not allowed" in result.error

    # -- edit() resolves paths --

    def test_edit_resolves_path(self) -> None:
        import base64 as b64
        import json

        mock = make_mock_client(run_result=FakeExecutionResult(stdout="1", exit_code=0))
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            sb.edit("/src/main.py", "old", "new")
            # edit() sends a base64-encoded JSON payload via stdin heredoc.
            cmd = handle.commands.run.call_args[0][0]
            payload_b64 = cmd.split("__DEEPAGENTS_EDIT_EOF__")[1].strip().strip("'\"")
            payload = json.loads(b64.b64decode(payload_b64).decode())
            assert payload["path"] == "/workspace/src/main.py"
            sb.stop()

    def test_edit_returns_error_on_traversal(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/workspace")
        result = sb.edit("../../etc/passwd", "old", "new")
        assert result.error is not None
        assert "Path traversal not allowed" in result.error

    # -- ls() resolves paths --

    def test_ls_resolves_path(self) -> None:
        import base64 as b64

        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            sb.ls("/src")
            cmd = handle.commands.run.call_args[0][0]
            encoded_path = b64.b64encode(b"/workspace/src").decode()
            assert encoded_path in cmd
            sb.stop()

    def test_ls_returns_error_on_traversal(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/workspace")
        result = sb.ls("../../etc")
        assert result.error is not None
        assert "Path traversal not allowed" in result.error

    # -- glob() resolves paths --

    def test_glob_resolves_path(self) -> None:
        import base64 as b64

        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            sb.glob("*.py", "/src")
            # glob uses base64-encoded path — verify the call was made with
            # the resolved path by checking the base64-encoded value in the command.
            assert handle.commands.run.called
            raw_cmd = handle.commands.run.call_args[0][0]
            # The resolved path "/workspace/src" is base64-encoded in the command.
            assert b64.b64encode(b"/workspace/src").decode() in raw_cmd
            sb.stop()

    def test_glob_returns_error_on_traversal(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/workspace")
        result = sb.glob("*.py", "../../etc")
        assert result.error is not None
        assert "Path traversal not allowed" in result.error

    # -- grep() resolves paths --

    def test_grep_resolves_path(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            sb.grep("TODO", "/src")
            cmd = handle.commands.run.call_args[0][0]
            assert "/workspace/src" in cmd
            sb.stop()

    def test_grep_defaults_to_root_dir_when_path_none(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            sb.grep("TODO")
            cmd = handle.commands.run.call_args[0][0]
            assert "/workspace" in cmd
            sb.stop()

    def test_grep_returns_error_on_traversal(self) -> None:
        sb, _, _ = _make_sandbox(virtual_mode=True, root_dir="/workspace")
        # grep with traversal path should return GrepResult with error.
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb2 = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            result = sb2.grep("TODO", "../../etc")
            assert result.error is not None
            assert "Path traversal not allowed" in result.error
            sb2.stop()

    # -- upload_files() resolves paths --

    def test_upload_resolves_path(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            results = sb.upload_files([("/src/test.txt", b"data")])
            assert results[0].error is None
            cmd = handle.commands.run.call_args[0][0]
            assert "/workspace/src/test.txt" in cmd
            sb.stop()

    def test_upload_blocks_traversal(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            results = sb.upload_files([("../../etc/passwd", b"bad")])
            assert results[0].error == "invalid_path"
            sb.stop()

    def test_upload_always_uses_shell_regardless_of_virtual_mode(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True)
            results = sb.upload_files([("/src/test.txt", b"data")])
            assert results[0].error is None
            # Upload should use run() (shell), not client.write().
            handle.commands.run.assert_called()
            cmd = handle.commands.run.call_args[0][0]
            assert "base64 -d" in cmd
            handle.files.write.assert_not_called()
            sb.stop()

    # -- download_files() --

    def test_shell_download_when_virtual_mode_false(self) -> None:
        import base64 as b64

        encoded = b64.b64encode(b"shell content").decode("ascii")
        mock = make_mock_client(run_result=FakeExecutionResult(stdout=encoded + "\n", exit_code=0))
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=False)
            results = sb.download_files(["/tmp/test.txt"])
            assert results[0].content == b"shell content"
            # Verify shell command was used (via run).
            cmd = handle.commands.run.call_args[0][0]
            assert "base64" in cmd
            sb.stop()

    def test_shell_download_when_virtual_mode_true(self) -> None:
        """download_files() always uses shell-based download, even in virtual mode.

        The k8s-agent-sandbox runtime restricts native /download to
        /app, making it incompatible with arbitrary absolute paths.
        """
        import base64 as b64

        encoded = b64.b64encode(b"shell content").decode("ascii")
        mock = make_mock_client(run_result=FakeExecutionResult(stdout=encoded + "\n", exit_code=0))
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            results = sb.download_files(["/src/test.txt"])
            assert results[0].content == b"shell content"
            assert results[0].error is None
            cmd = handle.commands.run.call_args[0][0]
            assert "base64" in cmd
            assert "/workspace/src/test.txt" in cmd
            handle.files.read.assert_not_called()
            sb.stop()

    def test_download_file_not_found_virtual_mode(self) -> None:
        mock = make_mock_client(run_result=FakeExecutionResult(stderr="No such file or directory", exit_code=1))
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True)
            results = sb.download_files(["/missing.txt"])
            assert results[0].content is None
            assert results[0].error == "file_not_found"
            sb.stop()

    def test_download_permission_denied_virtual_mode(self) -> None:
        mock = make_mock_client(run_result=FakeExecutionResult(stderr="Permission denied", exit_code=1))
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True)
            results = sb.download_files(["/secret.txt"])
            assert results[0].content is None
            assert results[0].error == "permission_denied"
            sb.stop()

    def test_download_relative_path_resolved_in_virtual_mode(self) -> None:
        import base64 as b64

        encoded = b64.b64encode(b"resolved content").decode("ascii")
        mock = make_mock_client(run_result=FakeExecutionResult(stdout=encoded + "\n", exit_code=0))
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True)
            results = sb.download_files(["relative.txt"])
            # In virtual mode, relative paths get "/" prepended, resolving to
            # /tmp/relative.txt which is a valid absolute path.
            assert results[0].error is None
            assert results[0].content == b"resolved content"
            cmd = handle.commands.run.call_args[0][0]
            assert "/tmp/relative.txt" in cmd
            sb.stop()

    def test_download_blocks_traversal_virtual_mode(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True, root_dir="/workspace")
            results = sb.download_files(["../../etc/passwd"])
            assert results[0].error == "invalid_path"
            assert results[0].content is None
            sb.stop()

    def test_download_multiple_files_virtual_mode(self) -> None:
        import base64 as b64

        call_count = 0

        def run_effect(cmd: str, timeout: int = 60) -> FakeExecutionResult:
            nonlocal call_count
            call_count += 1
            return FakeExecutionResult(
                stdout=b64.b64encode(f"content-{call_count}".encode()).decode() + "\n", exit_code=0
            )

        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        handle.commands.run = run_effect
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", virtual_mode=True)
            results = sb.download_files(["/a.txt", "/b.txt"])
            assert results[0].content == b"content-1"
            assert results[1].content == b"content-2"
            sb.stop()

    # -- Combined: allow_prefixes + virtual_mode --

    def test_allow_prefixes_check_against_resolved_path(self) -> None:
        """allow_prefixes should check the resolved path, not the virtual path."""
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                virtual_mode=True,
                root_dir="/workspace",
                allow_prefixes=["/workspace/"],
            )
            # Virtual path "/src/main.py" resolves to "/workspace/src/main.py" — allowed.
            sb.write("/src/main.py", "print('hi')")
            handle.commands.run.assert_called()
            sb.stop()

    def test_allow_prefixes_blocks_resolved_path_outside_prefix(self) -> None:
        """allow_prefixes blocks writes where the resolved path is outside the prefix."""
        sb, _, mock = _make_sandbox(
            virtual_mode=True,
            root_dir="/home/agent",
            allow_prefixes=["/workspace/"],
        )
        # Virtual path "/file.txt" resolves to "/home/agent/file.txt" — blocked.
        result = sb.write("/file.txt", "data")
        assert result.error is not None
        assert "not under any allowed prefix" in result.error
        mock.commands.run.assert_not_called()


# ---------------------------------------------------------------------------
# skip_cleanup
# ---------------------------------------------------------------------------


class TestSkipCleanup:
    def test_skip_cleanup_default_is_false(self) -> None:
        sb, _, _ = _make_sandbox()
        assert sb._skip_cleanup is False

    def test_skip_cleanup_usesclose_connection(self) -> None:
        """skip_cleanup=True calls close_connection() instead of delete_sandbox()."""
        mock = make_mock_client(claim_name="keep-alive")
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                skip_cleanup=True,
            )
            sb.start()
            sb.stop()

        # close_connection should be called (local cleanup only).
        handle.close_connection.assert_called_once()
        # delete_sandbox should NOT be called (preserve the SandboxClaim).
        mock.delete_sandbox.assert_not_called()

    def test_normal_stop_calls_delete_sandbox(self) -> None:
        """Normal stop (skip_cleanup=False) calls delete_sandbox()."""
        mock = make_mock_client(claim_name="delete-me")
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            sb.stop()

        # delete_sandbox should be called with claim_name and namespace.
        mock.delete_sandbox.assert_called_once_with("delete-me", "n")
        # close_connection should NOT be called.
        handle.close_connection.assert_not_called()

    def test_context_manager_respects_skip_cleanup(self) -> None:
        mock = make_mock_client(claim_name="ctx-mgr")
        handle = mock._mock_sandbox_handle
        with (
            patch("k8s_agent_sandbox.SandboxClient", return_value=mock),
            KubernetesSandbox(
                template_name="t",
                namespace="n",
                skip_cleanup=True,
            ) as sb,
        ):
            assert sb._started

        handle.close_connection.assert_called_once()
        mock.delete_sandbox.assert_not_called()

    def test_skip_cleanup_toleratesclose_connection_errors(self) -> None:
        mock = make_mock_client(claim_name="error-case")
        handle = mock._mock_sandbox_handle
        handle.close_connection.side_effect = RuntimeError("cleanup boom")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                skip_cleanup=True,
            )
            sb.start()
            sb.stop()  # should not raise
            assert not sb._started


# ---------------------------------------------------------------------------
# claim_name property
# ---------------------------------------------------------------------------


class TestClaimName:
    def test_claim_name_is_none_before_start(self) -> None:
        sb, _, _ = _make_sandbox()
        assert sb.claim_name is None

    def test_claim_name_returns_handle_value_after_start(self) -> None:
        mock = make_mock_client(claim_name="sandbox-claim-abc123")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            assert sb.claim_name == "sandbox-claim-abc123"
            sb.stop()

    def test_claim_name_returns_constructor_value_before_start(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                claim_name="pre-existing-claim",
            )
            assert sb.claim_name == "pre-existing-claim"

    def test_claim_name_prefers_live_handle(self) -> None:
        """After start, the live handle's claim_name takes precedence."""
        mock = make_mock_client(claim_name="live-claim")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                claim_name="reconnect-claim",
            )
            sb.start()
            assert sb.claim_name == "live-claim"
            sb.stop()

    def test_claim_name_is_none_after_stop(self) -> None:
        mock = make_mock_client(claim_name="ephemeral-claim")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()
            sb.stop()
            # No live handle, no constructor claim_name → None.
            assert sb.claim_name is None


# ---------------------------------------------------------------------------
# Reconnect via claim_name
# ---------------------------------------------------------------------------


class TestReconnect:
    def test_reconnect_calls_get_sandbox(self) -> None:
        """When claim_name is set, start() calls get_sandbox instead of create_sandbox."""
        mock = make_mock_client(claim_name="existing-claim")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                claim_name="existing-claim",
            )
            sb.start()

        mock.get_sandbox.assert_called_once_with(
            claim_name="existing-claim",
            namespace="n",
        )
        mock.create_sandbox.assert_not_called()
        sb.stop()

    def test_reconnect_execute_works(self) -> None:
        """After reconnect, execute() runs commands normally."""
        mock = make_mock_client(claim_name="reattach-claim")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                claim_name="reattach-claim",
            )
            sb.start()
            resp = sb.execute("echo hi")
            assert resp.exit_code == 0
            sb.stop()

    def test_reconnect_with_skip_cleanup(self) -> None:
        """Reconnect + skip_cleanup: get_sandbox to connect, close_connection on stop."""
        mock = make_mock_client(claim_name="persistent-claim")
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                claim_name="persistent-claim",
                skip_cleanup=True,
            )
            sb.start()
            sb.stop()

        mock.get_sandbox.assert_called_once()
        mock.create_sandbox.assert_not_called()
        handle.close_connection.assert_called_once()
        mock.delete_sandbox.assert_not_called()

    def test_reconnect_id_uses_claim_name(self) -> None:
        mock = make_mock_client(claim_name="my-claim-reconnect")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                claim_name="my-claim-reconnect",
            )
            sb.start()
            # id should come from the live handle's claim_name
            assert sb.id == "my-claim-reconnect"
            sb.stop()

    def test_no_claim_name_creates_sandbox(self) -> None:
        """Without claim_name, start() calls create_sandbox (default path)."""
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()

        mock.create_sandbox.assert_called_once()
        mock.get_sandbox.assert_not_called()
        sb.stop()


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


class TestLabels:
    def test_labels_default_is_none(self) -> None:
        sb, _, _ = _make_sandbox()
        assert sb._labels is None

    def test_labels_stored_on_construction(self) -> None:
        sb, _, _ = _make_sandbox(labels={"session": "abc", "env": "dev"})
        assert sb._labels == {"session": "abc", "env": "dev"}

    def test_labels_passed_to_create_sandbox(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                labels={"agent-id": "a1", "session": "s1"},
            )
            sb.start()

        mock.create_sandbox.assert_called_once_with(
            template="t",
            namespace="n",
            labels={"agent-id": "a1", "session": "s1"},
        )
        sb.stop()

    def test_labels_none_passed_as_none(self) -> None:
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n")
            sb.start()

        mock.create_sandbox.assert_called_once_with(
            template="t",
            namespace="n",
            labels=None,
        )
        sb.stop()

    def test_labels_ignored_on_reconnect(self) -> None:
        """Labels are not passed to get_sandbox (only relevant at creation)."""
        mock = make_mock_client(claim_name="existing")
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(
                template_name="t",
                namespace="n",
                claim_name="existing",
                labels={"should": "be-ignored"},
            )
            sb.start()

        mock.get_sandbox.assert_called_once_with(
            claim_name="existing",
            namespace="n",
        )
        mock.create_sandbox.assert_not_called()
        sb.stop()


# ---------------------------------------------------------------------------
# Sandbox handle constructor (ecosystem-standard mode)
# ---------------------------------------------------------------------------


class TestSandboxHandleConstructor:
    def test_handle_sets_started_immediately(self) -> None:
        handle = make_mock_client(claim_name="handle-claim")._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        assert sb._started
        assert sb._sandbox is handle

    def test_handle_id_uses_claim_name(self) -> None:
        handle = make_mock_client(claim_name="my-handle-claim")._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        assert sb.id == "my-handle-claim"

    def test_handle_claim_name_property(self) -> None:
        handle = make_mock_client(claim_name="prop-claim")._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        assert sb.claim_name == "prop-claim"

    def test_handle_execute_works_without_start(self) -> None:
        mock = make_mock_client(run_result=FakeExecutionResult(stdout="from handle\n", exit_code=0))
        handle = mock._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        resp = sb.execute("echo test")
        assert resp.output == "from handle\n"
        assert resp.exit_code == 0
        handle.commands.run.assert_called_once()

    def test_handle_stop_callsclose_connection(self) -> None:
        """Handle mode stop() closes local connection, does NOT delete."""
        mock = make_mock_client(claim_name="handle-stop")
        handle = mock._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        sb.stop()
        handle.close_connection.assert_called_once()
        assert not sb._started

    def test_handle_stop_does_not_call_delete(self) -> None:
        mock = make_mock_client(claim_name="no-delete")
        handle = mock._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        sb.stop()
        # No client exists in handle mode, so delete_sandbox cannot be called.
        assert sb._client is None

    def test_handle_context_manager(self) -> None:
        mock = make_mock_client(claim_name="ctx-handle")
        handle = mock._mock_sandbox_handle
        with KubernetesSandbox(sandbox=handle, namespace="n") as sb:
            assert sb._started
            resp = sb.execute("echo ctx")
            assert resp.exit_code == 0
        assert not sb._started
        handle.close_connection.assert_called_once()

    def test_handle_no_reconnect_on_error(self) -> None:
        """Handle mode does not attempt auto-reconnect."""
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        handle.commands.run.side_effect = ConnectionError("lost")
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        with pytest.raises(ConnectionError, match="lost"):
            sb.execute("cmd")

    def test_handle_owns_lifecycle_is_false(self) -> None:
        handle = make_mock_client()._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        assert sb._owns_lifecycle is False

    def test_handle_template_name_not_required(self) -> None:
        handle = make_mock_client()._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        assert sb._template_name is None

    def test_rejects_sandbox_with_claim_name(self) -> None:
        handle = make_mock_client()._mock_sandbox_handle
        with pytest.raises(ValueError, match="Cannot specify both"):
            KubernetesSandbox(sandbox=handle, claim_name="conflict")

    def test_rejects_no_sandbox_no_template(self) -> None:
        with pytest.raises(ValueError, match="Either 'sandbox' or 'template_name'"):
            KubernetesSandbox(namespace="n")

    def test_handle_with_enterprise_features(self) -> None:
        """Enterprise features (allow_prefixes) work with handle mode."""
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        sb = KubernetesSandbox(
            sandbox=handle,
            namespace="n",
            allow_prefixes=["/workspace/"],
        )
        result = sb.write("/etc/passwd", "bad")
        assert result.error is not None
        assert "not under any allowed prefix" in result.error
        handle.commands.run.assert_not_called()

    def test_handle_upload_files(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        results = sb.upload_files([("/tmp/test.txt", b"data")])
        assert results[0].error is None
        handle.commands.run.assert_called()

    def test_handle_download_files(self) -> None:
        import base64 as b64

        encoded = b64.b64encode(b"content").decode("ascii")
        mock = make_mock_client(run_result=FakeExecutionResult(stdout=encoded + "\n", exit_code=0))
        handle = mock._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n")
        results = sb.download_files(["/tmp/test.txt"])
        assert results[0].content == b"content"
        assert results[0].error is None


# ---------------------------------------------------------------------------
# execute() timeout parameter
# ---------------------------------------------------------------------------


class TestExecuteTimeout:
    def test_per_call_timeout_overrides_default(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n", command_timeout=300)
        sb.execute("echo hi", timeout=10)
        handle.commands.run.assert_called_once_with("sh -c 'echo hi'", timeout=10)

    def test_none_timeout_uses_default(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        sb = KubernetesSandbox(sandbox=handle, namespace="n", command_timeout=42)
        sb.execute("echo hi")
        handle.commands.run.assert_called_once_with("sh -c 'echo hi'", timeout=42)

    def test_timeout_with_config_based_constructor(self) -> None:
        mock = make_mock_client()
        handle = mock._mock_sandbox_handle
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="t", namespace="n", command_timeout=60)
            sb.execute("echo hi", timeout=5)
            handle.commands.run.assert_called_once_with("sh -c 'echo hi'", timeout=5)
            sb.stop()


# ---------------------------------------------------------------------------
# create_kubernetes_sandbox() factory
# ---------------------------------------------------------------------------


class TestCreateFactory:
    def test_reuses_existing_sandbox(self) -> None:
        """get_sandbox succeeds -> wraps existing handle."""
        mock = make_mock_client(claim_name="existing-claim")
        sb = create_kubernetes_sandbox(
            client=mock,
            claim_name="existing-claim",
            template_name="tpl",
            namespace="ns",
        )
        mock.get_sandbox.assert_called_once_with(claim_name="existing-claim", namespace="ns")
        mock.create_sandbox.assert_not_called()
        assert sb._started
        assert sb.id == "existing-claim"

    def test_creates_when_not_found(self) -> None:
        """Claim GET returns 404 -> creates claim directly."""
        from kubernetes.client import ApiException

        mock = make_mock_client(claim_name="new-claim")
        # Fast-check GET returns 404 (claim doesn't exist)
        mock.k8s_helper.custom_objects_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
        # get_sandbox called once after creation
        mock.get_sandbox.return_value = mock._mock_sandbox_handle
        sb = create_kubernetes_sandbox(
            client=mock,
            claim_name="new-claim",
            template_name="tpl",
            namespace="ns",
            labels={"thread_id": "t1"},
        )
        # Should create claim with user-specified name (not auto-generated)
        mock.k8s_helper.create_sandbox_claim.assert_called_once_with(
            "new-claim",
            "tpl",
            "ns",
            labels={"thread_id": "t1"},
        )
        mock.k8s_helper.resolve_sandbox_name.assert_called_once()
        mock.k8s_helper.wait_for_sandbox_ready.assert_called_once()
        # get_sandbox called only once (post-create), not for initial lookup
        assert mock.get_sandbox.call_count == 1
        # create_sandbox (auto-name) should NOT be called
        mock.create_sandbox.assert_not_called()
        assert sb._started
        assert sb._owns_lifecycle is False

    def test_cleans_up_on_creation_failure(self) -> None:
        """If sandbox creation fails after claim is created, the claim is deleted."""
        from kubernetes.client import ApiException

        mock = make_mock_client()
        # Fast-check GET returns 404 (claim doesn't exist)
        mock.k8s_helper.custom_objects_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
        mock.k8s_helper.resolve_sandbox_name.side_effect = TimeoutError("timed out")
        with pytest.raises(TimeoutError, match="timed out"):
            create_kubernetes_sandbox(
                client=mock,
                claim_name="fail-claim",
                template_name="tpl",
                namespace="ns",
            )
        # Claim should be cleaned up on failure
        mock.k8s_helper.delete_sandbox_claim.assert_called_once_with("fail-claim", "ns")

    def test_handles_concurrent_409_conflict(self) -> None:
        """409 Conflict on create_sandbox_claim falls back to get_sandbox."""
        from kubernetes.client import ApiException

        mock = make_mock_client(claim_name="race-claim")
        # Fast-check GET returns 404 (claim doesn't exist yet)
        mock.k8s_helper.custom_objects_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
        # Another caller created the claim between our GET and POST
        mock.k8s_helper.create_sandbox_claim.side_effect = ApiException(status=409)
        sb = create_kubernetes_sandbox(
            client=mock,
            claim_name="race-claim",
            template_name="tpl",
            namespace="ns",
        )
        # Should NOT resolve/wait (we didn't create the claim)
        mock.k8s_helper.resolve_sandbox_name.assert_not_called()
        mock.k8s_helper.wait_for_sandbox_ready.assert_not_called()
        # Should NOT clean up the other caller's claim
        mock.k8s_helper.delete_sandbox_claim.assert_not_called()
        # Should fall back to get_sandbox to attach to the existing claim
        mock.get_sandbox.assert_called_once_with(claim_name="race-claim", namespace="ns")
        assert sb._started

    def test_forwards_kwargs(self) -> None:
        """Extra kwargs are forwarded to KubernetesSandbox."""
        mock = make_mock_client()
        sb = create_kubernetes_sandbox(
            client=mock,
            claim_name="claim",
            template_name="tpl",
            namespace="ns",
            allow_prefixes=["/workspace/"],
            virtual_mode=True,
        )
        assert sb._allow_prefixes == ("/workspace/",)
        assert sb._virtual_mode is True


# ---------------------------------------------------------------------------
# connection_config parameter
# ---------------------------------------------------------------------------


class TestConnectionConfig:
    def test_connection_config_used_directly(self) -> None:
        """When connection_config is passed, _create_client uses it as-is."""
        from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

        cfg = SandboxInClusterConnectionConfig()
        with patch("k8s_agent_sandbox.SandboxClient") as MockClient:
            MockClient.return_value = make_mock_client()
            sb = KubernetesSandbox(template_name="tpl", namespace="ns", connection_config=cfg)
            sb.start()
            MockClient.assert_called_once()
            assert MockClient.call_args[1]["connection_config"] is cfg
            sb.stop()

    def test_in_cluster_use_pod_ip(self) -> None:
        """SandboxInClusterConnectionConfig with use_pod_ip=True is forwarded."""
        from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

        cfg = SandboxInClusterConnectionConfig(use_pod_ip=True)
        with patch("k8s_agent_sandbox.SandboxClient") as MockClient:
            MockClient.return_value = make_mock_client()
            sb = KubernetesSandbox(template_name="tpl", namespace="ns", connection_config=cfg)
            sb.start()
            config = MockClient.call_args[1]["connection_config"]
            assert config.use_pod_ip is True
            sb.stop()

    def test_connection_config_conflicts_with_api_url(self) -> None:
        from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

        cfg = SandboxInClusterConnectionConfig()
        with pytest.raises(ValueError, match="connection_config"):
            KubernetesSandbox(
                template_name="tpl",
                namespace="ns",
                connection_config=cfg,
                api_url="http://localhost:8080",
            )

    def test_connection_config_conflicts_with_gateway(self) -> None:
        from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

        cfg = SandboxInClusterConnectionConfig()
        with pytest.raises(ValueError, match="connection_config"):
            KubernetesSandbox(
                template_name="tpl",
                namespace="ns",
                connection_config=cfg,
                gateway_name="my-gw",
            )

    def test_no_connection_config_falls_back_to_tunnel(self) -> None:
        """Without connection_config or api_url/gateway, uses local tunnel."""
        with patch("k8s_agent_sandbox.SandboxClient") as MockClient:
            MockClient.return_value = make_mock_client()
            sb = KubernetesSandbox(template_name="tpl", namespace="ns")
            sb.start()
            config = MockClient.call_args[1]["connection_config"]
            assert type(config).__name__ == "SandboxLocalTunnelConnectionConfig"
            sb.stop()


# ---------------------------------------------------------------------------
# shutdown_after_seconds parameter
# ---------------------------------------------------------------------------


class TestShutdownAfterSeconds:
    def test_forwarded_to_create_sandbox(self) -> None:
        """shutdown_after_seconds is passed through to client.create_sandbox()."""
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="tpl", namespace="ns", shutdown_after_seconds=600)
            sb.start()
            mock.create_sandbox.assert_called_once()
            call_kwargs = mock.create_sandbox.call_args[1]
            assert call_kwargs["shutdown_after_seconds"] == 600
            sb.stop()

    def test_not_forwarded_when_none(self) -> None:
        """When shutdown_after_seconds is None, it is not included in kwargs."""
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="tpl", namespace="ns")
            sb.start()
            call_kwargs = mock.create_sandbox.call_args[1]
            assert "shutdown_after_seconds" not in call_kwargs
            sb.stop()

    def test_stored_on_init(self) -> None:
        sb, _, _ = _make_sandbox(shutdown_after_seconds=300)
        assert sb._shutdown_after_seconds == 300

    def test_default_is_none(self) -> None:
        sb, _, _ = _make_sandbox()
        assert sb._shutdown_after_seconds is None


class TestWarmPool:
    def test_forwarded_to_create_sandbox(self) -> None:
        """warmpool is passed through to client.create_sandbox()."""
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="tpl", namespace="ns", warmpool="my-pool")
            sb.start()
            mock.create_sandbox.assert_called_once()
            call_kwargs = mock.create_sandbox.call_args[1]
            assert call_kwargs["warmpool"] == "my-pool"
            sb.stop()

    def test_not_forwarded_when_none(self) -> None:
        """When warmpool is None, it is not included in kwargs."""
        mock = make_mock_client()
        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            sb = KubernetesSandbox(template_name="tpl", namespace="ns")
            sb.start()
            call_kwargs = mock.create_sandbox.call_args[1]
            assert "warmpool" not in call_kwargs
            sb.stop()

    def test_stored_on_init(self) -> None:
        sb, _, _ = _make_sandbox(warmpool="fast-pool")
        assert sb._warmpool == "fast-pool"

    def test_default_is_none(self) -> None:
        sb, _, _ = _make_sandbox()
        assert sb._warmpool is None
