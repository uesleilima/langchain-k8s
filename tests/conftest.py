"""Shared test fixtures for langchain-k8s.

Mock compatibility verified against k8s-agent-sandbox >=0.5.4, whose claim
path is ``k8s_helper.get_sandbox_claim`` → ``create_sandbox_claim(name,
warmpool, namespace, ...)`` → ``wait_for_claim_ready``, and whose
``SandboxClient.create_sandbox`` takes ``warmpool`` as its first parameter.

``SandboxClient`` and ``K8sHelper`` are **autospec'd**, so a signature drift
in the SDK fails loudly here instead of silently downstream.  The 0.4.6 →
0.5.4 bump is the cautionary tale: ``create_sandbox_claim`` kept its
positional arity while parameter 2 changed meaning from template name to
warm pool name, so a spec-less mock accepted the stale call unchanged while
the real cluster wrote a dangling ``warmPoolRef``.

Two deliberate limits on that autospec:

* ``k8s_helper`` is attached by hand.  It is an instance attribute assigned
  in ``SandboxClient.__init__``, so ``create_autospec`` on the class does
  not synthesise it.
* The ``Sandbox`` handle stays a plain ``MagicMock``.  Its ``commands`` and
  ``files`` are instance attributes too, so autospec would give them no
  child specs while breaking the many tests that reach into
  ``.commands.run`` / ``.files.read``.

``SandboxClient`` and ``K8sHelper`` are imported at **module scope on
purpose**, and that is the one place in this repo where the lazy-import idiom
used throughout ``src/`` must not be copied.  Nearly every test calls
``make_mock_client()`` inside a ``patch("k8s_agent_sandbox.SandboxClient")``
block; a deferred import would therefore resolve the name to the test's own
``MagicMock`` and ``create_autospec`` would fail outright with
``InvalidSpecError: Cannot autospec a Mock object``.  Binding the names here,
at import time, snapshots the real classes before any test can patch the
module attribute.  Invariant 4 in ``AGENTS.md`` is about keeping the SDK out
of ``src/`` import paths — it does not apply to test scaffolding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, create_autospec, patch

import pytest
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.k8s_helper import K8sHelper
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

from langchain_k8s import KubernetesSandbox


class FakeToolModel(FakeMessagesListChatModel):
    """``FakeMessagesListChatModel`` with a no-op ``bind_tools``."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeToolModel:  # noqa: ARG002
        return self


@dataclass
class FakeExecutionResult:
    """Mimics ``k8s_agent_sandbox.sandbox_client.ExecutionResult``."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


def make_mock_client(
    *,
    claim_name: str = "test-claim-abc",
    run_result: FakeExecutionResult | None = None,
) -> MagicMock:
    """Create a mock ``SandboxClient`` (factory) with a mock ``Sandbox`` handle."""
    # Build the Sandbox handle mock
    sandbox_handle = MagicMock()
    sandbox_handle.claim_name = claim_name
    sandbox_handle.sandbox_id = "test-sandbox-abc"
    sandbox_handle.namespace = "test-ns"
    sandbox_handle.is_active = True
    sandbox_handle.commands.run = MagicMock(return_value=run_result or FakeExecutionResult())
    sandbox_handle.files.read = MagicMock(return_value=b"file content")
    sandbox_handle.files.write = MagicMock(return_value=None)
    sandbox_handle.files.list = MagicMock(return_value=[])
    sandbox_handle.files.exists = MagicMock(return_value=True)
    sandbox_handle.terminate = MagicMock()
    sandbox_handle.close_connection = MagicMock()

    # Build the K8sHelper mock (used by create_kubernetes_sandbox factory)
    k8s_helper = create_autospec(K8sHelper, instance=True)
    # Default: the claim already exists.  Tests that want the create path
    # set this to None.
    k8s_helper.get_sandbox_claim.return_value = {
        "metadata": {"name": claim_name, "resourceVersion": "1"},
    }
    # A real dict, not a bare mock, so the resourceVersion the factory
    # threads into wait_for_claim_ready is assertable.
    k8s_helper.create_sandbox_claim.return_value = {
        "metadata": {"name": claim_name, "resourceVersion": "42"},
    }
    k8s_helper.wait_for_claim_ready.return_value = f"sandbox-{claim_name}"
    # Still real methods on K8sHelper, and kept mocked so that
    # assert_not_called() is positive evidence the factory uses the single
    # claim watch rather than the old resolve-then-wait pair.
    k8s_helper.resolve_sandbox_name.return_value = f"sandbox-{claim_name}"

    # Build the SandboxClient factory mock
    client = create_autospec(SandboxClient, instance=True)
    client.create_sandbox.return_value = sandbox_handle
    client.get_sandbox.return_value = sandbox_handle
    client.k8s_helper = k8s_helper

    # Attach the sandbox handle for easy test access
    client._mock_sandbox_handle = sandbox_handle

    return client


@pytest.fixture()
def mock_sandbox_client() -> MagicMock:
    """Provide a mock ``SandboxClient`` that is automatically patched."""
    return make_mock_client()


@pytest.fixture()
def sandbox(mock_sandbox_client: MagicMock) -> KubernetesSandbox:
    """Provide a ``KubernetesSandbox`` wired to a mock SDK client."""
    with patch(
        "k8s_agent_sandbox.SandboxClient",
        return_value=mock_sandbox_client,
    ):
        sb = KubernetesSandbox(
            warmpool_name="test-pool",
            namespace="test-ns",
        )
        yield sb
        # Ensure cleanup even if test forgets
        if sb._started:
            sb.stop()


@pytest.fixture()
def started_sandbox(sandbox: KubernetesSandbox) -> KubernetesSandbox:
    """Provide a ``KubernetesSandbox`` that has already been started."""
    sandbox.start()
    return sandbox
