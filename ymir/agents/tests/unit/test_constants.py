import pytest

from ymir.agents import constants
from ymir.agents.constants import mr_description_footer, trace_viewer_issue_url


def test_trace_viewer_issue_url_uses_configured_viewer(monkeypatch):
    monkeypatch.setenv("TRACE_VIEWER_URL", "https://trace.example/")

    assert trace_viewer_issue_url("RHEL/1") == ("https://trace.example/#/issues/RHEL%2F1")


def test_trace_viewer_issue_url_returns_none_without_configuration(monkeypatch):
    monkeypatch.delenv("TRACE_VIEWER_URL", raising=False)

    assert trace_viewer_issue_url("RHEL-1") is None


def test_trace_viewer_issue_url_returns_none_for_empty_configuration(monkeypatch):
    monkeypatch.setenv("TRACE_VIEWER_URL", "")

    assert trace_viewer_issue_url("RHEL-1") is None


def test_mr_footer_links_to_all_issue_traces(monkeypatch):
    monkeypatch.setenv("TRACE_VIEWER_URL", "https://trace.example/")

    footer = mr_description_footer("bash", "RHEL/1")

    assert footer == (
        "## Execution traces\n\n"
        "- [View all traces for RHEL/1](https://trace.example/#/issues/RHEL%2F1)\n\n"
        + mr_description_footer("bash")
    )


def test_mr_footer_deduplicates_issues_in_order(monkeypatch):
    monkeypatch.setenv("TRACE_VIEWER_URL", "https://trace.example")

    footer = mr_description_footer("bash", ["RHEL-2", "", "RHEL-1", "RHEL-2"])

    assert footer.startswith(
        "## Execution traces\n\n"
        "- [View all traces for RHEL-2](https://trace.example/#/issues/RHEL-2)\n"
        "- [View all traces for RHEL-1](https://trace.example/#/issues/RHEL-1)\n\n---\n"
    )
    assert footer.count("View all traces for RHEL-2") == 1


@pytest.mark.parametrize("issues", [None, "", [], [""]])
def test_mr_footer_omits_traces_without_issues(monkeypatch, issues):
    monkeypatch.setenv("TRACE_VIEWER_URL", "https://trace.example")

    assert mr_description_footer("bash", issues) == mr_description_footer("bash")
    assert "Execution traces" not in mr_description_footer("bash", issues)


@pytest.mark.parametrize("viewer_url", [None, ""])
def test_mr_footer_omits_traces_without_viewer(monkeypatch, viewer_url):
    if viewer_url is None:
        monkeypatch.delenv("TRACE_VIEWER_URL", raising=False)
    else:
        monkeypatch.setenv("TRACE_VIEWER_URL", viewer_url)

    assert mr_description_footer("bash", "RHEL-1") == mr_description_footer("bash")
    assert "Execution traces" not in mr_description_footer("bash", "RHEL-1")


@pytest.mark.parametrize("viewer_url", ["https://trace.example", "https://trace.example/"])
def test_trace_viewer_run_url_needs_no_issue(monkeypatch, viewer_url):
    monkeypatch.setenv("TRACE_VIEWER_URL", viewer_url)
    trace_id = "a" * 32

    assert constants.trace_viewer_run_url(trace_id) == f"https://trace.example/#/trace/_/{trace_id}"


@pytest.mark.parametrize("viewer_url", [None, ""])
def test_trace_viewer_run_url_without_configuration(monkeypatch, viewer_url):
    if viewer_url is None:
        monkeypatch.delenv("TRACE_VIEWER_URL", raising=False)
    else:
        monkeypatch.setenv("TRACE_VIEWER_URL", viewer_url)

    assert constants.trace_viewer_run_url("a" * 32) is None


def test_mr_footer_places_run_trace_before_standard_footer(monkeypatch):
    monkeypatch.setenv("TRACE_VIEWER_URL", "https://trace.example")
    url = "https://trace.example/#/trace/_/" + "a" * 32

    assert mr_description_footer("bash", trace_url=url) == (
        f"## Execution traces\n\n- [View this run's trace]({url})\n\n" + mr_description_footer("bash")
    )
