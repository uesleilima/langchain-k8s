"""Smoke tests for package imports."""


def test_import_package() -> None:
    import langchain_k8s

    assert langchain_k8s is not None


def test_import_kubernetes_sandbox() -> None:
    from langchain_k8s import KubernetesSandbox

    assert KubernetesSandbox is not None


def test_import_version() -> None:
    from langchain_k8s import __version__

    assert isinstance(__version__, str)
    assert __version__
