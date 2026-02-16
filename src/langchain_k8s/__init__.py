"""Kubernetes Agent Sandbox integration for LangChain Deep Agents."""

from langchain_k8s._version import __version__
from langchain_k8s.sandbox import KubernetesSandbox

__all__ = ["KubernetesSandbox", "__version__"]
