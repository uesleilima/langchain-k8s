"""Shared test fixtures for langchain-k8s."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from langchain_k8s import KubernetesSandbox

from typing import Any
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel


class FakeToolModel(FakeMessagesListChatModel):
    """``FakeMessagesListChatModel`` with a no-op ``bind_tools``."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeToolModel":  # noqa: ARG002
        return self


@dataclass
class FakeExecutionResult:
    """Mimics ``agentic_sandbox.sandbox_client.ExecutionResult``."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


def make_mock_client(
    *,
    claim_name: str = "test-claim-abc",
    run_result: FakeExecutionResult | None = None,
) -> MagicMock:
    """Create a mock ``SandboxClient`` with sensible defaults."""
    client = MagicMock()
    client.claim_name = claim_name
    client.sandbox_name = "test-sandbox-abc"
    client.pod_name = "test-pod-abc"
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.run = MagicMock(return_value=run_result or FakeExecutionResult())
    client.read = MagicMock(return_value=b"file content")
    client.write = MagicMock(return_value=None)
    return client


@pytest.fixture()
def mock_sandbox_client() -> MagicMock:
    """Provide a mock ``SandboxClient`` that is automatically patched."""
    return make_mock_client()


@pytest.fixture()
def sandbox(mock_sandbox_client: MagicMock) -> KubernetesSandbox:
    """Provide a ``KubernetesSandbox`` wired to a mock SDK client."""
    with (
        patch(
            "langchain_k8s.sandbox._SandboxClient",
            return_value=mock_sandbox_client,
            create=True,
        ),
        patch(
            "agentic_sandbox.SandboxClient",
            return_value=mock_sandbox_client,
        ),
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
