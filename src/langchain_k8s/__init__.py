"""Kubernetes execution sandboxes for LangChain Deep Agents.

This package provides :class:`KubernetesSandbox`, a sandbox backend that
runs shell commands and performs file operations inside ephemeral or
persistent Kubernetes pods via the
`kubernetes-sigs/agent-sandbox <https://github.com/kubernetes-sigs/agent-sandbox>`_
controller.

Quick start::

    from langchain_k8s import KubernetesSandbox

    backend = KubernetesSandbox(
        template_name="python-sandbox-template",
        namespace="agent-sandbox-system",
    )
    with backend:
        result = backend.execute("echo hello")
        print(result.output)

For production use with thread-scoped sandboxes, see
:func:`create_kubernetes_sandbox`.

Exported symbols
~~~~~~~~~~~~~~~~

- :class:`KubernetesSandbox` — Main sandbox backend class.
- :func:`create_kubernetes_sandbox` — Get-or-create factory for
  thread-scoped sandboxes.
- ``__version__`` — Package version string (PEP 440).
"""

from langchain_k8s._version import __version__
from langchain_k8s.sandbox import KubernetesSandbox, create_kubernetes_sandbox

__all__ = ["KubernetesSandbox", "create_kubernetes_sandbox", "__version__"]
