"""Unit tests for KubernetesSandbox used as an agent backend.

Uses ``langchain.agents.create_agent`` with a fake chat model to drive a real
agent loop without calling any LLM.  The sandbox SDK is fully mocked so these
tests run offline and fast.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from langchain_k8s import KubernetesSandbox
from tests.conftest import FakeExecutionResult, FakeToolModel, make_mock_client


def _run_agent(
    responses: list[AIMessage],
    mock_client: Any,
    user_message: str,
) -> tuple[dict[str, Any], KubernetesSandbox]:
    """Build a ``create_agent`` with a mocked sandbox, invoke it, and return results.

    The ``k8s_agent_sandbox.SandboxClient`` patch stays active for the entire
    lifetime of the agent (construction → invoke → assertions) so that lazy
    sandbox initialisation inside ``_create_client()`` picks up the mock.

    Returns ``(invoke_result, backend)``.
    """
    model = FakeToolModel(responses=responses)

    with patch("k8s_agent_sandbox.SandboxClient", return_value=mock_client):
        backend = KubernetesSandbox(
            template_name="test-tpl",
            namespace="test-ns",
        )

        agent = create_agent(
            model,
            middleware=[FilesystemMiddleware(backend=backend)],
        )

        result = agent.invoke({"messages": [HumanMessage(content=user_message)]})

    return result, backend


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeepAgentExecute:
    """Agent invokes the ``execute`` tool and the sandbox backend runs it."""

    def test_single_execute(self) -> None:
        mock = make_mock_client(
            run_result=FakeExecutionResult(stdout="hello world\n", exit_code=0),
        )
        result, backend = _run_agent(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo hello world"},
                            "id": "call_exec_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done — the sandbox said hello world."),
            ],
            mock_client=mock,
            user_message="Run echo hello world",
        )

        messages = result["messages"]
        # Expect: Human → AI (tool_call) → Tool (result) → AI (final)
        assert len(messages) == 4
        assert isinstance(messages[0], HumanMessage)
        assert isinstance(messages[1], AIMessage)
        assert messages[1].tool_calls[0]["name"] == "execute"
        assert isinstance(messages[2], ToolMessage)
        assert "hello world" in messages[2].content
        assert isinstance(messages[3], AIMessage)
        assert "hello world" in messages[3].content

        # Verify the backend actually received the command
        mock._mock_sandbox_handle.commands.run.assert_called_once_with("sh -c 'echo hello world'", timeout=300)
        backend.stop()

    def test_execute_failed_command(self) -> None:
        mock = make_mock_client(
            run_result=FakeExecutionResult(
                stderr="ls: /nonexistent: No such file or directory",
                exit_code=2,
            ),
        )
        result, backend = _run_agent(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "ls /nonexistent"},
                            "id": "call_fail",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="The directory does not exist."),
            ],
            mock_client=mock,
            user_message="List /nonexistent",
        )

        tool_msg = result["messages"][2]
        assert isinstance(tool_msg, ToolMessage)
        assert "No such file or directory" in tool_msg.content
        assert "exit code 2" in tool_msg.content
        backend.stop()

    def test_multi_step_execute(self) -> None:
        """Agent makes two tool calls in sequence (separate turns)."""
        call_count = 0

        def multi_run(cmd: str, timeout: int = 60) -> FakeExecutionResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FakeExecutionResult(stdout="file created\n", exit_code=0)
            return FakeExecutionResult(stdout="hello from file\n", exit_code=0)

        mock = make_mock_client()
        mock._mock_sandbox_handle.commands.run = multi_run

        result, backend = _run_agent(
            responses=[
                # Turn 1: create a file
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo hello > /tmp/test.txt"},
                            "id": "call_write",
                            "type": "tool_call",
                        }
                    ],
                ),
                # Turn 2: read the file
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "cat /tmp/test.txt"},
                            "id": "call_read",
                            "type": "tool_call",
                        }
                    ],
                ),
                # Turn 3: final answer
                AIMessage(content="File contains: hello from file"),
            ],
            mock_client=mock,
            user_message="Create a file and read it back",
        )

        messages = result["messages"]
        # Human → AI(tool) → Tool → AI(tool) → Tool → AI(final)
        assert len(messages) == 6
        assert call_count == 2
        assert "hello from file" in messages[-1].content
        backend.stop()


class TestDeepAgentExecuteAndFiles:
    """Agent combines execute + file operations in one session."""

    def test_execute_python_script(self) -> None:
        """Agent runs a command and gets the result."""
        mock = make_mock_client(
            run_result=FakeExecutionResult(stdout="script done\n", exit_code=0),
        )
        result, backend = _run_agent(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "python3 -c 'print(42)'"},
                            "id": "call_exec",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Script completed successfully."),
            ],
            mock_client=mock,
            user_message="Run a python script",
        )

        messages = result["messages"]
        tool_msg = messages[2]
        assert isinstance(tool_msg, ToolMessage)
        assert "script done" in tool_msg.content
        assert "exit code 0" in tool_msg.content
        backend.stop()


class TestDeepAgentLifecycle:
    """Agent creation and teardown with the backend."""

    def test_backend_not_started_until_tool_call(self) -> None:
        """Sandbox is only created when the agent actually calls a tool."""
        mock = make_mock_client()
        model = FakeToolModel(
            responses=[
                AIMessage(content="I don't need to run anything."),
            ]
        )

        with patch("k8s_agent_sandbox.SandboxClient", return_value=mock):
            backend = KubernetesSandbox(
                template_name="test-tpl",
                namespace="test-ns",
            )

            agent = create_agent(
                model,
                middleware=[FilesystemMiddleware(backend=backend)],
            )

            result = agent.invoke({"messages": [HumanMessage(content="Just say hello")]})

        # Agent responded without tool calls → sandbox was never started
        assert not backend._started
        assert "don't need to run anything" in result["messages"][-1].content
        mock.create_sandbox.assert_not_called()

    def test_backend_started_after_execute(self) -> None:
        """Sandbox is lazily started when the agent calls execute."""
        mock = make_mock_client(
            run_result=FakeExecutionResult(stdout="ok\n", exit_code=0),
        )
        result, backend = _run_agent(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo ok"},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done."),
            ],
            mock_client=mock,
            user_message="Run echo ok",
        )

        assert backend._started
        mock.create_sandbox.assert_called_once()
        backend.stop()
