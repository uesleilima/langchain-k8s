"""Integration test configuration — auto-skip when no Kind cluster is available."""

from __future__ import annotations

import subprocess

import pytest

CLUSTER_NAME = "langchain-k8s"


def _kind_cluster_exists() -> bool:
    """Check if the expected Kind cluster is running."""
    try:
        result = subprocess.run(
            ["kind", "get", "clusters"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return CLUSTER_NAME in result.stdout.splitlines()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip integration tests when no Kind cluster is available."""
    if _kind_cluster_exists():
        return
    skip_marker = pytest.mark.skip(
        reason=f"Kind cluster '{CLUSTER_NAME}' not found. Run ./scripts/kind-setup.sh first."
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)
