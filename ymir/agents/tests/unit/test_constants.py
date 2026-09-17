from ymir.agents.constants import trace_viewer_issue_url


def test_trace_viewer_issue_url_uses_configured_viewer(monkeypatch):
    monkeypatch.setenv("TRACE_VIEWER_URL", "https://trace.example/")

    assert trace_viewer_issue_url("RHEL/1") == ("https://trace.example/#/issues/RHEL%2F1")


def test_trace_viewer_issue_url_returns_none_without_configuration(monkeypatch):
    monkeypatch.delenv("TRACE_VIEWER_URL", raising=False)

    assert trace_viewer_issue_url("RHEL-1") is None


def test_trace_viewer_issue_url_returns_none_for_empty_configuration(monkeypatch):
    monkeypatch.setenv("TRACE_VIEWER_URL", "")

    assert trace_viewer_issue_url("RHEL-1") is None
