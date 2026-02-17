"""Integration tests for KubernetesSandbox as a deep-agent backend against Kind.

These tests wire ``KubernetesSandbox`` into a real LangChain agent loop using
``FakeMessagesListChatModel`` so the LLM is deterministic while the sandbox
runs real commands on a Kind cluster.

Prerequisites
-------------
Run the setup script first::

    ./scripts/kind-setup.sh

Run tests::

    uv run pytest tests/integration/ -v -m integration

Tests are automatically skipped when no Kind cluster is detected.
"""

from __future__ import annotations

import threading

import pytest
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from langchain_k8s import KubernetesSandbox
from tests.conftest import FakeToolModel

pytestmark = pytest.mark.integration

TEMPLATE = "python-sandbox-template"
NAMESPACE = "agent-sandbox-system"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCreateDeepAgent:
    """Integration tests using ``create_deep_agent`` — the real public API.

    Unlike the ``create_agent`` tests above which wire only
    ``FilesystemMiddleware``, these tests go through the full deep-agent
    middleware stack (TodoList, Filesystem, SubAgent, Summarization,
    Caching, PatchToolCalls).
    """

    def test_deep_agent_execute(self) -> None:
        """Deep agent runs a command in the sandbox."""
        model = FakeToolModel(responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "execute",
                    "args": {"command": "echo 'deep-agent-hello'"},
                    "id": "call_deep_exec",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="Command ran."),
        ])

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke(
                {"messages": [HumanMessage(content="Run echo")]}
            )

        messages = result["messages"]
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        assert any("deep-agent-hello" in m.content for m in tool_msgs)

    def test_deep_agent_python_script(self) -> None:
        """Deep agent runs a Python script via execute."""
        model = FakeToolModel(responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "execute",
                    "args": {"command": "python3 -c \"import sys; print(sys.version_info[:2])\""},
                    "id": "call_py",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="Python version retrieved."),
        ])

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke(
                {"messages": [HumanMessage(content="Get Python version")]}
            )

        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        # Should contain a version tuple like (3, 12)
        assert any("3," in m.content or "3." in m.content for m in tool_msgs)

    def test_deep_agent_multi_step(self) -> None:
        """Deep agent makes two sequential tool calls through the full stack."""
        model = FakeToolModel(responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "execute",
                    "args": {"command": "echo 'deep-step-1' > /tmp/deep-multi.txt"},
                    "id": "call_s1",
                    "type": "tool_call",
                }],
            ),
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "execute",
                    "args": {"command": "cat /tmp/deep-multi.txt"},
                    "id": "call_s2",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="File read."),
        ])

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke(
                {"messages": [HumanMessage(content="Write then read")]}
            )

        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        # The second tool message should contain the file content
        assert any("deep-step-1" in m.content for m in tool_msgs)

    def test_deep_agent_failed_command(self) -> None:
        """Deep agent receives failure info through the full middleware stack."""
        model = FakeToolModel(responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "execute",
                    "args": {"command": "cat /nonexistent_deep_agent_file"},
                    "id": "call_deep_fail",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="File not found."),
        ])

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke(
                {"messages": [HumanMessage(content="Read missing file")]}
            )

        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert any("No such file" in m.content for m in tool_msgs)

    def test_deep_agent_lazy_init(self) -> None:
        """Deep agent lazily starts the sandbox on first tool call."""
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        )
        assert not backend._started

        model = FakeToolModel(responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "execute",
                    "args": {"command": "echo 'deep-lazy'"},
                    "id": "call_deep_lazy",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="Done."),
        ])

        agent = create_deep_agent(model=model, backend=backend)
        result = agent.invoke(
            {"messages": [HumanMessage(content="Run command")]}
        )

        assert backend._started
        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert any("deep-lazy" in m.content for m in tool_msgs)
        backend.stop()

    def test_deep_agent_no_tool_call_skips_sandbox(self) -> None:
        """Deep agent that doesn't call any tools never starts the sandbox."""
        model = FakeToolModel(responses=[
            AIMessage(content="No tools needed here."),
        ])

        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        )

        agent = create_deep_agent(model=model, backend=backend)
        result = agent.invoke(
            {"messages": [HumanMessage(content="Just answer")]}
        )

        assert not backend._started
        assert "No tools needed" in result["messages"][-1].content

    def test_deep_agent_multiple_invocations_reuse(self) -> None:
        """Multiple deep-agent invocations on the same backend reuse the pod."""
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            reuse_sandbox=True,
        )

        # Invocation 1: write
        model_1 = FakeToolModel(responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "execute",
                    "args": {"command": "echo 'deep-reuse' > /tmp/deep-reuse.txt"},
                    "id": "call_dw",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="Written."),
        ])
        agent_1 = create_deep_agent(model=model_1, backend=backend)
        agent_1.invoke(
            {"messages": [HumanMessage(content="Write file")]}
        )
        first_id = backend.id

        # Invocation 2: read it back — same pod
        model_2 = FakeToolModel(responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "execute",
                    "args": {"command": "cat /tmp/deep-reuse.txt"},
                    "id": "call_dr",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="Read."),
        ])
        agent_2 = create_deep_agent(model=model_2, backend=backend)
        result_2 = agent_2.invoke(
            {"messages": [HumanMessage(content="Read file")]}
        )

        assert backend.id == first_id
        tool_msgs = [m for m in result_2["messages"] if isinstance(m, ToolMessage)]
        assert any("deep-reuse" in m.content for m in tool_msgs)
        backend.stop()

    def test_deep_agent_stop_and_reinvoke(self) -> None:
        """Stopping and re-invoking a deep agent gets a fresh pod."""
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            reuse_sandbox=True,
        )

        # Invocation 1
        model_1 = FakeToolModel(responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "execute",
                    "args": {"command": "echo 'ephemeral' > /tmp/deep-eph.txt"},
                    "id": "call_e1",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="Done."),
        ])
        agent_1 = create_deep_agent(model=model_1, backend=backend)
        agent_1.invoke(
            {"messages": [HumanMessage(content="Write")]}
        )
        first_id = backend.id

        backend.stop()

        # Invocation 2 — new pod, old file gone
        model_2 = FakeToolModel(responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "execute",
                    "args": {"command": "cat /tmp/deep-eph.txt 2>&1 || echo 'GONE'"},
                    "id": "call_e2",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="Checked."),
        ])
        agent_2 = create_deep_agent(model=model_2, backend=backend)
        result_2 = agent_2.invoke(
            {"messages": [HumanMessage(content="Read on new pod")]}
        )

        assert backend.id != first_id
        tool_msgs = [m for m in result_2["messages"] if isinstance(m, ToolMessage)]
        assert any(
            "No such file" in m.content or "GONE" in m.content for m in tool_msgs
        )
        backend.stop()

    def test_deep_agent_parallel_independent_backends(self) -> None:
        """Parallel deep agents with independent backends get isolated pods."""
        errors: list[Exception] = []
        sandbox_ids: dict[int, str] = {}

        def deep_agent_worker(idx: int) -> None:
            try:
                model = FakeToolModel(responses=[
                    AIMessage(
                        content="",
                        tool_calls=[{
                            "name": "execute",
                            "args": {"command": f"echo 'deep-iso-{idx}'"},
                            "id": f"call_di_{idx}",
                            "type": "tool_call",
                        }],
                    ),
                    AIMessage(content=f"Agent {idx} done."),
                ])

                with KubernetesSandbox(
                    template_name=TEMPLATE,
                    namespace=NAMESPACE,
                ) as backend:
                    agent = create_deep_agent(model=model, backend=backend)
                    result = agent.invoke(
                        {"messages": [HumanMessage(content=f"Task {idx}")]}
                    )
                    sandbox_ids[idx] = backend.id

                    tool_msgs = [
                        m for m in result["messages"] if isinstance(m, ToolMessage)
                    ]
                    assert any(f"deep-iso-{idx}" in m.content for m in tool_msgs)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=deep_agent_worker, args=(i,)) for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Deep agent errors: {errors}"
        assert len(sandbox_ids) == 3
        unique_ids = set(sandbox_ids.values())
        assert len(unique_ids) == 3, (
            f"Expected 3 unique ids, got {len(unique_ids)}: {sandbox_ids}"
        )
