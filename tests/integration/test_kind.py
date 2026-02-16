"""Integration tests for KubernetesSandbox against a real Kind cluster.

Prerequisites
-------------
Run the setup script first::

    ./scripts/kind-setup.sh

This creates a Kind cluster named ``langchain-k8s`` with:

1. agent-sandbox controller + extension CRDs
2. sandbox-router deployment
3. ``python-runtime-sandbox`` image loaded into Kind
4. ``python-sandbox-template`` SandboxTemplate

Run tests::

    uv run pytest tests/integration/ -v -m integration

Tests are automatically skipped when no Kind cluster is detected.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from langchain_k8s import KubernetesSandbox

pytestmark = pytest.mark.integration

TEMPLATE = "python-sandbox-template"
NAMESPACE = "agent-sandbox-system"


@pytest.fixture()
def sandbox() -> Generator[KubernetesSandbox]:
    """Provide a sandbox connected to Kind via auto-tunnel."""
    sb = KubernetesSandbox(
        template_name=TEMPLATE,
        namespace=NAMESPACE,
    )
    yield sb
    if sb._started:
        sb.stop()


# ---------------------------------------------------------------------------
# Basic execution
# ---------------------------------------------------------------------------


class TestBasicExecution:
    def test_echo(self, sandbox: KubernetesSandbox) -> None:
        resp = sandbox.execute("echo 'Hello from K8s sandbox'")
        assert resp.exit_code == 0
        assert "Hello from K8s sandbox" in resp.output

    def test_python_version(self, sandbox: KubernetesSandbox) -> None:
        resp = sandbox.execute("python3 --version")
        assert resp.exit_code == 0
        assert "Python" in resp.output

    def test_python_script(self, sandbox: KubernetesSandbox) -> None:
        resp = sandbox.execute("python3 -c \"print(2 + 2)\"")
        assert resp.exit_code == 0
        assert "4" in resp.output

    def test_failed_command(self, sandbox: KubernetesSandbox) -> None:
        resp = sandbox.execute("sh -c 'exit 42'")
        assert resp.exit_code == 42

    def test_command_chaining(self, sandbox: KubernetesSandbox) -> None:
        resp = sandbox.execute("echo 'a' && echo 'b' && echo 'c'")
        assert resp.exit_code == 0
        assert "a" in resp.output
        assert "c" in resp.output

    def test_pipe(self, sandbox: KubernetesSandbox) -> None:
        resp = sandbox.execute("echo 'hello world' | wc -w")
        assert resp.exit_code == 0
        assert "2" in resp.output

    def test_nonexistent_command(self, sandbox: KubernetesSandbox) -> None:
        resp = sandbox.execute("this_command_does_not_exist_xyz 2>&1 || true")
        # The command itself fails, but the shell wrapper succeeds
        assert resp.exit_code == 0


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------


class TestFileOperations:
    def test_upload_and_verify(self, sandbox: KubernetesSandbox) -> None:
        results = sandbox.upload_files([("/tmp/test-upload.txt", b"upload content\n")])
        assert results[0].error is None

        resp = sandbox.execute("cat /tmp/test-upload.txt")
        assert resp.exit_code == 0
        assert "upload content" in resp.output

    def test_download(self, sandbox: KubernetesSandbox) -> None:
        sandbox.execute("echo 'download me' > /tmp/test-download.txt")
        results = sandbox.download_files(["/tmp/test-download.txt"])
        assert results[0].error is None
        assert results[0].content is not None
        assert b"download me" in results[0].content

    def test_download_nonexistent(self, sandbox: KubernetesSandbox) -> None:
        results = sandbox.download_files(["/tmp/no-such-file-xyz.txt"])
        assert results[0].error is not None

    def test_upload_download_roundtrip(self, sandbox: KubernetesSandbox) -> None:
        original = b"binary \x00\x01\x02 data"
        sandbox.upload_files([("/tmp/roundtrip.bin", original)])
        results = sandbox.download_files(["/tmp/roundtrip.bin"])
        assert results[0].content == original

    def test_upload_multiple(self, sandbox: KubernetesSandbox) -> None:
        files = [
            ("/tmp/multi-a.txt", b"aaa"),
            ("/tmp/multi-b.txt", b"bbb"),
        ]
        results = sandbox.upload_files(files)
        assert all(r.error is None for r in results)

        resp = sandbox.execute("cat /tmp/multi-a.txt /tmp/multi-b.txt")
        assert "aaa" in resp.output
        assert "bbb" in resp.output


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_context_manager_cleanup(self) -> None:
        with KubernetesSandbox(template_name=TEMPLATE, namespace=NAMESPACE) as sb:
            resp = sb.execute("echo 'inside context'")
            assert resp.exit_code == 0
        assert not sb._started

    def test_reuse_sandbox_true_same_pod(self) -> None:
        """Persistent mode: files survive across execute() calls (same pod)."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            reuse_sandbox=True,
        ) as sb:
            sb.execute("echo 'marker' > /tmp/reuse-test.txt")
            resp = sb.execute("cat /tmp/reuse-test.txt")
            assert "marker" in resp.output

    def test_explicit_start_stop(self) -> None:
        sb = KubernetesSandbox(template_name=TEMPLATE, namespace=NAMESPACE)
        sb.start()
        resp = sb.execute("echo 'started'")
        assert resp.exit_code == 0
        sb.stop()
        assert not sb._started
