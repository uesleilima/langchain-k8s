"""Monkey-patch for the Kubernetes Python client ``NO_PROXY`` bug.

The ``kubernetes`` Python client (v34.1.0+) has a known bug where it
ignores ``NO_PROXY`` / ``no_proxy`` environment variables: the value is
read from the environment but immediately overwritten to ``None`` by a
stray ``self.no_proxy = None`` on line 173 of ``configuration.py``.
As a result, *all* HTTP traffic is routed through the proxy even for
hosts that should be excluded.

This module applies the same fix proposed in the upstream PR: it
monkey-patches ``Configuration.__init__`` so that the proxy/no_proxy
env vars are loaded **after** the generated code runs its
(buggy) initialisers, ensuring the values are not overwritten.

The patch is idempotent and is only applied when the bug is detected.

Note: pinning ``kubernetes<=33.1.0`` is **not** a viable alternative.
That version did not support ``HTTP_PROXY`` / ``HTTPS_PROXY`` at all,
so it only appears to work because it never proxies anything.

Upstream tracking:
    Bug:  https://github.com/kubernetes-client/python/issues/2460
    Fix:  https://github.com/kubernetes-client/python/pull/2513
          https://github.com/kubernetes-client/python/pull/2459
    Affected versions: >=34.1.0 (including 35.0.0).
    Status: Fixed on master (Feb 2026, PRs #2459 + #2513), but NOT yet
            in any released version. Latest release v35.0.0 is still affected.
    Once a fixed version is released, this workaround can be removed.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PATCHED = False


def patch_k8s_proxy_config() -> None:
    """Monkey-patch ``kubernetes.client.Configuration`` to honour ``NO_PROXY``.

    The bug is that ``Configuration.__init__`` reads the proxy env vars
    into ``self.proxy`` and ``self.no_proxy``, but then a second stray
    ``self.no_proxy = None`` resets it.  The upstream fix (PR #2459 /
    #2513) simply moves the env-var loading to happen *after* the
    generated field initialisers.

    This function wraps ``__init__`` to re-apply the env var values at
    the very end, achieving the same effect.  It is idempotent and
    safe to call multiple times.

    The patch is applied at import time by :mod:`langchain_k8s.sandbox`
    and should not normally need to be called directly.

    Raises:
        No exceptions are raised.  If the ``kubernetes`` package is
        not installed or the bug is not detected, the function returns
        silently.
    """
    global _PATCHED  # noqa: PLW0603
    if _PATCHED:
        return

    try:
        from kubernetes.client.configuration import Configuration
    except ImportError:
        logger.debug("kubernetes package not installed; skipping proxy patch")
        return

    if not _is_buggy(Configuration):
        logger.debug("kubernetes.client.Configuration does not exhibit the no_proxy bug; skipping patch")
        return

    _original_init = Configuration.__init__

    def _patched_init(self: Configuration, *args: object, **kwargs: object) -> None:
        _original_init(self, *args, **kwargs)
        # Re-apply proxy env vars that the buggy generated code overwrote.
        _apply_proxy_env(self)

    Configuration.__init__ = _patched_init  # type: ignore[method-assign]
    _PATCHED = True
    logger.debug("Patched kubernetes.client.Configuration.__init__ for NO_PROXY support")


def _is_buggy(config_cls: type) -> bool:
    """Detect whether ``Configuration.__init__`` has the ``no_proxy`` bug.

    Instantiates a ``Configuration`` with ``NO_PROXY`` set to a sentinel
    value and checks whether ``no_proxy`` is ``None`` (or differs)
    afterwards.

    Args:
        config_cls: The ``kubernetes.client.Configuration`` class to
            probe.

    Returns:
        ``True`` if the bug is present and patching is needed;
        ``False`` otherwise.
    """
    sentinel = "__langchain_k8s_probe__"
    original = os.environ.get("NO_PROXY")
    try:
        os.environ["NO_PROXY"] = sentinel
        cfg = config_cls()
        return cfg.no_proxy is None or cfg.no_proxy != sentinel
    except Exception:  # noqa: BLE001
        return False
    finally:
        if original is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = original


def _apply_proxy_env(cfg: object) -> None:
    """Set ``proxy`` and ``no_proxy`` on *cfg* from environment variables.

    Mirrors the intended logic from the ``kubernetes`` client, applied
    in the correct order so that ``no_proxy`` is not clobbered.

    The following environment variables are checked (first match wins
    within each group):

    - Proxy: ``HTTPS_PROXY``, ``https_proxy``, ``HTTP_PROXY``,
      ``http_proxy``
    - No-proxy: ``NO_PROXY``, ``no_proxy``

    Args:
        cfg: A ``kubernetes.client.Configuration`` instance (or any
            object with ``proxy`` and ``no_proxy`` attributes).
    """
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var)
        if val:
            cfg.proxy = val  # type: ignore[attr-defined]
    for var in ("NO_PROXY", "no_proxy"):
        val = os.environ.get(var)
        if val:
            cfg.no_proxy = val  # type: ignore[attr-defined]
