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

import contextlib
import threading

import pytest
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from langchain_k8s import KubernetesSandbox, create_kubernetes_sandbox
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
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'deep-agent-hello'"},
                            "id": "call_deep_exec",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Command ran."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke({"messages": [HumanMessage(content="Run echo")]})

        messages = result["messages"]
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        assert any("deep-agent-hello" in m.content for m in tool_msgs)

    def test_deep_agent_python_script(self) -> None:
        """Deep agent runs a Python script via execute."""
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": 'python3 -c "import sys; print(sys.version_info[:2])"'},
                            "id": "call_py",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Python version retrieved."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke({"messages": [HumanMessage(content="Get Python version")]})

        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        # Should contain a version tuple like (3, 12)
        assert any("3," in m.content or "3." in m.content for m in tool_msgs)

    def test_deep_agent_multi_step(self) -> None:
        """Deep agent makes two sequential tool calls through the full stack."""
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'deep-step-1' > /tmp/deep-multi.txt"},
                            "id": "call_s1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "cat /tmp/deep-multi.txt"},
                            "id": "call_s2",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="File read."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke({"messages": [HumanMessage(content="Write then read")]})

        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        # The second tool message should contain the file content
        assert any("deep-step-1" in m.content for m in tool_msgs)

    def test_deep_agent_failed_command(self) -> None:
        """Deep agent receives failure info through the full middleware stack."""
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "cat /nonexistent_deep_agent_file"},
                            "id": "call_deep_fail",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="File not found."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke({"messages": [HumanMessage(content="Read missing file")]})

        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert any("No such file" in m.content for m in tool_msgs)

    def test_deep_agent_lazy_init(self) -> None:
        """Deep agent lazily starts the sandbox on first tool call."""
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        )
        assert not backend._started

        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'deep-lazy'"},
                            "id": "call_deep_lazy",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done."),
            ]
        )

        agent = create_deep_agent(model=model, backend=backend)
        result = agent.invoke({"messages": [HumanMessage(content="Run command")]})

        assert backend._started
        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert any("deep-lazy" in m.content for m in tool_msgs)
        backend.stop()

    def test_deep_agent_no_tool_call_skips_sandbox(self) -> None:
        """Deep agent that doesn't call any tools never starts the sandbox."""
        model = FakeToolModel(
            responses=[
                AIMessage(content="No tools needed here."),
            ]
        )

        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        )

        agent = create_deep_agent(model=model, backend=backend)
        result = agent.invoke({"messages": [HumanMessage(content="Just answer")]})

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
        model_1 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'deep-reuse' > /tmp/deep-reuse.txt"},
                            "id": "call_dw",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Written."),
            ]
        )
        agent_1 = create_deep_agent(model=model_1, backend=backend)
        agent_1.invoke({"messages": [HumanMessage(content="Write file")]})
        first_id = backend.id

        # Invocation 2: read it back — same pod
        model_2 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "cat /tmp/deep-reuse.txt"},
                            "id": "call_dr",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Read."),
            ]
        )
        agent_2 = create_deep_agent(model=model_2, backend=backend)
        result_2 = agent_2.invoke({"messages": [HumanMessage(content="Read file")]})

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
        model_1 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'ephemeral' > /tmp/deep-eph.txt"},
                            "id": "call_e1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done."),
            ]
        )
        agent_1 = create_deep_agent(model=model_1, backend=backend)
        agent_1.invoke({"messages": [HumanMessage(content="Write")]})
        first_id = backend.id

        backend.stop()

        # Invocation 2 — new pod, old file gone
        model_2 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "cat /tmp/deep-eph.txt 2>&1 || echo 'GONE'"},
                            "id": "call_e2",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Checked."),
            ]
        )
        agent_2 = create_deep_agent(model=model_2, backend=backend)
        result_2 = agent_2.invoke({"messages": [HumanMessage(content="Read on new pod")]})

        assert backend.id != first_id
        tool_msgs = [m for m in result_2["messages"] if isinstance(m, ToolMessage)]
        assert any("No such file" in m.content or "GONE" in m.content for m in tool_msgs)
        backend.stop()

    def test_deep_agent_parallel_independent_backends(self) -> None:
        """Parallel deep agents with independent backends get isolated pods."""
        errors: list[Exception] = []
        sandbox_ids: dict[int, str] = {}

        def deep_agent_worker(idx: int) -> None:
            try:
                model = FakeToolModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "execute",
                                    "args": {"command": f"echo 'deep-iso-{idx}'"},
                                    "id": f"call_di_{idx}",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(content=f"Agent {idx} done."),
                    ]
                )

                with KubernetesSandbox(
                    template_name=TEMPLATE,
                    namespace=NAMESPACE,
                ) as backend:
                    agent = create_deep_agent(model=model, backend=backend)
                    result = agent.invoke({"messages": [HumanMessage(content=f"Task {idx}")]})
                    sandbox_ids[idx] = backend.id

                    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
                    assert any(f"deep-iso-{idx}" in m.content for m in tool_msgs)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=deep_agent_worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Deep agent errors: {errors}"
        assert len(sandbox_ids) == 3
        unique_ids = set(sandbox_ids.values())
        assert len(unique_ids) == 3, f"Expected 3 unique ids, got {len(unique_ids)}: {sandbox_ids}"


# ---------------------------------------------------------------------------
# Lifecycle management: reconnect, labels, claim_name
# ---------------------------------------------------------------------------


class TestDeepAgentReconnect:
    """Deep agent reconnection tests through the full agent stack."""

    def test_deep_agent_reconnect_preserves_state(self) -> None:
        """Agent writes a file, process restarts, reconnects, reads file back."""
        # Phase 1: create sandbox, run agent that writes a marker file
        backend_1 = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            skip_cleanup=True,
        )
        model_1 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'deep-reconnect-marker' > /tmp/deep-reconnect.txt"},
                            "id": "call_rc_write",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="File written."),
            ]
        )
        agent_1 = create_deep_agent(model=model_1, backend=backend_1)
        agent_1.invoke({"messages": [HumanMessage(content="Write marker")]})
        saved_claim = backend_1.claim_name
        assert saved_claim is not None
        backend_1.stop()

        # Phase 2: reconnect and verify the file is still there
        try:
            backend_2 = KubernetesSandbox(
                template_name=TEMPLATE,
                namespace=NAMESPACE,
                claim_name=saved_claim,
            )
            model_2 = FakeToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "cat /tmp/deep-reconnect.txt"},
                                "id": "call_rc_read",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="File read."),
                ]
            )
            agent_2 = create_deep_agent(model=model_2, backend=backend_2)
            result = agent_2.invoke({"messages": [HumanMessage(content="Read marker")]})
            tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
            assert any("deep-reconnect-marker" in m.content for m in tool_msgs)
            assert backend_2.claim_name == saved_claim
        finally:
            if backend_2._started:
                backend_2._skip_cleanup = False
                backend_2.stop()

    def test_deep_agent_reconnect_multi_step(self) -> None:
        """Reconnected agent performs multi-step work on the existing pod."""
        # Phase 1: create sandbox and write initial state
        backend_1 = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            skip_cleanup=True,
        )
        model_1 = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'line1' > /tmp/deep-multi-rc.txt"},
                            "id": "call_mrc_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Written."),
            ]
        )
        agent_1 = create_deep_agent(model=model_1, backend=backend_1)
        agent_1.invoke({"messages": [HumanMessage(content="Write")]})
        saved_claim = backend_1.claim_name
        backend_1.stop()

        # Phase 2: reconnect and do a multi-step append + read
        try:
            backend_2 = KubernetesSandbox(
                template_name=TEMPLATE,
                namespace=NAMESPACE,
                claim_name=saved_claim,
            )
            model_2 = FakeToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "echo 'line2' >> /tmp/deep-multi-rc.txt"},
                                "id": "call_mrc_2",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "cat /tmp/deep-multi-rc.txt"},
                                "id": "call_mrc_3",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="Done."),
                ]
            )
            agent_2 = create_deep_agent(model=model_2, backend=backend_2)
            result = agent_2.invoke({"messages": [HumanMessage(content="Append and read")]})
            tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
            # The cat output should contain both lines
            assert any("line1" in m.content and "line2" in m.content for m in tool_msgs)
        finally:
            if backend_2._started:
                backend_2._skip_cleanup = False
                backend_2.stop()


class TestDeepAgentLabels:
    """Deep agent tests with labeled sandboxes."""

    def test_deep_agent_with_labels(self) -> None:
        """Deep agent runs successfully on a labeled sandbox."""
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'labeled-sandbox'"},
                            "id": "call_label",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            labels={"agent": "deep-test", "env": "integration"},
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke({"messages": [HumanMessage(content="Run")]})
            assert backend.claim_name is not None

        tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert any("labeled-sandbox" in m.content for m in tool_msgs)

    def test_deep_agent_labels_with_skip_cleanup(self) -> None:
        """Labeled sandbox with skip_cleanup preserves claim for later reconnect."""
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
            labels={"session": "deep-label-test"},
            skip_cleanup=True,
        )
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'label+skip'"},
                            "id": "call_ls",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done."),
            ]
        )
        agent = create_deep_agent(model=model, backend=backend)
        agent.invoke({"messages": [HumanMessage(content="Run")]})
        saved_claim = backend.claim_name
        assert saved_claim is not None
        backend.stop()

        # Reconnect to the labeled sandbox
        try:
            backend_2 = KubernetesSandbox(
                template_name=TEMPLATE,
                namespace=NAMESPACE,
                claim_name=saved_claim,
            )
            backend_2.start()
            resp = backend_2.execute("echo 'reconnected-labeled'")
            assert resp.exit_code == 0
            assert "reconnected-labeled" in resp.output
        finally:
            if backend_2._started:
                backend_2._skip_cleanup = False
                backend_2.stop()


class TestDeepAgentClaimName:
    """Deep agent tests for claim_name property exposure."""

    def test_claim_name_available_during_agent_run(self) -> None:
        """claim_name is accessible while the agent is running."""
        model = FakeToolModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "execute",
                            "args": {"command": "echo 'claim-check'"},
                            "id": "call_cn",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Done."),
            ]
        )

        with KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        ) as backend:
            agent = create_deep_agent(model=model, backend=backend)
            agent.invoke({"messages": [HumanMessage(content="Run")]})
            # claim_name is populated after the agent triggers sandbox creation
            assert backend.claim_name is not None
            assert isinstance(backend.claim_name, str)

    def test_claim_name_none_before_start(self) -> None:
        """claim_name is None before any agent invocation starts the sandbox."""
        backend = KubernetesSandbox(
            template_name=TEMPLATE,
            namespace=NAMESPACE,
        )
        assert backend.claim_name is None


# ---------------------------------------------------------------------------
# Thread-scoped sandbox (ecosystem-standard mode)
# ---------------------------------------------------------------------------


class TestDeepAgentThreadScoped:
    """Deep agent tests using the ecosystem-standard sandbox handle pattern.

    Simulates the thread-scoped graph factory documented in the production
    guide: each "thread" gets its own sandbox via ``create_kubernetes_sandbox()``,
    and resuming the thread reuses the same sandbox with its state intact.
    """

    def _make_client(self):  # noqa: ANN202
        from k8s_agent_sandbox import SandboxClient
        from k8s_agent_sandbox.models import SandboxLocalTunnelConnectionConfig

        return SandboxClient(
            connection_config=SandboxLocalTunnelConnectionConfig(),
        )

    def test_thread_scoped_sandbox_execute(self) -> None:
        """Agent runs on a sandbox created via create_kubernetes_sandbox()."""
        client = self._make_client()
        claim = "thread-exec-test"
        try:
            backend = create_kubernetes_sandbox(
                client=client,
                claim_name=claim,
                template_name=TEMPLATE,
                namespace=NAMESPACE,
                labels={"thread_id": "t-exec"},
            )
            assert backend._started
            assert backend._owns_lifecycle is False

            model = FakeToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "echo 'thread-scoped-hello'"},
                                "id": "call_ts_exec",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="Done."),
                ]
            )
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke({"messages": [HumanMessage(content="Run")]})

            tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
            assert any("thread-scoped-hello" in m.content for m in tool_msgs)
        finally:
            client.delete_sandbox(claim, NAMESPACE)

    def test_thread_scoped_sandbox_state_persists(self) -> None:
        """Resuming a thread reuses the same sandbox — filesystem state persists.

        Simulates two sequential agent invocations on the same thread_id:
        the first writes a file, the second reads it back from the same pod.

        Uses a full UUID as thread_id to validate that Kubernetes claim names
        support the length (``sandbox-<uuid>`` = 45 chars, well under the
        253-char DNS subdomain limit).
        """
        import uuid

        client = self._make_client()
        thread_id = str(uuid.uuid4())
        claim = f"sandbox-{thread_id}"

        try:
            # Turn 1: agent writes a marker file
            backend_1 = create_kubernetes_sandbox(
                client=client,
                claim_name=claim,
                template_name=TEMPLATE,
                namespace=NAMESPACE,
                labels={"thread_id": thread_id},
            )
            model_1 = FakeToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "echo 'persist-marker' > /tmp/thread-persist.txt"},
                                "id": "call_tp_w",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="Written."),
                ]
            )
            agent_1 = create_deep_agent(model=model_1, backend=backend_1)
            agent_1.invoke({"messages": [HumanMessage(content="Write")]})

            # Turn 2: same thread_id -> same sandbox (get-or-create finds existing)
            backend_2 = create_kubernetes_sandbox(
                client=client,
                claim_name=claim,
                template_name=TEMPLATE,
                namespace=NAMESPACE,
                labels={"thread_id": thread_id},
            )
            model_2 = FakeToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "cat /tmp/thread-persist.txt"},
                                "id": "call_tp_r",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="Read."),
                ]
            )
            agent_2 = create_deep_agent(model=model_2, backend=backend_2)
            result = agent_2.invoke({"messages": [HumanMessage(content="Read")]})

            tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
            assert any("persist-marker" in m.content for m in tool_msgs)
        finally:
            client.delete_sandbox(claim, NAMESPACE)

    def test_thread_scoped_sandbox_multi_step(self) -> None:
        """Thread-scoped sandbox supports multi-step agent interactions."""
        client = self._make_client()
        claim = "thread-multi-step"

        try:
            backend = create_kubernetes_sandbox(
                client=client,
                claim_name=claim,
                template_name=TEMPLATE,
                namespace=NAMESPACE,
            )
            model = FakeToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "echo 'step-1' > /tmp/ts-multi.txt"},
                                "id": "call_tsm_1",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "echo 'step-2' >> /tmp/ts-multi.txt && cat /tmp/ts-multi.txt"},
                                "id": "call_tsm_2",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="Done."),
                ]
            )
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke({"messages": [HumanMessage(content="Multi-step")]})

            tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
            assert any("step-1" in m.content and "step-2" in m.content for m in tool_msgs)
        finally:
            client.delete_sandbox(claim, NAMESPACE)

    def test_thread_scoped_parallel_threads(self) -> None:
        """Parallel threads get isolated sandboxes via create_kubernetes_sandbox()."""
        client = self._make_client()
        num_threads = 3
        errors: list[Exception] = []
        sandbox_ids: dict[int, str] = {}
        claims = [f"thread-parallel-{i}" for i in range(num_threads)]

        def thread_worker(idx: int) -> None:
            try:
                backend = create_kubernetes_sandbox(
                    client=client,
                    claim_name=claims[idx],
                    template_name=TEMPLATE,
                    namespace=NAMESPACE,
                    labels={"thread_id": f"parallel-{idx}"},
                )
                model = FakeToolModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "execute",
                                    "args": {"command": f"echo 'thread-{idx}'"},
                                    "id": f"call_par_{idx}",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(content=f"Thread {idx} done."),
                    ]
                )
                agent = create_deep_agent(model=model, backend=backend)
                result = agent.invoke({"messages": [HumanMessage(content=f"Task {idx}")]})
                sandbox_ids[idx] = backend.id

                tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
                assert any(f"thread-{idx}" in m.content for m in tool_msgs)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=thread_worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Clean up all sandboxes
        for claim in claims:
            with contextlib.suppress(Exception):
                client.delete_sandbox(claim, NAMESPACE)

        assert not errors, f"Thread errors: {errors}"
        assert len(sandbox_ids) == num_threads
        unique_ids = set(sandbox_ids.values())
        assert len(unique_ids) == num_threads, (
            f"Expected {num_threads} unique ids, got {len(unique_ids)}: {sandbox_ids}"
        )

    def test_handle_mode_with_enterprise_features(self) -> None:
        """Ecosystem-standard mode works with allow_prefixes and virtual_mode.

        Uses ``/tmp`` as ``root_dir`` because it is always writable inside
        the container.  Directories like ``/workspace`` require an explicit
        writable volume mount in the ``SandboxTemplate`` pod spec;
        without one the container's OS user gets ``PermissionError``
        even though the sandbox policy (``allow_prefixes``) allows the
        path.  See the README "Container permissions vs. sandbox policy"
        section for details.
        """
        client = self._make_client()
        claim = "thread-enterprise"

        try:
            backend = create_kubernetes_sandbox(
                client=client,
                claim_name=claim,
                template_name=TEMPLATE,
                namespace=NAMESPACE,
                allow_prefixes=["/tmp/"],
                virtual_mode=True,
                root_dir="/tmp",
            )

            model = FakeToolModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "execute",
                                "args": {"command": "echo 'enterprise-test'"},
                                "id": "call_ent",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="Done."),
                ]
            )
            agent = create_deep_agent(model=model, backend=backend)
            result = agent.invoke({"messages": [HumanMessage(content="Run")]})

            tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
            assert any("enterprise-test" in m.content for m in tool_msgs)

            # write() under /tmp should work (resolved + allowed)
            write_result = backend.write("/src/test.py", "print('ok')")
            assert write_result.error is None

            # read back what we wrote
            read_result = backend.read("/src/test.py")
            assert read_result.error is None
            assert read_result.file_data is not None
            assert read_result.file_data["content"] == "print('ok')"
        finally:
            client.delete_sandbox(claim, NAMESPACE)
