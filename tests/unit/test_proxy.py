"""Unit tests for the kubernetes proxy monkey-patch."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import langchain_k8s.proxy as proxy_mod
from langchain_k8s.proxy import _apply_proxy_env, _is_buggy, patch_k8s_proxy_config


# ---------------------------------------------------------------------------
# Fake Configuration that reproduces the upstream bug
# ---------------------------------------------------------------------------


class _BuggyConfiguration:
    """Mimics kubernetes.client.Configuration with the no_proxy bug."""

    def __init__(self, **_kwargs: object) -> None:
        self.proxy = None
        # Load proxy from environment variables (if set)
        if os.getenv("HTTPS_PROXY"):
            self.proxy = os.getenv("HTTPS_PROXY")
        if os.getenv("https_proxy"):
            self.proxy = os.getenv("https_proxy")
        if os.getenv("HTTP_PROXY"):
            self.proxy = os.getenv("HTTP_PROXY")
        if os.getenv("http_proxy"):
            self.proxy = os.getenv("http_proxy")
        self.no_proxy = None
        # Load no_proxy from environment variables (if set)
        if os.getenv("NO_PROXY"):
            self.no_proxy = os.getenv("NO_PROXY")
        if os.getenv("no_proxy"):
            self.no_proxy = os.getenv("no_proxy")
        # BUG: stray reset clobbers the value just loaded
        self.no_proxy = None


class _FixedConfiguration:
    """Mimics a future fixed Configuration (no bug)."""

    def __init__(self, **_kwargs: object) -> None:
        self.proxy = None
        self.no_proxy = None
        if os.getenv("HTTPS_PROXY"):
            self.proxy = os.getenv("HTTPS_PROXY")
        if os.getenv("https_proxy"):
            self.proxy = os.getenv("https_proxy")
        if os.getenv("HTTP_PROXY"):
            self.proxy = os.getenv("HTTP_PROXY")
        if os.getenv("http_proxy"):
            self.proxy = os.getenv("http_proxy")
        if os.getenv("NO_PROXY"):
            self.no_proxy = os.getenv("NO_PROXY")
        if os.getenv("no_proxy"):
            self.no_proxy = os.getenv("no_proxy")


# ---------------------------------------------------------------------------
# _is_buggy
# ---------------------------------------------------------------------------


class TestIsBuggy:
    def test_detects_buggy_configuration(self) -> None:
        assert _is_buggy(_BuggyConfiguration)

    def test_detects_fixed_configuration(self) -> None:
        assert not _is_buggy(_FixedConfiguration)


# ---------------------------------------------------------------------------
# _apply_proxy_env
# ---------------------------------------------------------------------------


class TestApplyProxyEnv:
    def test_sets_proxy_and_no_proxy(self) -> None:
        env = {
            "HTTP_PROXY": "http://proxy:8888",
            "NO_PROXY": "127.0.0.1,localhost",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = MagicMock()
            cfg.proxy = None
            cfg.no_proxy = None
            _apply_proxy_env(cfg)
            assert cfg.proxy == "http://proxy:8888"
            assert cfg.no_proxy == "127.0.0.1,localhost"

    def test_noop_without_env_vars(self) -> None:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                   "NO_PROXY", "no_proxy"):
            os.environ.pop(k, None)
        cfg = MagicMock()
        cfg.proxy = None
        cfg.no_proxy = None
        _apply_proxy_env(cfg)
        assert cfg.proxy is None
        assert cfg.no_proxy is None

    def test_prefers_http_proxy_over_https_proxy(self) -> None:
        """HTTP_PROXY takes precedence (loaded last)."""
        env = {
            "HTTPS_PROXY": "http://https-proxy:8888",
            "HTTP_PROXY": "http://http-proxy:8888",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = MagicMock()
            cfg.proxy = None
            _apply_proxy_env(cfg)
            assert cfg.proxy == "http://http-proxy:8888"


# ---------------------------------------------------------------------------
# patch_k8s_proxy_config
# ---------------------------------------------------------------------------


class TestPatchK8sProxyConfig:
    def setup_method(self) -> None:
        # Reset the global _PATCHED flag before each test
        proxy_mod._PATCHED = False

    def test_patches_buggy_configuration(self) -> None:
        env = {
            "HTTP_PROXY": "http://proxy:8888",
            "NO_PROXY": "127.0.0.1,localhost",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "langchain_k8s.proxy.Configuration",
                _BuggyConfiguration,
                create=True,
            ):
                # Redirect the import inside patch_k8s_proxy_config
                with patch.dict(
                    "sys.modules",
                    {"kubernetes.client.configuration": MagicMock(
                        Configuration=_BuggyConfiguration
                    )},
                ):
                    patch_k8s_proxy_config()

                    # After patching, creating a _BuggyConfiguration should
                    # now retain the NO_PROXY value
                    cfg = _BuggyConfiguration()
                    assert cfg.no_proxy == "127.0.0.1,localhost"
                    assert cfg.proxy == "http://proxy:8888"

    def test_idempotent(self) -> None:
        """Calling patch_k8s_proxy_config twice does not double-wrap."""
        with patch.dict(
            "sys.modules",
            {"kubernetes.client.configuration": MagicMock(
                Configuration=_BuggyConfiguration
            )},
        ):
            patch_k8s_proxy_config()
            first_init = _BuggyConfiguration.__init__

            patch_k8s_proxy_config()
            second_init = _BuggyConfiguration.__init__

            assert first_init is second_init

    def test_skips_when_not_buggy(self) -> None:
        """No patch applied if Configuration is already fixed."""
        original_init = _FixedConfiguration.__init__
        with patch.dict(
            "sys.modules",
            {"kubernetes.client.configuration": MagicMock(
                Configuration=_FixedConfiguration
            )},
        ):
            patch_k8s_proxy_config()
            assert _FixedConfiguration.__init__ is original_init

    def test_skips_when_kubernetes_not_installed(self) -> None:
        """No crash if kubernetes package is absent."""
        with patch.dict("sys.modules", {"kubernetes.client.configuration": None}):
            patch_k8s_proxy_config()  # should not raise
