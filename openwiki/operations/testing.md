# Operations: Testing

This document explains the test structure, how to run tests, and test patterns used in langchain-k8s.

**Source:** `tests/` directory, `tests/conftest.py` (fixtures), `Makefile` (test targets), `pyproject.toml` (test config)

## Test Structure

```
tests/
├── conftest.py                    Shared fixtures and mock factory
├── unit/
│   ├── test_sandbox.py            ~2,000 lines, comprehensive unit tests
│   ├── test_agent.py              Agent integration tests (mocked)
│   ├── test_proxy.py              Proxy patch tests
│   └── test_imports.py            Smoke test (imports work)
└── integration/
    ├── conftest.py                Integration-specific fixtures
    ├── test_kind.py               ~1,000 lines, integration tests
    ├── test_agent_kind.py         Agent integration tests (real cluster)
    └── test_deepagent_kind.py     DeepAgent integration tests
```

## Running Tests

### Quick Start

```bash
# Install dependencies
uv sync

# Run unit tests (no cluster required)
make test-unit

# Run integration tests (requires Kind cluster)
./scripts/kind-setup.sh
make test-integration
./scripts/kind-teardown.sh

# Run all tests
make test

# See all available targets
make help
```

### Unit Tests

Unit tests mock the `SandboxClient` SDK and test `KubernetesSandbox` logic in isolation.

```bash
# Run all unit tests
uv run pytest tests/unit/ -v

# Run specific test file
uv run pytest tests/unit/test_sandbox.py -v

# Run specific test function
uv run pytest tests/unit/test_sandbox.py::TestExecute::test_execute_success -v

# Run with coverage
uv run pytest tests/unit/ --cov=src/langchain_k8s --cov-report=html
# Open htmlcov/index.html
```

**Test files:**

- `test_sandbox.py` — Most comprehensive; tests `KubernetesSandbox` features (execute, file ops, policies, virtual mode)
- `test_agent.py` — Tests integration with LangChain agents
- `test_proxy.py` — Tests the Kubernetes client NO_PROXY monkey-patch
- `test_imports.py` — Smoke test; verifies imports work

### Integration Tests

Integration tests create a real Kind cluster and run actual sandboxes. These tests verify end-to-end behavior.

```bash
# Setup Kind cluster (one-time)
./scripts/kind-setup.sh

# Run integration tests
uv run pytest tests/integration/ -v -m integration

# Run specific integration test
uv run pytest tests/integration/test_kind.py::TestIntegration::test_execute -v -m integration

# Teardown Kind cluster
./scripts/kind-teardown.sh
```

**Test files:**

- `test_kind.py` — Tests basic sandbox lifecycle, execute, file operations
- `test_agent_kind.py` — Tests agent integration (mocked agent)
- `test_deepagent_kind.py` — Tests real DeepAgent integration

### Test Markers

```bash
# Run only unit tests (skip integration)
uv run pytest tests/ -v -m "not integration"

# Run only integration tests
uv run pytest tests/ -v -m integration

# List all markers
uv run pytest --markers
```

**Marker definition:** In `pyproject.toml`

```toml
markers = [
    "integration: marks tests requiring a Kind cluster (deselect with '-m \"not integration\"')",
]
```

## Mock Fixtures

### `make_mock_client()`

Factory function in `tests/conftest.py` that creates a mock `SandboxClient`:

```python
from tests.conftest import make_mock_client

# Create mock client
client = make_mock_client(
    claim_name="test-claim-abc",
    run_result=FakeExecutionResult(
        stdout="file content",
        stderr="",
        exit_code=0,
    ),
)

# Use mock client
backend = KubernetesSandbox(sandbox=client._mock_sandbox_handle)
result = backend.execute("cat /tmp/file.txt")
assert result.output == "file content"
```

### Fixtures

**`mock_sandbox_client`** (pytest fixture):

- Provides a mock `SandboxClient`
- Automatically patches `k8s_agent_sandbox.SandboxClient`
- Pre-configured with a mock sandbox handle

```python
def test_execute(mock_sandbox_client):
    # mock_sandbox_client is auto-patched and ready
    backend = KubernetesSandbox(
        template_name="test-template",
        namespace="test-ns",
    )
    backend.start()
    result = backend.execute("echo hello")
    assert result.exit_code == 0
```

**`sandbox`** (pytest fixture):

- Provides a fully initialized `KubernetesSandbox` with mock client
- Auto-patched, ready to use
- Auto-cleanup on test end

```python
def test_write(sandbox):
    # sandbox is ready; no need to call start()
    result = sandbox.write("/tmp/file.txt", b"hello")
    assert result.success
```

**`started_sandbox`** (pytest fixture):

- Like `sandbox`, but `start()` has already been called

```python
def test_execute_after_start(started_sandbox):
    result = started_sandbox.execute("echo hello")
    assert result.exit_code == 0
```

## Test Patterns

### Testing `execute()`

```python
def test_execute_success(sandbox):
    """Test successful command execution."""
    result = sandbox.execute("echo hello")
    assert result.exit_code == 0
    assert result.output == "hello\n"
    assert result.error == ""
```

### Testing File Operations

```python
def test_write_and_read(sandbox):
    """Test write then read."""
    sandbox.write("/tmp/test.txt", b"hello")
    result = sandbox.read("/tmp/test.txt")
    assert result.content == b"hello"
```

### Testing Error Handling

```python
def test_file_not_found(sandbox):
    """Test FileNotFoundError classification."""
    try:
        sandbox.read("/nonexistent/file.txt")
        assert False, "Should raise"
    except FileOperationError as e:
        assert e.type == FileNotFoundError
```

### Testing Virtual Mode

```python
def test_virtual_mode_path_traversal(sandbox):
    """Test that path traversal is blocked."""
    backend = KubernetesSandbox(
        template_name="test-template",
        namespace="test-ns",
        virtual_mode=True,
        root_dir="/workspace",
    )

    with pytest.raises(ValueError, match="Path traversal"):
        backend.read("../../../etc/passwd")
```

### Testing Policy Enforcement

```python
def test_allow_prefixes_blocks_write(sandbox):
    """Test write policy blocks unauthorized paths."""
    backend = KubernetesSandbox(
        template_name="test-template",
        namespace="test-ns",
        allow_prefixes=["/workspace/"],
    )

    try:
        backend.write("/etc/passwd", b"bad")
        assert False, "Should raise"
    except FileOperationError as e:
        assert e.type == ValueError
        assert "not under any allowed prefix" in str(e)
```

## Common Test Issues

### "Cannot import k8s_agent_sandbox"

```bash
# Ensure dev dependencies are installed
uv sync
```

### "Mock SandboxClient not found"

The mock is auto-patched in `conftest.py`. If not working:

```python
# Check the patch path
from unittest.mock import patch

# This should match what conftest does
with patch("k8s_agent_sandbox.SandboxClient", return_value=mock_client):
    ...
```

### Integration tests fail with "cluster not found"

```bash
# Ensure Kind cluster is running
./scripts/kind-setup.sh

# Verify cluster
kubectl cluster-info
kubectl get nodes
```

### Integration test timeout

```bash
# Increase pytest timeout
uv run pytest tests/integration/ -v --timeout=60
```

## Continuous Integration

The repository runs tests on every pull request via GitHub Actions:

**`.github/workflows/ci.yml`:**

- Runs on Python 3.11 and 3.12
- Linting (`ruff check`)
- Type checking (`pyright`)
- Unit tests (with coverage)
- Excludes integration tests (no cluster in CI)

**`.github/workflows/integration.yml`:**

- Manual trigger (`workflow_dispatch`)
- Spins up Kind cluster
- Runs integration tests
- Destroys cluster after tests

## Coverage

```bash
# Generate coverage report
uv run pytest tests/unit/ --cov=src/langchain_k8s --cov-report=html

# View report
open htmlcov/index.html
```

**Current coverage:** See SonarCloud badge in `README.md`

## Debugging Tests

### Print Debug Output

```python
def test_something(sandbox):
    import logging
    logging.basicConfig(level=logging.DEBUG)

    result = sandbox.execute("echo hello")
    print(f"Exit code: {result.exit_code}")
    print(f"Output: {result.output}")
```

### Run Single Test with Logging

```bash
uv run pytest tests/unit/test_sandbox.py::TestExecute::test_execute_success -v -s --log-cli-level=DEBUG
```

### Inspect Mock Calls

```python
def test_mock_inspection(mock_sandbox_client):
    backend = KubernetesSandbox(
        template_name="test-template",
        namespace="test-ns",
    )
    backend.start()
    backend.execute("echo hello")

    # Check mock was called
    sandbox_handle = mock_sandbox_client._mock_sandbox_handle
    sandbox_handle.commands.run.assert_called()

    # Check call arguments
    call_args = sandbox_handle.commands.run.call_args
    print(call_args)
```

## Adding New Tests

When adding a feature or fixing a bug:

1. **Write a unit test** in `tests/unit/test_sandbox.py`
2. **Use the `sandbox` or `started_sandbox` fixture**
3. **Mock the SDK** (don't create real pods)
4. **Test normal case and error cases**
5. **Add integration test** if cluster interaction is involved (in `tests/integration/test_kind.py`)

Example:

```python
def test_new_feature(sandbox):
    """Test the new feature."""
    result = sandbox.execute("some_command")
    assert result.exit_code == 0

def test_new_feature_error(sandbox):
    """Test error handling."""
    mock_sandbox = sandbox._sandbox
    mock_sandbox.commands.run.side_effect = ConnectionError("Pod died")

    # Should auto-reconnect and retry
    result = sandbox.execute("some_command")
    # ...
```

## Further Reading

- [Integrations: Upgrading](../integrations/upgrading.md) — Testing after dependency upgrades
- [Operations: Deployment](deployment.md) — Setting up local cluster for integration tests
- `Makefile` — Available test targets
- `pyproject.toml` — Test configuration
