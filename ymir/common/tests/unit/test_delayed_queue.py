"""Unit tests for delayed Redis ZSET scheduling helpers."""

import time

import pytest
from flexmock import flexmock

from ymir.common.delayed_queue import promote_due_tasks, schedule_task
from ymir.common.models import Task


@pytest.mark.asyncio
async def test_schedule_task_zadds_with_future_score():
    calls = []

    async def _spy_zadd(key, mapping):
        calls.append((key, mapping))
        return 1

    redis = flexmock()
    redis.should_receive("zadd").replace_with(_spy_zadd).once()

    before = time.time()
    await schedule_task(redis, "reproducer_queue_delayed", '{"attempts":0}', delay_seconds=1800)
    after = time.time()

    key, mapping = calls[0]
    assert key == "reproducer_queue_delayed"
    score = mapping['{"attempts":0}']
    assert before + 1800 <= score <= after + 1800


@pytest.mark.asyncio
async def test_promote_due_tasks_moves_ready_members():
    todo_payload = Task(
        metadata={"jira_issue": "RHEL-1"},
        attempts=1,
        user_triggered=True,
    ).model_dump_json()
    normal_payload = Task(
        metadata={"jira_issue": "RHEL-2"},
        attempts=1,
        user_triggered=False,
    ).model_dump_json()

    # Future payloads are excluded by the Redis score cutoff.
    async def _mock_zrangebyscore(*_args, **_kwargs):
        return [todo_payload.encode(), normal_payload.encode()]

    redis = flexmock()
    redis.should_receive("zrangebyscore").replace_with(_mock_zrangebyscore).once()
    redis.should_receive("eval").with_args(
        str,
        2,
        "reproducer_queue_delayed",
        "reproducer_queue_todo",
        todo_payload.encode(),
        float,
    ).and_return(1).once()
    redis.should_receive("eval").with_args(
        str,
        2,
        "reproducer_queue_delayed",
        "reproducer_queue",
        normal_payload.encode(),
        float,
    ).and_return(1).once()

    def target(payload: str) -> str:
        task = Task.model_validate_json(payload)
        return "reproducer_queue_todo" if task.user_triggered else "reproducer_queue"

    promoted = await promote_due_tasks(
        redis,
        "reproducer_queue_delayed",
        target,
        now=time.time(),
    )

    assert promoted == 2


@pytest.mark.asyncio
async def test_promote_due_tasks_noop_when_empty():
    async def _mock_zrangebyscore(*_args, **_kwargs):
        return []

    redis = flexmock()
    redis.should_receive("zrangebyscore").replace_with(_mock_zrangebyscore).once()
    redis.should_receive("eval").never()

    promoted = await promote_due_tasks(redis, "reproducer_queue_delayed", lambda _: "reproducer_queue")
    assert promoted == 0


@pytest.mark.asyncio
async def test_promote_due_tasks_does_not_duplicate_claimed_member():
    redis = flexmock()
    redis.should_receive("zrangebyscore").and_return([b"task"]).once()
    # Another worker already removed this member before this worker's claim.
    redis.should_receive("eval").with_args(str, 2, "delayed", "ready", b"task", 100).and_return(0).once()
    assert await promote_due_tasks(redis, "delayed", lambda _: "ready", now=100) == 0
