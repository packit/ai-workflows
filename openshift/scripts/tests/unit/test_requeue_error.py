import json

import pytest

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
        "requeued_from_error_list": True,
    }
    restored = Task.model_validate_json(task_json)
    assert restored.requeued_from_error_list is True
    assert restored.user_triggered is False


def test_prepare_requeue_task_uses_priority_queue_when_requested() -> None:
    task = {"attempts": 2, "user_triggered": False}

    queue, task_json = prepare_requeue_task(task, "backport_queue_c10s", user_triggered=True)

    assert queue == "backport_queue_c10s_todo"
    assert json.loads(task_json) == {
        "attempts": 0,
        "user_triggered": True,
        "requeued_from_error_list": True,
    }
