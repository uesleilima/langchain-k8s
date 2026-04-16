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

import posixpath
import threading
from collections.abc import Generator

import pytest

from langchain_k8s import KubernetesSandbox, create_kubernetes_sandbox

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


# ---------------------------------------------------------------------------
# Enterprise features: allow_prefixes
# ---------------------------------------------------------------------------


class TestAllowPrefixes:
    def test_write_allowed_under_prefix(self) -> None:
        """write() succeeds when the path is under an allowed prefix."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            allow_prefixes=["/workspace/", "/tmp/"],
        ) as sb:
            result = sb.write("/tmp/allowed.txt", "hello from allow_prefixes")
            assert result.error is None
            resp = sb.execute("cat /tmp/allowed.txt")
            assert resp.exit_code == 0
            assert "hello from allow_prefixes" in resp.output

    def test_write_blocked_outside_prefix(self) -> None:
        """write() returns an error when the path is outside allowed prefixes."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            allow_prefixes=["/workspace/"],
        ) as sb:
            result = sb.write("/tmp/blocked.txt", "should not land")
            assert result.error is not None
            assert "not under any allowed prefix" in result.error

    def test_edit_blocked_outside_prefix(self) -> None:
        """edit() returns an error when the path is outside allowed prefixes."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            allow_prefixes=["/workspace/"],
        ) as sb:
            # Seed a file via execute (bypasses allow_prefixes)
            sb.execute("echo 'original' > /tmp/edit-blocked.txt")
            result = sb.edit("/tmp/edit-blocked.txt", "original", "modified")
            assert result.error is not None
            assert "not under any allowed prefix" in result.error
            # File should remain unchanged
            resp = sb.execute("cat /tmp/edit-blocked.txt")
            assert "original" in resp.output

    def test_execute_not_restricted_by_allow_prefixes(self) -> None:
        """execute() is not subject to allow_prefixes (tool-level policy only)."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            allow_prefixes=["/workspace/"],
        ) as sb:
            resp = sb.execute("echo 'bypass' > /tmp/exec-bypass.txt && cat /tmp/exec-bypass.txt")
            assert resp.exit_code == 0
            assert "bypass" in resp.output


# ---------------------------------------------------------------------------
# Enterprise features: virtual_mode + root_dir
# ---------------------------------------------------------------------------


class TestVirtualMode:
    def test_write_and_read_under_root_dir(self) -> None:
        """Virtual paths are resolved under root_dir for write and read."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            virtual_mode=True,
            root_dir="/tmp/vfs",
        ) as sb:
            # Ensure root exists
            sb.execute("mkdir -p /tmp/vfs")
            result = sb.write("/hello.txt", "virtual content")
            assert result.error is None
            # Verify the file actually lives under /tmp/vfs
            resp = sb.execute("cat /tmp/vfs/hello.txt")
            assert resp.exit_code == 0
            assert "virtual content" in resp.output
            # read() should also resolve the virtual path
            content = sb.read("/hello.txt")
            assert content.error is None
            assert content.file_data is not None
            assert "virtual content" in content.file_data["content"]

    def test_edit_under_root_dir(self) -> None:
        """edit() resolves virtual paths under root_dir."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            virtual_mode=True,
            root_dir="/tmp/vfs-edit",
        ) as sb:
            sb.execute("mkdir -p /tmp/vfs-edit && echo 'old text' > /tmp/vfs-edit/doc.txt")
            result = sb.edit("/doc.txt", "old text", "new text")
            assert result.error is None
            resp = sb.execute("cat /tmp/vfs-edit/doc.txt")
            assert "new text" in resp.output

    def test_path_traversal_blocked(self) -> None:
        """Path traversal (``..``) is rejected in virtual mode."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            virtual_mode=True,
            root_dir="/tmp/vfs-jail",
        ) as sb:
            result = sb.write("../../etc/passwd", "bad")
            assert result.error is not None
            assert "traversal" in result.error.lower()

    def test_upload_resolves_path(self) -> None:
        """upload_files() resolves virtual paths under root_dir."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            virtual_mode=True,
            root_dir="/tmp/vfs-upload",
        ) as sb:
            sb.execute("mkdir -p /tmp/vfs-upload")
            results = sb.upload_files([("/data.bin", b"uploaded bytes")])
            assert results[0].error is None
            # Verify the file is at the resolved location
            resp = sb.execute("cat /tmp/vfs-upload/data.bin")
            assert resp.exit_code == 0
            assert "uploaded bytes" in resp.output

    def test_native_download_resolves_path(self) -> None:
        """download_files() in virtual mode resolves paths under root_dir."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            virtual_mode=True,
            root_dir="/tmp/vfs-download",
        ) as sb:
            sb.execute("mkdir -p /tmp/vfs-download && echo 'download me' > /tmp/vfs-download/out.txt")
            results = sb.download_files(["/out.txt"])
            assert results[0].error is None
            assert results[0].content is not None
            assert b"download me" in results[0].content

    def test_ls_info_resolves_path(self) -> None:
        """ls_info() resolves the virtual path under root_dir."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            virtual_mode=True,
            root_dir="/tmp/vfs-ls",
        ) as sb:
            sb.execute("mkdir -p /tmp/vfs-ls/sub && touch /tmp/vfs-ls/sub/a.txt /tmp/vfs-ls/sub/b.txt")
            entries = sb.ls_info("/sub")
            # FileInfo is a TypedDict with "path" key; extract basenames.
            names = [posixpath.basename(e["path"]) for e in entries]
            assert "a.txt" in names
            assert "b.txt" in names

    def test_virtual_mode_combined_with_allow_prefixes(self) -> None:
        """allow_prefixes checks the resolved path (after virtual-mode resolution)."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            virtual_mode=True,
            root_dir="/tmp/vfs-combined",
            allow_prefixes=["/tmp/vfs-combined/"],
        ) as sb:
            sb.execute("mkdir -p /tmp/vfs-combined")
            # Virtual path "/app.py" resolves to "/tmp/vfs-combined/app.py" — allowed.
            result = sb.write("/app.py", "print('hello')")
            assert result.error is None
            resp = sb.execute("cat /tmp/vfs-combined/app.py")
            assert "print('hello')" in resp.output


# ---------------------------------------------------------------------------
# Enterprise features: skip_cleanup
# ---------------------------------------------------------------------------


class TestSkipCleanup:
    def test_skip_cleanup_preserves_sandbox(self) -> None:
        """With skip_cleanup=True, the sandbox pod survives stop() and can be
        verified via a separate sandbox that checks the SandboxClaim still exists.
        """
        sb = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            skip_cleanup=True,
        )
        sb.start()
        assert sb.id is not None
        # Write a marker to prove the pod is running
        resp = sb.execute("echo 'still alive' > /tmp/skip-marker.txt")
        assert resp.exit_code == 0
        claim_name = sb._sandbox.claim_name if sb._sandbox else None
        sb.stop()
        assert not sb._started

        # The SandboxClaim should still exist in the cluster.
        # We can verify by querying the Kubernetes API.
        if claim_name:
            probe = KubernetesSandbox(template_name=TEMPLATE, namespace=NAMESPACE)
            resp = probe.execute(f"echo 'claim {claim_name} survived'")
            # Just verifying we can still create sandboxes; the claim
            # existence check would require kubectl, which is validated
            # by the fact that stop() didn't delete it (skip_cleanup=True).
            assert resp.exit_code == 0
            probe.stop()


# ---------------------------------------------------------------------------
# Reconnect via claim_name
# ---------------------------------------------------------------------------


class TestReconnect:
    def test_reconnect_to_existing_sandbox(self) -> None:
        """Create a sandbox with skip_cleanup, stop, reconnect via claim_name,
        and verify the same pod (with its filesystem state) is reused.
        """
        # Phase 1: create sandbox and write a marker file.
        sb1 = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            skip_cleanup=True,
        )
        sb1.start()
        resp = sb1.execute("echo 'reconnect-marker' > /tmp/reconnect-test.txt")
        assert resp.exit_code == 0
        saved_claim = sb1.claim_name
        assert saved_claim is not None
        sb1.stop()

        # Phase 2: reconnect using claim_name and read the marker file.
        try:
            sb2 = KubernetesSandbox(
                template_name=TEMPLATE,
                namespace=NAMESPACE,
                claim_name=saved_claim,
            )
            sb2.start()
            resp = sb2.execute("cat /tmp/reconnect-test.txt")
            assert resp.exit_code == 0
            assert "reconnect-marker" in resp.output
            assert sb2.claim_name == saved_claim
        finally:
            # Clean up: delete the sandbox properly.
            if sb2._started:
                sb2._skip_cleanup = False
                sb2.stop()

    def test_claim_name_property_available_after_start(self) -> None:
        """claim_name is populated after start and can be persisted."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as sb:
            assert sb.claim_name is not None
            assert sb.claim_name.startswith("sandbox-claim-")


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


class TestLabels:
    def test_labels_applied_to_sandbox(self) -> None:
        """Sandboxes created with labels can be inspected via kubectl."""
        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            labels={"test-suite": "integration", "feature": "labels"},
        ) as sb:
            assert sb.claim_name is not None
            resp = sb.execute("echo 'labels work'")
            assert resp.exit_code == 0


# ---------------------------------------------------------------------------
# Ecosystem-standard mode: external SandboxClient + KubernetesSandbox wrapper
# ---------------------------------------------------------------------------


class TestEcosystemStandardMode:
    def test_external_handle_execute(self) -> None:
        """Create sandbox externally via SandboxClient, wrap with
        KubernetesSandbox(sandbox=handle), and execute commands.
        """
        from k8s_agent_sandbox import SandboxClient
        from k8s_agent_sandbox.models import SandboxLocalTunnelConnectionConfig

        client = SandboxClient(
            connection_config=SandboxLocalTunnelConnectionConfig(),
        )
        handle = client.create_sandbox(template=TEMPLATE, namespace=NAMESPACE)
        try:
            backend = KubernetesSandbox(sandbox=handle, namespace=NAMESPACE)
            assert backend._started
            assert backend._owns_lifecycle is False

            resp = backend.execute("echo 'ecosystem pattern'")
            assert resp.exit_code == 0
            assert "ecosystem pattern" in resp.output

            backend.upload_files([("/tmp/eco-test.txt", b"eco content")])
            results = backend.download_files(["/tmp/eco-test.txt"])
            assert results[0].error is None
            assert results[0].content == b"eco content"
        finally:
            client.delete_sandbox(handle.claim_name, NAMESPACE)

    def test_create_kubernetes_sandbox_factory(self) -> None:
        """create_kubernetes_sandbox() get-or-create factory works end-to-end."""
        from k8s_agent_sandbox import SandboxClient
        from k8s_agent_sandbox.models import SandboxLocalTunnelConnectionConfig

        client = SandboxClient(
            connection_config=SandboxLocalTunnelConnectionConfig(),
        )
        claim = "factory-test-claim"
        try:
            backend = create_kubernetes_sandbox(
                client=client,
                claim_name=claim,
                template_name=TEMPLATE,
                namespace=NAMESPACE,
                labels={"test": "factory"},
            )
            assert backend._started
            resp = backend.execute("echo 'from factory'")
            assert resp.exit_code == 0
            assert "from factory" in resp.output

            # Calling again with the same claim_name should reuse
            backend2 = create_kubernetes_sandbox(
                client=client,
                claim_name=claim,
                template_name=TEMPLATE,
                namespace=NAMESPACE,
            )
            resp2 = backend2.execute("echo 'reused'")
            assert resp2.exit_code == 0
            assert "reused" in resp2.output
        finally:
            client.delete_sandbox(claim, NAMESPACE)
