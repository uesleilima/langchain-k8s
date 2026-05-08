"""Shared test fixtures for langchain-k8s.

Mock compatibility verified for k8s-agent-sandbox >=0.4.5.
The mock SandboxClient supports create_sandbox(warmpool=...) via **kwargs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
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
    k8s_helper = MagicMock()
    k8s_helper.create_sandbox_claim = MagicMock()
    k8s_helper.resolve_sandbox_name = MagicMock(return_value=f"sandbox-{claim_name}")
    k8s_helper.wait_for_sandbox_ready = MagicMock()
    k8s_helper.delete_sandbox_claim = MagicMock()

    # Build the SandboxClient factory mock
    client = MagicMock()
    client.create_sandbox = MagicMock(return_value=sandbox_handle)
    client.get_sandbox = MagicMock(return_value=sandbox_handle)
    client.delete_sandbox = MagicMock()
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
            template_name="test-template",
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
