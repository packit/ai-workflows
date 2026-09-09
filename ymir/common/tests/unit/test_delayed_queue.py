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
    future_payload = Task(
        metadata={"jira_issue": "RHEL-3"},
        attempts=1,
        user_triggered=False,
    ).model_dump_json()

    # future_payload was not returned by zrangebyscore — stays delayed
    async def _mock_zrangebyscore(*_args, **_kwargs):
        return [todo_payload.encode(), normal_payload.encode()]

    async def _mock_execute(*_args, **_kwargs):
        return [1, 1]

    pipe = flexmock()
    pipe.should_receive("lpush").with_args("reproducer_queue_todo", todo_payload).once().ordered()
    pipe.should_receive("lpush").with_args("reproducer_queue", normal_payload).once().ordered()
    pipe.should_receive("lpush").with_args("reproducer_queue", future_payload).never()
    pipe.should_receive("zrem")
    pipe.should_receive("execute").replace_with(_mock_execute)

    redis = flexmock()
    redis.should_receive("zrangebyscore").replace_with(_mock_zrangebyscore).once()
    redis.should_receive("pipeline").and_return(pipe)

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
    redis.should_receive("pipeline").never()

    promoted = await promote_due_tasks(redis, "reproducer_queue_delayed", lambda _: "reproducer_queue")
    assert promoted == 0
