# Integrations: Upgrading Dependencies

This document provides step-by-step procedures for upgrading langchain-k8s's dependencies, with special focus on `k8s-agent-sandbox` upgrades (most likely to require code changes).

**Source:** `AGENTS.md` (Upgrading section), recent commits (PR merge logs)

## Upgrade Checklist

### General Upgrade Process

For any dependency upgrade:

1. **Identify what changed** — Read the release notes
2. **Update version constraint** — In `pyproject.toml`
3. **Sync dependencies** — `uv sync --all-groups` (updates `uv.lock`)
4. **Check for breaking changes** — Run tests
5. **Adapt code if needed** — API changes, renamed methods, etc.
6. **Update documentation** — `README.md`, `AGENTS.md`, specs
7. **Test thoroughly** — Unit + integration tests
8. **Create PR** — With clear description of changes

### `k8s-agent-sandbox` Upgrade (Most Common)

When bumping the `k8s-agent-sandbox` version, update **all** of these:

| File                           | What to update                                             |
| ------------------------------ | ---------------------------------------------------------- |
| `pyproject.toml`               | `k8s-agent-sandbox>=X.Y.Z` version constraint              |
| `k8s/sandbox-template.yaml`    | Container image tag (e.g., `python-runtime-sandbox:0.4.6`) |
| `scripts/kind-setup.sh`        | `AGENT_SANDBOX_VERSION` variable                           |
| `src/langchain_k8s/sandbox.py` | Adapt to SDK API changes (method renames, new parameters)  |
| `tests/conftest.py`            | Update mocks if SDK interfaces changed                     |
| `tests/unit/test_sandbox.py`   | Update test expectations for new features                  |
| `README.md`                    | Document new connection modes, parameters, behavior        |
| `AGENTS.md`                    | Update version references if needed                        |

### Example: Upgrade from 0.4.5 to 0.4.6

**Step 1: Update version constraint**

```toml
# pyproject.toml
dependencies = [
    "deepagents>=0.5.3",
    "k8s-agent-sandbox>=0.4.6",  # was >=0.4.5
]
```

**Step 2: Update Kubernetes manifests**

```bash
# k8s/sandbox-template.yaml
# Change: ghcr.io/kubernetes-sigs/agent-sandbox:python-runtime-sandbox-0.4.5
# To:     ghcr.io/kubernetes-sigs/agent-sandbox:python-runtime-sandbox-0.4.6
```

**Step 3: Update setup script**

```bash
# scripts/kind-setup.sh
# Change: AGENT_SANDBOX_VERSION="0.4.5"
# To:     AGENT_SANDBOX_VERSION="0.4.6"
```

**Step 4: Sync dependencies**

```bash
uv sync --all-groups
```

**Step 5: Check release notes for breaking changes**

Read the [kubernetes-sigs/agent-sandbox release](https://github.com/kubernetes-sigs/agent-sandbox/releases) for:

- New features (may need to expose in `KubernetesSandbox`)
- Method renames (need to update `sandbox.py` and mocks)
- Parameter changes (need to update constructor or `_ensure_sandbox()`)
- Deprecations (need to update code)

**Step 6: Update sandbox.py if needed**

Example from commit `c8dd818`:

```python
# If the SDK method signature changed:
# OLD: self._sandbox.create(template=..., namespace=...)
# NEW: self._sandbox.create(template=..., namespace=..., warmpool=...)

# In _ensure_sandbox():
handle = self._client.create_sandbox(
    template=self._template_name,
    namespace=self._namespace,
    warmpool=self._warmpool,  # NEW parameter
)
```

**Step 7: Update test mocks**

In `tests/conftest.py`:

```python
# If SandboxClient.create_sandbox() signature changed:
# OLD: mock_client.create_sandbox(template=..., namespace=...)
# NEW: mock_client.create_sandbox(template=..., namespace=..., warmpool=..., **kwargs)

# Update factory:
client = MagicMock()
client.create_sandbox = MagicMock(
    return_value=sandbox_handle,
    spec=['template', 'namespace', 'warmpool', 'labels']  # updated spec
)
```

**Step 8: Run tests**

```bash
# Unit tests (quick)
make test-unit

# If tests pass, integration tests (slow)
./scripts/kind-setup.sh
make test-integration
./scripts/kind-teardown.sh
```

**Step 9: Update documentation**

```markdown
# README.md

## Connection modes

- Production mode: `gateway_name` requires agent-sandbox >=0.4.6
- New feature: warm pools (controller v0.4.6+)
```

**Step 10: Create PR**

```
Title: feat: upgrade k8s-agent-sandbox to v0.4.6

- Updated version constraint in pyproject.toml
- Updated container image tags in k8s/sandbox-template.yaml
- Updated setup script (AGENT_SANDBOX_VERSION)
- Added support for warmpool parameter
- Updated mock SDK in tests/conftest.py
- All unit and integration tests pass

Closes: #123 (if applicable)
```

## Real-World Example: 0.4.3 to 0.4.5 Upgrade

From the repository history (commit `64be2ee`):

**Changes made:**

1. **pyproject.toml**: `k8s-agent-sandbox>=0.4.5`
2. **k8s/sandbox-template.yaml**: Image tag to 0.4.5
3. **scripts/kind-setup.sh**: AGENT_SANDBOX_VERSION to 0.4.5
4. **src/langchain_k8s/sandbox.py**: Added warmpool parameter support

   ```python
   warmpool: WarmPool | None = None

   handle = self._client.create_sandbox(
       template=self._template_name,
       namespace=self._namespace,
       warmpool=self._warmpool,  # NEW
   )
   ```

5. **tests/conftest.py**: Updated mock to support warmpool
6. **tests/unit/test_sandbox.py**: Added warmpool tests
7. **README.md**: Documented warmpool feature

**Result:** Smooth upgrade, no breaking changes.

## Upgrading `deepagents`

Less frequent than `k8s-agent-sandbox` upgrades, but may add new sandbox features.

**Steps:**

1. Update version in `pyproject.toml`
2. `uv sync --all-groups`
3. Check if new `BaseSandbox` methods exist (unlikely in minor/patch)
4. Run tests
5. If new protocol methods exist, implement in `KubernetesSandbox`

**Recent upgrades:**

- 0.5.3: Latest stable

## Upgrading Python Version Support

When Python 3.14 is released (eventually):

1. **Update pyproject.toml:**

   ```toml
   requires-python = ">=3.11,<4.0"
   classifiers = [
       "Programming Language :: Python :: 3.11",
       "Programming Language :: Python :: 3.12",
       "Programming Language :: Python :: 3.13",
       "Programming Language :: Python :: 3.14",  # ADD
   ]
   ```

2. **Update CI:**

   ```yaml
   # .github/workflows/ci.yml
   strategy:
     matrix:
       python-version: ["3.11", "3.12", "3.13", "3.14"] # ADD
   ```

3. **Test locally:** `python3.14 -m pytest tests/unit/`

4. **Drop old Python version when EOL:** Usually not until 3.11 reaches EOL (October 2027)

## Dependency Version Constraints

langchain-k8s uses **loose version constraints** to allow flexibility:

| Constraint          | Meaning                            | Example                    |
| ------------------- | ---------------------------------- | -------------------------- |
| `>=X.Y.Z`           | Minimum version, any newer allowed | `k8s-agent-sandbox>=0.4.6` |
| `>=X.Y.Z, <X+1.0.0` | Major version pinning              | `pytest>=7.3.0, <9.0.0`    |
| `~=X.Y.Z`           | Compatible release (rarely used)   |                            |

**Philosophy:**

- Allow patch/minor bumps (bug fixes, new features)
- Pin major version (prevent breaking changes)
- Trust semantic versioning (v0.X.Y still uses semantic versioning for 0.Y as major)

## Security Updates

### Using `pip-audit`

```bash
# Check for known security issues in dependencies
pip-audit

# Update vulnerable packages
uv add <package>@latest
```

### CVE Monitoring

- GitHub dependabot may alert on CVEs
- Respond quickly to critical/high severity issues
- Coordinate with maintainer team

## Troubleshooting Upgrades

### "ImportError: cannot import name 'X' from 'k8s_agent_sandbox'"

**Cause:** SDK method/class was renamed or removed.

**Fix:**

1. Check release notes for what changed
2. Update `sandbox.py` and `conftest.py` to use new name
3. Search for all uses: `grep -r "old_name" src/ tests/`

### "TypeError: create_sandbox() takes N positional arguments but M given"

**Cause:** SDK method signature changed (new required parameter).

**Fix:**

1. Check SDK documentation for new parameters
2. Update all calls to `create_sandbox()` in `sandbox.py`
3. Update mocks in `conftest.py`

### "Tests pass locally but fail in CI"

**Cause:** CI may be running on different Python version or dependencies.

**Fix:**

```bash
# Reproduce CI environment
python3.12 -m pytest tests/unit/

# Check uv.lock is committed
git add uv.lock
```

### Integration tests timeout

**Cause:** Kind cluster setup failed or controller is slow.

**Fix:**

```bash
# Check cluster is running
kubectl cluster-info

# Check controller logs
kubectl logs -n agent-sandbox-system -l app=agent-sandbox-controller

# Increase timeout
uv run pytest tests/integration/ --timeout=120
```

## When NOT to Upgrade

- **Beta/RC releases** — Wait for stable release
- **Unrelated breaking changes** — If only bug fix needed, pin to working version
- **During frozen period** — Coordinate with team before major upgrades
- **Without tests** — Always upgrade with full test coverage

## CI/CD Integration

GitHub Actions automatically:

1. Installs dependencies with `uv sync --all-groups`
2. Runs linting, type checking, unit tests
3. Publishes to PyPI on GitHub release

**If CI fails after upgrade:**

1. Check error logs
2. Fix issues locally: `make check && make test-unit`
3. Force push if needed: `git push origin branch --force-with-lease`

## Further Reading

- [Integrations: Dependencies](dependencies.md) — Dependency overview
- `AGENTS.md` — Upgrading checklist (original source)
- `pyproject.toml` — Authoritative version declarations
- Recent upgrade commits in git history
