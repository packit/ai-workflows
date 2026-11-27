"""Routing between the current and sustaining queue families.

The sustaining bucket is a deployment-level property (``SUSTAINING=true``),
so every queue selection must derive from that flag and never from the issue.
These tests pin the selection for both buckets, both branch families and the
``_todo`` priority twins.
"""

import pytest

from ymir.common.constants import RedisQueues


@pytest.fixture(autouse=True)
def _clear_sustaining(monkeypatch):
    monkeypatch.delenv("SUSTAINING", raising=False)


@pytest.mark.parametrize(
    ("getter", "bucket", "branch", "expected"),
    [
        ("get_rebase_queue_for_branch", False, "rhel-9.8.0", RedisQueues.REBASE_QUEUE_C9S.value),
        ("get_rebase_queue_for_branch", False, "rhel-10.2.0", RedisQueues.REBASE_QUEUE_C10S.value),
        (
            "get_rebase_queue_for_branch",
            True,
            "rhel-9.8.0",
            RedisQueues.REBASE_QUEUE_C9S_SUSTAINING.value,
        ),
        (
            "get_rebase_queue_for_branch",
            True,
            "rhel-10.2.0",
            RedisQueues.REBASE_QUEUE_C10S_SUSTAINING.value,
        ),
        ("get_backport_queue_for_branch", False, "rhel-8.10.0", RedisQueues.BACKPORT_QUEUE_C9S.value),
        (
            "get_backport_queue_for_branch",
            True,
            "rhel-8.10.0",
            RedisQueues.BACKPORT_QUEUE_C9S_SUSTAINING.value,
        ),
        ("get_rebuild_queue_for_branch", False, "c10s", RedisQueues.REBUILD_QUEUE_C10S.value),
        (
            "get_rebuild_queue_for_branch",
            True,
            "c10s",
            RedisQueues.REBUILD_QUEUE_C10S_SUSTAINING.value,
        ),
    ],
)
def test_branch_getters_select_bucket(getter, bucket, branch, expected):
    assert getattr(RedisQueues, getter)(branch, sustaining=bucket) == expected


@pytest.mark.parametrize(
    "getter",
    ["get_rebase_queue_for_branch", "get_backport_queue_for_branch", "get_rebuild_queue_for_branch"],
)
def test_user_triggered_tasks_keep_the_bucket_on_the_priority_twin(getter):
    current = getattr(RedisQueues, getter)("rhel-9.8.0", user_triggered=True, sustaining=False)
    sustaining = getattr(RedisQueues, getter)("rhel-9.8.0", user_triggered=True, sustaining=True)

    assert current.endswith("_todo")
    assert sustaining.endswith("_todo")
    assert "sustaining" not in current
    assert "sustaining" in sustaining


@pytest.mark.parametrize(
    ("getter", "container_version", "expected"),
    [
        ("get_backport_queue_for_container", "c9s", RedisQueues.BACKPORT_QUEUE_C9S.value),
        ("get_backport_queue_for_container", "c10s", RedisQueues.BACKPORT_QUEUE_C10S.value),
        ("get_rebase_queue_for_container", "c9s", RedisQueues.REBASE_QUEUE_C9S.value),
        ("get_rebuild_queue_for_container", "c10s", RedisQueues.REBUILD_QUEUE_C10S.value),
        (
            "get_rebuild_queue_for_container",
            "c9s",
            RedisQueues.REBUILD_QUEUE_C9S_SUSTAINING.value,
        ),
    ],
)
def test_container_getters_select_bucket(getter, container_version, expected):
    sustaining = "sustaining" in expected
    assert getattr(RedisQueues, getter)(container_version, sustaining=sustaining) == expected


def test_triage_queue_helper_selects_bucket_and_priority_twin():
    assert RedisQueues.triage_queue(sustaining=False) == RedisQueues.TRIAGE_QUEUE.value
    assert (
        RedisQueues.triage_queue(user_triggered=True, sustaining=False) == RedisQueues.TRIAGE_QUEUE_TODO.value
    )
    assert RedisQueues.triage_queue(sustaining=True) == RedisQueues.TRIAGE_QUEUE_SUSTAINING.value
    assert (
        RedisQueues.triage_queue(user_triggered=True, sustaining=True)
        == RedisQueues.TRIAGE_QUEUE_SUSTAINING_TODO.value
    )


def test_default_bucket_comes_from_the_sustaining_env_var(monkeypatch):
    monkeypatch.setenv("SUSTAINING", "true")

    assert RedisQueues.triage_queue() == RedisQueues.TRIAGE_QUEUE_SUSTAINING.value
    assert (
        RedisQueues.get_backport_queue_for_branch("rhel-9.8.0")
        == RedisQueues.BACKPORT_QUEUE_C9S_SUSTAINING.value
    )
    # The flag is read case-insensitively, like the other boolean env vars.
    monkeypatch.setenv("SUSTAINING", "TRUE")
    assert RedisQueues.get_rebuild_queue_for_container("c9s") == (
        RedisQueues.REBUILD_QUEUE_C9S_SUSTAINING.value
    )


def test_sustaining_priority_twins_are_declared_queues():
    """priority_twin() builds names by string suffix; they must exist as queues.

    A drift here would make agents BRPOP on a list the fetcher never feeds.
    """
    declared = RedisQueues.all_queues()
    for queue in declared:
        if queue.endswith("_sustaining"):
            assert RedisQueues.priority_twin(queue) in declared
