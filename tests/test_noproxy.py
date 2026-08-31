import importlib
import os


def test_local_hosts_forced_into_no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "example.com")
    monkeypatch.setenv("no_proxy", "")
    import reviewhive

    importlib.reload(reviewhive)
    assert "127.0.0.1" in os.environ["NO_PROXY"]
    assert "localhost" in os.environ["NO_PROXY"]
    assert "example.com" in os.environ["NO_PROXY"]
    assert "127.0.0.1" in os.environ["no_proxy"]
