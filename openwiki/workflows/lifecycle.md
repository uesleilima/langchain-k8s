# Workflows: Lifecycle & Sandbox Reuse

This document explains sandbox lifecycle strategies, thread-scoped patterns for production, and the `create_kubernetes_sandbox()` factory function.

**Source:** `src/langchain_k8s/sandbox.py` (KubernetesSandbox class, create_kubernetes_sandbox function), `README.md` (sandbox lifecycle section), `specs/plans/foundation.md` (lifecycle design)

## Lifecycle Strategies

### Default: one lazily-created pod, with auto-reconnect (`reuse_sandbox=True`)

**One pod is created lazily and reused across all `execute()` calls.** Pod reuse is how config-based mode always works; the flag additionally enables reconnect-and-retry on connection failure.

```python
from langchain_k8s import KubernetesSandbox
from langchain_anthropic import ChatAnthropic
from deepagents import create_deep_agent

# Create backend (no pod created yet)
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    reuse_sandbox=True,  # default
)

# Create agent
agent = create_deep_agent(
    model=ChatAnthropic(model="claude-sonnet-4-20250514"),
    backend=backend,
)

# First invoke() creates the pod lazily
result1 = agent.invoke({
    "messages": [{"role": "user", "content": "Write a Python script"}]
})
# Pod created on first invoke()

# Second invoke() reuses the same pod
result2 = agent.invoke({
    "messages": [{"role": "user", "content": "Now add error handling"}]
})
# Pod is still alive; filesystem state from first invoke() persists

# Third invoke() still reuses the same pod
result3 = agent.invoke({
    "messages": [{"role": "user", "content": "Add logging"}]
})

# Clean up when done
backend.stop()  # Pod is deleted
```

**Characteristics:**

- ✅ Low latency (no cold-start per call)
- ✅ Filesystem state persists between calls (shared workspace)
- ✅ Efficient resource usage (one pod per agent)
- ✅ Auto-reconnects if pod dies (retry logic)
- ⚠️ Must explicitly call `stop()` or use context manager for cleanup
- ⚠️ Requires sticky sessions in multi-replica deployments (see [Architecture: Enterprise](../architecture/enterprise.md))

**Use case:** Long-lived agent sessions where you want low latency and shared workspace.

### Strict Failure Propagation (`reuse_sandbox=False`)

**`reuse_sandbox` controls error recovery, not pod lifetime.** Setting it to `False` does not give you a pod per invocation — there is no such mode. It disables the single automatic reconnect-and-retry inside `execute()`.

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    reuse_sandbox=False,
)

agent = create_deep_agent(
    model=ChatAnthropic(model="claude-sonnet-4-20250514"),
    backend=backend,
)

# One pod, created lazily on first use, reused for every invoke() —
# exactly as with the default. Filesystem state is shared throughout.
result1 = agent.invoke({...})
result2 = agent.invoke({...})

# The difference: if the pod dies mid-session, this raises instead of
# quietly provisioning a replacement and retrying.

backend.stop()   # deletes the SandboxClaim
```

**Characteristics:**

- ✅ Pod failures surface to the caller instead of being papered over
- ✅ No hidden pod churn — latency and resource use stay predictable
- ❌ A transient connection blip ends the session unless you retry yourself

**Use case:** Callers with their own retry or supervision strategy, or where a silently swapped-out pod (and its lost filesystem state) would be worse than a visible error.

**To get a fresh pod per task**, create a fresh backend per task, or call `stop()` then `start()` — both of which delete the claim and provision a new sandbox. This is independent of `reuse_sandbox`.

## Lazy Initialization

Both strategies use **lazy initialization**: the pod is not created in `__init__`, but on the first `execute()` call.

```python
# Pod not created yet (no cluster access)
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
)

# Pod created here (lazy)
result = backend.execute("echo hello")
```

### Why Lazy Initialization?

1. **Fast startup** — Constructor returns immediately; no cluster latency
2. **Error deferral** — Cluster connectivity issues surface when needed, not during setup
3. **Flexible deployment** — Can create backends in main thread, share across worker threads
4. **Explicit lifecycle** — You control when the pod is created via `start()` or implicit first `execute()`

## Thread-Scoped Sandboxes (Production Pattern)

For production deployments where each conversation thread gets its own sandbox, use `create_kubernetes_sandbox()`:

```python
from langchain_k8s import create_kubernetes_sandbox
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxGatewayConnectionConfig
from langchain_anthropic import ChatAnthropic
from deepagents import create_deep_agent
from langchain_core.runnables import RunnableConfig

# Shared SDK client (reused across threads)
client = SandboxClient(
    connection_config=SandboxGatewayConnectionConfig(gateway_name="sandbox-gw"),
)

async def make_agent(config: RunnableConfig):
    """Graph factory — each thread_id gets its own sandbox."""
    thread_id = config["configurable"]["thread_id"]

    # Get-or-create pattern: if a sandbox with this claim_name exists, reuse it
    backend = create_kubernetes_sandbox(
        client=client,
        claim_name=f"sandbox-{thread_id}",
        template_name="python-sandbox-template",
        namespace="agent-sandbox-system",
        labels={"thread_id": thread_id},
    )

    return create_deep_agent(
        model=ChatAnthropic(model="claude-sonnet-4-20250514"),
        backend=backend,
    )
```

### How It Works

1. `create_kubernetes_sandbox()` checks if a sandbox with `claim_name` already exists
2. If yes, reuse it (return the handle)
3. If no, create a new one
4. Attach `labels` to the sandbox for tracking (optional)

### Multi-Turn Conversation

```python
# Turn 1: User starts a conversation
config = {"configurable": {"thread_id": "user-session-abc"}}
agent = await make_agent(config)
result = agent.invoke({
    "messages": [{"role": "user", "content": "Write a Python script that fetches weather data"}]
})
# Sandbox "sandbox-user-session-abc" created with label thread_id=user-session-abc
# Agent writes files to /workspace/...
```

```python
# Turn 2: User continues the conversation (same thread_id)
config = {"configurable": {"thread_id": "user-session-abc"}}
agent = await make_agent(config)
result = agent.invoke({
    "messages": [{"role": "user", "content": "Now add error handling to the script"}]
})
# Same sandbox is reused
# Agent can read/modify files from Turn 1 — filesystem state persists
```

```python
# When the conversation ends, clean up
client.delete_sandbox("sandbox-user-session-abc", "agent-sandbox-system")
```

### Advantages

- **Per-thread isolation** — Each conversation has its own pod and workspace
- **Persistence** — Filesystem state survives across conversation turns
- **Automatic scaling** — Each active conversation gets one pod
- **Cleanup** — Can explicitly delete sandboxes when conversations end
- **Monitoring** — Labels make it easy to track sandboxes by user/session

### LangGraph Integration

```python
from langgraph.graph import StateGraph
from langchain_core.runnables import RunnableConfig

graph_builder = StateGraph(...)  # Your graph definition

async def get_agent(config: RunnableConfig):
    """Graph factory for LangGraph."""
    agent = await make_agent(config)
    # Wrap agent in a LangGraph node if needed
    return agent

# In your LangServe deployment
graph = graph_builder.compile()

# LangServe will call your graph with config["configurable"]["thread_id"]
# from the conversation state, ensuring sticky sessions
```

## Context Manager Pattern

Both strategies support context managers for automatic cleanup:

```python
# Persistent mode
with KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
) as backend:
    result = backend.execute("echo hello")
# Pod is automatically deleted on exit

# Or with ecosystem-standard mode
from k8s_agent_sandbox import SandboxClient
client = SandboxClient(...)
handle = client.create_sandbox(template="...")

with KubernetesSandbox(sandbox=handle) as backend:
    result = backend.execute("echo hello")
# Caller still owns cleanup (context manager doesn't delete the pod)
```

## Explicit Lifecycle Control

For more control, use `start()` and `stop()`:

```python
backend = KubernetesSandbox(...)

# Explicitly start the pod
backend.start()

try:
    result1 = backend.execute("echo step 1")
    result2 = backend.execute("echo step 2")
finally:
    # Explicitly stop (cleanup)
    backend.stop()
```

## Auto-Reconnection (Persistent Mode)

In persistent mode, if the pod dies unexpectedly, the next `execute()` call will automatically recreate it:

```python
backend = KubernetesSandbox(...)
backend.start()

result1 = backend.execute("echo hello")  # ✅ Works

# Pod dies (e.g., node reboot, OOMKilled, etc.)
# ...

result2 = backend.execute("echo hello")  # ✅ Automatically reconnects and recreates pod
```

**How it works:**

1. `execute()` tries to run the command
2. If it raises, and `reuse_sandbox` is `True` **and** the backend owns the lifecycle, log a warning
3. Call `_destroy_sandbox()` to tear down the dead sandbox — this clears `_started`
4. Call `_ensure_sandbox()`, which therefore provisions a replacement
5. Retry the command once
6. If it fails again, propagate the error

**Note:** Only retries once — the retry is not recursive. Auto-reconnect is inactive in handle mode regardless of `reuse_sandbox`, because the caller owns the Kubernetes resources.

## Warm Pools

The `k8s-agent-sandbox` controller can pre-warm a pool of idle sandbox pods, reducing cold-start latency:

`warmpool` is a `str | None` — the **name** of a `SandboxWarmPool` resource that already exists in the cluster. Pool size and idle behaviour live in that resource's spec, not in the constructor call.

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    warmpool="python-sandbox-pool",
)
```

**When to use:**

- High-throughput deployments (many concurrent agent sessions)
- Latency-sensitive applications (want sub-second pod ready time)
- Predictable load patterns (know roughly how many pods you need)

**Tradeoff:**

- ✅ Reduced cold-start latency
- ❌ Increased resource usage (idle pods consume CPU/memory)

## Skipping Cleanup (`skip_cleanup`)

For testing or debugging, you can skip sandbox cleanup:

```python
backend = KubernetesSandbox(
    template_name="python-sandbox-template",
    namespace="agent-sandbox-system",
    skip_cleanup=True,  # Pod will NOT be deleted on stop()
)

backend.start()
result = backend.execute("echo hello")
backend.stop()  # Pod is NOT deleted

# Pod is still running; you can inspect it:
# kubectl logs -n agent-sandbox-system <pod-name>
```

**Use case:** Debugging agent failures (inspect pod logs, filesystem).

## Comparing Lifecycle Strategies

| Aspect               | Single backend    | Backend per task              | Thread-Scoped                    |
| -------------------- | ----------------- | ----------------------------- | -------------------------------- |
| **Pods per agent**   | 1                 | 1 per task                    | 1 per thread                     |
| **Cold-start**       | Once (first call) | Every task                    | Once (first turn)                |
| **Filesystem state** | Persists          | Fresh each task               | Persists per thread              |
| **Best for**         | Long sessions     | One-off or batch jobs         | Multi-turn conversations         |
| **Scaling**          | Simple            | Simple                        | Complex (requires get-or-create) |
| **Cleanup**          | Manual (`stop()`) | Manual (`stop()` per backend) | Manual (by `claim_name`)         |

`reuse_sandbox` is orthogonal to all three columns — it selects whether `execute()` auto-recovers from a dropped connection, nothing more.

## Further Reading

- [Workflows: Connection](connection.md) — Connection modes and configuration
- [Architecture: Design](../architecture/design.md) — Lazy initialization, threading, error handling
- [Architecture: Enterprise](../architecture/enterprise.md) — Sticky sessions for horizontal scaling
- [Operations: Deployment](../operations/deployment.md) — Production setup patterns
