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

import threading
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
        resp = sandbox.execute('python3 -c "print(2 + 2)"')
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


# ---------------------------------------------------------------------------
# Concurrency and lazy initialisation
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_sequential_execute_reuses_pod(self) -> None:
        """Multiple sequential execute() calls reuse the same sandbox pod."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as sb:
            # Write a marker on the first call
            sb.execute("echo 'seq-marker' > /tmp/seq-test.txt")
            # Subsequent calls should see it (same pod)
            r1 = sb.execute("cat /tmp/seq-test.txt")
            r2 = sb.execute("cat /tmp/seq-test.txt")
            r3 = sb.execute("cat /tmp/seq-test.txt")
            assert "seq-marker" in r1.output
            assert "seq-marker" in r2.output
            assert "seq-marker" in r3.output

    def test_lazy_init_without_explicit_start(self) -> None:
        """execute() works without calling start() first."""
        sb = KubernetesSandbox(template_name=TEMPLATE, namespace=NAMESPACE)
        assert not sb._started
        resp = sb.execute("echo 'lazy'")
        assert sb._started
        assert resp.exit_code == 0
        assert "lazy" in resp.output
        sb.stop()

    def test_concurrent_execute_same_sandbox(self) -> None:
        """Multiple threads calling execute() concurrently share one pod."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as sb:
            results: dict[int, str] = {}
            errors: list[Exception] = []

            def worker(idx: int) -> None:
                try:
                    resp = sb.execute(f"echo 'thread-{idx}'")
                    assert resp.exit_code == 0
                    results[idx] = resp.output
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Thread errors: {errors}"
            assert len(results) == 5
            for i in range(5):
                assert f"thread-{i}" in results[i]

    def test_concurrent_mixed_operations(self) -> None:
        """execute(), upload_files(), download_files() from parallel threads."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as sb:
            # Seed a file so downloads have something to read
            sb.execute("echo 'seed-data' > /tmp/concurrent-read.txt")

            errors: list[Exception] = []

            def exec_worker() -> None:
                try:
                    resp = sb.execute("echo 'concurrent-exec'")
                    assert resp.exit_code == 0
                except Exception as e:
                    errors.append(e)

            def upload_worker(idx: int) -> None:
                try:
                    results = sb.upload_files([(f"/tmp/concurrent-upload-{idx}.txt", b"upload-data")])
                    assert results[0].error is None
                except Exception as e:
                    errors.append(e)

            def download_worker() -> None:
                try:
                    results = sb.download_files(["/tmp/concurrent-read.txt"])
                    assert results[0].error is None
                    assert results[0].content is not None
                    assert b"seed-data" in results[0].content
                except Exception as e:
                    errors.append(e)

            threads = [
                threading.Thread(target=exec_worker),
                threading.Thread(target=exec_worker),
                threading.Thread(target=upload_worker, args=(0,)),
                threading.Thread(target=upload_worker, args=(1,)),
                threading.Thread(target=download_worker),
                threading.Thread(target=download_worker),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Thread errors: {errors}"

            # Verify uploads persisted
            resp = sb.execute("cat /tmp/concurrent-upload-0.txt /tmp/concurrent-upload-1.txt")
            assert resp.exit_code == 0
            assert resp.output.count("upload-data") == 2

    def test_concurrent_stop_is_safe(self) -> None:
        """Multiple threads calling stop() concurrently don't crash."""
        sb = KubernetesSandbox(template_name=TEMPLATE, namespace=NAMESPACE)
        sb.start()
        assert sb._started

        errors: list[Exception] = []

        def stop_worker() -> None:
            try:
                sb.stop()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=stop_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Stop errors: {errors}"
        assert not sb._started

    def test_multiple_sandbox_instances_in_parallel(self) -> None:
        """Multiple KubernetesSandbox instances run on separate pods concurrently."""
        num_instances = 3
        errors: list[Exception] = []
        sandbox_ids: dict[int, str] = {}

        def instance_worker(idx: int) -> None:
            try:
                with KubernetesSandbox(
                    template_name=TEMPLATE,
                    namespace=NAMESPACE,
                ) as sb:
                    # Each sandbox gets its own pod — write a unique marker
                    marker = f"instance-{idx}-marker"
                    sb.execute(f"echo '{marker}' > /tmp/instance-marker.txt")

                    # Read it back to confirm isolation
                    resp = sb.execute("cat /tmp/instance-marker.txt")
                    assert resp.exit_code == 0
                    assert marker in resp.output

                    # Record the sandbox id (claim name) to verify uniqueness
                    sandbox_ids[idx] = sb.id
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=instance_worker, args=(i,)) for i in range(num_instances)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Instance errors: {errors}"
        assert len(sandbox_ids) == num_instances
        # Each instance should have a distinct sandbox id (different pods)
        unique_ids = set(sandbox_ids.values())
        assert len(unique_ids) == num_instances, (
            f"Expected {num_instances} unique sandbox ids, got {len(unique_ids)}: {sandbox_ids}"
        )
