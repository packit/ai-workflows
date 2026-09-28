import json
import subprocess
import sys

import pytest
import requests
from flexmock import flexmock

from openshift.scripts import requeue_error
from openshift.scripts.requeue_error import prepare_requeue_task
from ymir.common.models import Task


@pytest.mark.parametrize("source_queue", ["backport_queue_c9s", "backport_queue_c9s_todo"])
def test_prepare_requeue_task_defaults_to_priority_queue_without_maintainer_feedback(source_queue) -> None:
    task = {"metadata": {"issue": "RHEL-1"}, "attempts": 3, "user_triggered": True}

    queue, task_json = prepare_requeue_task(task, source_queue, user_triggered=False)

    assert queue == "backport_queue_c9s_todo"
    assert json.loads(task_json) == {
        "metadata": {"issue": "RHEL-1"},
        "attempts": 0,
        "user_triggered": False,
    }
    restored = Task.model_validate_json(task_json)
    assert restored.user_triggered is False


def test_prepare_requeue_task_uses_priority_queue_when_requested() -> None:
    task = {"attempts": 2, "user_triggered": False}

    queue, task_json = prepare_requeue_task(task, "backport_queue_c10s", user_triggered=True)

    assert queue == "backport_queue_c10s_todo"
    assert json.loads(task_json) == {
        "attempts": 0,
        "user_triggered": True,
    }


@pytest.mark.parametrize(
    "labels", [[], ["maintainer"], ["ymir_triage_errored", "maintainer", "ymir_future_label"]]
)
def test_remote_cleanup_removes_all_ymir_labels_only(monkeypatch, labels) -> None:
    monkeypatch.setenv("JIRA_URL", "https://jira.example/")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "test-token")
    monkeypatch.setattr(sys, "argv", ["-", "RHEL-1"])
    response = flexmock()
    response.should_receive("raise_for_status").once()
    response.should_receive("json").and_return({"fields": {"labels": labels}})
    flexmock(requests).should_receive("get").with_args(
        "https://jira.example/rest/api/3/issue/RHEL-1",
        params={"fields": "labels"},
        headers=dict,
        timeout=30,
    ).and_return(response).once()
    removals = [{"remove": label} for label in labels if label.startswith("ymir_")]
    if removals:
        updated = flexmock()
        updated.should_receive("raise_for_status").once()
        flexmock(requests).should_receive("put").with_args(
            "https://jira.example/rest/api/3/issue/RHEL-1",
            json={"update": {"labels": removals}},
            headers=dict,
            timeout=30,
        ).and_return(updated).once()
    else:
        flexmock(requests).should_receive("put").never()

    exec(requeue_error._CLEAR_YMIR_LABELS_PYTHON, {})  # noqa: S102 - exercise the shipped gateway script


@pytest.mark.parametrize("failure", ["fetch", "update"])
def test_remote_cleanup_propagates_jira_failure(monkeypatch, failure) -> None:
    monkeypatch.setenv("JIRA_URL", "https://jira.example")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "test-token")
    monkeypatch.setattr(sys, "argv", ["-", "RHEL-1"])
    fetched = flexmock()
    flexmock(requests).should_receive("get").and_return(fetched)
    if failure == "fetch":
        fetched.should_receive("raise_for_status").and_raise(requests.HTTPError)
        flexmock(requests).should_receive("put").never()
    else:
        fetched.should_receive("raise_for_status")
        fetched.should_receive("json").and_return({"fields": {"labels": ["ymir_triage_errored"]}})
        updated = flexmock()
        flexmock(requests).should_receive("put").and_return(updated)
        updated.should_receive("raise_for_status").and_raise(requests.HTTPError)

    with pytest.raises(requests.HTTPError):
        exec(requeue_error._CLEAR_YMIR_LABELS_PYTHON, {})  # noqa: S102 - exercise the shipped gateway script


@pytest.mark.parametrize("failure", [None, "exit", "timeout", "os"])
def test_clear_ymir_labels_runs_in_gateway(failure) -> None:
    call = (
        flexmock(subprocess)
        .should_receive("run")
        .with_args(
            ["oc", "exec", "-i", "deployment/mcp-gateway", "--", "python3", "-", "RHEL-1"],
            input=requeue_error._CLEAR_YMIR_LABELS_PYTHON.encode(),
            capture_output=True,
            timeout=90,
        )
        .once()
    )
    if failure == "timeout":
        call.and_raise(subprocess.TimeoutExpired("oc", 90))
    elif failure == "os":
        call.and_raise(OSError("oc unavailable"))
    else:
        call.and_return(subprocess.CompletedProcess([], 1 if failure else 0, b"", b""))
    if failure:
        with pytest.raises(SystemExit, match="labels"):
            requeue_error.clear_ymir_labels("RHEL-1", "mcp-gateway")
    else:
        requeue_error.clear_ymir_labels("RHEL-1", "mcp-gateway")


@pytest.mark.parametrize("mode", ["success", "dry-run", "cleanup-failed", "missing-issue"])
def test_main_clears_labels_before_requeue(monkeypatch, capsys, mode) -> None:
    monkeypatch.setattr(
        sys, "argv", ["requeue_error.py", "42"] + (["--dry-run"] if mode == "dry-run" else [])
    )
    entry = {"queue": "triage_queue", "task": {"metadata": {"issue": "RHEL-1"}, "attempts": 3}}
    if mode == "missing-issue":
        entry["task"]["metadata"] = {}
    flexmock(requeue_error).should_receive("fetch_via_oc").and_return("blob")
    flexmock(requeue_error).should_receive("find_entry").and_return(("raw", entry))
    cleanup = flexmock(requeue_error).should_receive("clear_ymir_labels")
    enqueue = flexmock(requeue_error).should_receive("atomic_requeue")
    if mode in ("dry-run", "missing-issue"):
        cleanup.never()
        enqueue.never()
    elif mode == "cleanup-failed":
        cleanup.with_args("RHEL-1", "mcp-gateway").and_raise(SystemExit, "cleanup failed").once()
        enqueue.never()
    else:
        cleanup.with_args("RHEL-1", "mcp-gateway").once().ordered()
        enqueue.with_args("valkey", "error_list", "triage_queue_todo", "raw", str, 42).and_return(
            1
        ).once().ordered()
    if mode in ("cleanup-failed", "missing-issue"):
        with pytest.raises(SystemExit):
            requeue_error.main()
    else:
        requeue_error.main()
        if mode == "dry-run":
            assert "Would remove all ymir_* labels from RHEL-1" in capsys.readouterr().out
