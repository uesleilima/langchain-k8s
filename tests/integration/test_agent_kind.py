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
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from langchain_k8s import KubernetesSandbox
from tests.conftest import FakeToolModel

pytestmark = pytest.mark.integration

TEMPLATE = "python-sandbox-template"
NAMESPACE = "agent-sandbox-system"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeepAgentExecute:
    """Agent invokes the ``execute`` tool against a real sandbox pod."""

    def test_agent_execute_echo(self) -> None:
        """Agent calls execute(echo) and receives the output."""
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'hello from agent'"},
                            "id": "call_echo",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="The sandbox said hello."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_agent(
                model,
                middleware=[FilesystemMiddleware(backend=backend)],
            )
            result = agent.invoke({"messages": [HumanMessage(content="Run echo")]})

        messages = result["messages"]
        assert len(messages) == 4
        tool_msg = messages[2]
        assert isinstance(tool_msg, ToolMessage)
        assert "hello from agent" in tool_msg.content
        assert "exit code 0" in tool_msg.content

    def test_agent_execute_python(self) -> None:
        """Agent runs a Python one-liner in the sandbox."""
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": 'python3 -c "print(2 + 2)"'},
                            "id": "call_py",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Python computed 4."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_agent(
                model,
                middleware=[FilesystemMiddleware(backend=backend)],
            )
            result = agent.invoke({"messages": [HumanMessage(content="Compute 2+2 with Python")]})

        tool_msg = result["messages"][2]
        assert isinstance(tool_msg, ToolMessage)
        assert "4" in tool_msg.content
        assert "exit code 0" in tool_msg.content

    def test_agent_execute_failed_command(self) -> None:
        """Agent receives non-zero exit code when command fails."""
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "ls /no_such_directory_xyz"},
                            "id": "call_fail",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="The directory does not exist."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_agent(
                model,
                middleware=[FilesystemMiddleware(backend=backend)],
            )
            result = agent.invoke({"messages": [HumanMessage(content="List a bad dir")]})

        tool_msg = result["messages"][2]
        assert isinstance(tool_msg, ToolMessage)
        assert "No such file or directory" in tool_msg.content
        assert "failed" in tool_msg.content

    def test_agent_multi_step_execute(self) -> None:
        """Agent makes two sequential execute calls — state persists across them."""
        model = FakeToolModel(
            responses=[
                # Step 1: write a file
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'agent-data' > /tmp/agent-test.txt"},
                            "id": "call_write",
                            "type": "tool_call",
                        }
                    ],
                ),
                # Step 2: read it back
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "cat /tmp/agent-test.txt"},
                            "id": "call_read",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="File contains agent-data."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_agent(
                model,
                middleware=[FilesystemMiddleware(backend=backend)],
            )
            result = agent.invoke({"messages": [HumanMessage(content="Write and read a file")]})

        messages = result["messages"]
        # Human → AI(tool) → Tool → AI(tool) → Tool → AI(final) = 6
        assert len(messages) == 6
        # The second tool result should contain the file content
        second_tool_msg = messages[4]
        assert isinstance(second_tool_msg, ToolMessage)
        assert "agent-data" in second_tool_msg.content


class TestDeepAgentFileOps:
    """Agent uses write_file / read_file which route through the backend."""

    def test_agent_execute_and_read_file(self) -> None:
        """Agent creates a file via execute, then reads it with read_file."""
        model = FakeToolModel(
            responses=[
                # Step 1: create the file via execute
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo -n 'hello agent' > /tmp/read-test.txt"},
                            "id": "call_create",
                            "type": "tool_call",
                        }
                    ],
                ),
                # Step 2: read it via read_file (goes through backend.read → download_files)
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/tmp/read-test.txt"},
                            "id": "call_read",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done reading the file."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_agent(
                model,
                middleware=[FilesystemMiddleware(backend=backend)],
            )
            result = agent.invoke({"messages": [HumanMessage(content="Create and read a file")]})

        messages = result["messages"]
        assert len(messages) == 6
        read_result = messages[4]
        assert isinstance(read_result, ToolMessage)
        assert "hello agent" in read_result.content


class TestDeepAgentLifecycle:
    """Verify lazy init and lifecycle through the agent loop."""

    def test_sandbox_not_started_until_tool_call(self) -> None:
        """If the agent never calls a tool, the sandbox is never created."""
        model = FakeToolModel(
            responses=[
                AIMessage(content="No tools needed, just a plain answer."),
            ]
        )

        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        )

        agent = create_agent(
            model,
            middleware=[FilesystemMiddleware(backend=backend)],
        )
        result = agent.invoke({"messages": [HumanMessage(content="Just answer without tools")]})

        assert not backend._started
        assert "No tools needed" in result["messages"][-1].content

    def test_sandbox_lazy_started_by_agent(self) -> None:
        """The sandbox is lazily created when the agent's first tool call arrives."""
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo lazy-start"},
                            "id": "call_lazy",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done."),
            ]
        )

        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        )
        assert not backend._started

        agent = create_agent(
            model,
            middleware=[FilesystemMiddleware(backend=backend)],
        )
        result = agent.invoke({"messages": [HumanMessage(content="Run something")]})

        assert backend._started
        tool_msg = result["messages"][2]
        assert isinstance(tool_msg, ToolMessage)
        assert "lazy-start" in tool_msg.content
        backend.stop()

    def test_multiple_invocations_same_backend_reuse(self) -> None:
        """Multiple agent.invoke() calls reuse the same sandbox pod (reuse_sandbox=True).

        State written by the first invocation is visible to the second.
        """
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            reuse_sandbox=True,
        )

        # First invocation: write a marker file
        model_1 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'invoke-1-marker' > /tmp/multi-invoke.txt"},
                            "id": "call_w",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Written."),
            ]
        )
        agent_1 = create_agent(
            model_1,
            middleware=[FilesystemMiddleware(backend=backend)],
        )
        agent_1.invoke({"messages": [HumanMessage(content="Write marker")]})
        sandbox_id_after_first = backend.id

        # Second invocation: read it back — same pod, file still there
        model_2 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "cat /tmp/multi-invoke.txt"},
                            "id": "call_r",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Read it."),
            ]
        )
        agent_2 = create_agent(
            model_2,
            middleware=[FilesystemMiddleware(backend=backend)],
        )
        result_2 = agent_2.invoke({"messages": [HumanMessage(content="Read marker")]})

        # Same pod was reused
        assert backend.id == sandbox_id_after_first
        tool_msg = result_2["messages"][2]
        assert isinstance(tool_msg, ToolMessage)
        assert "invoke-1-marker" in tool_msg.content
        backend.stop()

    def test_multiple_invocations_no_tool_then_tool(self) -> None:
        """First invoke has no tool calls (sandbox stays idle), second invoke triggers lazy start."""
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        )

        # First invocation: no tool calls
        model_1 = FakeToolModel(
            responses=[
                AIMessage(content="I can answer this without tools."),
            ]
        )
        agent_1 = create_agent(
            model_1,
            middleware=[FilesystemMiddleware(backend=backend)],
        )
        agent_1.invoke({"messages": [HumanMessage(content="What is 1+1?")]})
        assert not backend._started

        # Second invocation: now the agent calls execute — lazy start happens here
        model_2 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'deferred-start'"},
                            "id": "call_deferred",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done."),
            ]
        )
        agent_2 = create_agent(
            model_2,
            middleware=[FilesystemMiddleware(backend=backend)],
        )
        result_2 = agent_2.invoke({"messages": [HumanMessage(content="Now run something")]})

        assert backend._started
        tool_msg = result_2["messages"][2]
        assert isinstance(tool_msg, ToolMessage)
        assert "deferred-start" in tool_msg.content
        backend.stop()

    def test_eager_start_then_agent_invoke(self) -> None:
        """Explicit start() before agent.invoke() — agent reuses the pre-warmed pod."""
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        )
        backend.start()
        sandbox_id_before = backend.id
        assert backend._started

        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'pre-warmed'"},
                            "id": "call_warm",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done."),
            ]
        )
        agent = create_agent(
            model,
            middleware=[FilesystemMiddleware(backend=backend)],
        )
        result = agent.invoke({"messages": [HumanMessage(content="Run on pre-warmed pod")]})

        # Same sandbox — no second pod was created
        assert backend.id == sandbox_id_before
        tool_msg = result["messages"][2]
        assert isinstance(tool_msg, ToolMessage)
        assert "pre-warmed" in tool_msg.content
        backend.stop()

    def test_shared_backend_across_parallel_agents(self) -> None:
        """Two agents sharing the same backend invoke concurrently — same pod."""
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            reuse_sandbox=True,
        )

        errors: list[Exception] = []
        results: dict[int, str] = {}

        def agent_worker(idx: int) -> None:
            try:
                model = FakeToolModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "execute",
                                    "args": {"command": f"echo 'agent-{idx}'"},
                                    "id": f"call_{idx}",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(content=f"Agent {idx} done."),
                    ]
                )
                agent = create_agent(
                    model,
                    middleware=[FilesystemMiddleware(backend=backend)],
                )
                result = agent.invoke({"messages": [HumanMessage(content=f"Agent {idx} task")]})
                tool_msg = result["messages"][2]
                assert isinstance(tool_msg, ToolMessage)
                results[idx] = tool_msg.content
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=agent_worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Agent errors: {errors}"
        assert len(results) == 3
        for i in range(3):
            assert f"agent-{i}" in results[i]
        backend.stop()

    def test_independent_backends_parallel_agents(self) -> None:
        """Each agent gets its own backend — separate pods, isolated state."""
        errors: list[Exception] = []
        sandbox_ids: dict[int, str] = {}

        def agent_worker(idx: int) -> None:
            try:
                model = FakeToolModel(
                    responses=[
                        # Write a unique marker
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "execute",
                                    "args": {
                                        "command": f"echo 'isolated-{idx}' > /tmp/iso.txt",
                                    },
                                    "id": f"call_w_{idx}",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        # Read it back to verify isolation
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "execute",
                                    "args": {"command": "cat /tmp/iso.txt"},
                                    "id": f"call_r_{idx}",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(content=f"Agent {idx} confirmed."),
                    ]
                )

                with KubernetesSandbox(
                    template_name=TEMPLATE,
                    namespace=NAMESPACE,
                ) as backend:
                    agent = create_agent(
                        model,
                        middleware=[FilesystemMiddleware(backend=backend)],
                    )
                    result = agent.invoke({"messages": [HumanMessage(content=f"Isolated task {idx}")]})
                    sandbox_ids[idx] = backend.id

                    # The read result should contain only this agent's marker
                    read_msg = result["messages"][4]
                    assert isinstance(read_msg, ToolMessage)
                    assert f"isolated-{idx}" in read_msg.content
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=agent_worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Agent errors: {errors}"
        assert len(sandbox_ids) == 3
        # All sandbox IDs should be unique (different pods)
        unique_ids = set(sandbox_ids.values())
        assert len(unique_ids) == 3, f"Expected 3 unique sandbox ids, got {len(unique_ids)}: {sandbox_ids}"

    def test_stop_and_reinvoke_creates_new_pod(self) -> None:
        """Stopping a backend and invoking again creates a fresh pod."""
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            reuse_sandbox=True,
        )

        # First invocation
        model_1 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'first-pod' > /tmp/pod-marker.txt"},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Written."),
            ]
        )
        agent_1 = create_agent(
            model_1,
            middleware=[FilesystemMiddleware(backend=backend)],
        )
        agent_1.invoke({"messages": [HumanMessage(content="Write marker")]})
        first_id = backend.id
        assert backend._started

        # Explicitly stop — destroys the pod
        backend.stop()
        assert not backend._started

        # Second invocation — should lazy-start a new pod
        model_2 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "cat /tmp/pod-marker.txt 2>&1 || echo 'GONE'"},
                            "id": "call_2",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Checked."),
            ]
        )
        agent_2 = create_agent(
            model_2,
            middleware=[FilesystemMiddleware(backend=backend)],
        )
        result_2 = agent_2.invoke({"messages": [HumanMessage(content="Read marker on new pod")]})

        # New pod — different ID, file should not exist
        assert backend._started
        assert backend.id != first_id
        tool_msg = result_2["messages"][2]
        assert isinstance(tool_msg, ToolMessage)
        # File from the old pod is gone
        assert "No such file" in tool_msg.content or "GONE" in tool_msg.content
        backend.stop()
