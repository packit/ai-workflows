"""Unit tests for delayed Redis ZSET scheduling helpers."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from ymir.common.delayed_queue import promote_due_tasks, schedule_task
from ymir.common.models import Task


@pytest.mark.asyncio
async def test_schedule_task_zadds_with_future_score():
    redis = MagicMock()
    redis.zadd = AsyncMock(return_value=1)

    before = time.time()
    await schedule_task(redis, "reproducer_queue_delayed", '{"attempts":0}', delay_seconds=1800)
    after = time.time()

    redis.zadd.assert_awaited_once()
    key, mapping = redis.zadd.await_args.args
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

    redis = MagicMock()
    # Only return due members (helper uses zrangebyscore with max=now)
    redis.zrangebyscore = AsyncMock(return_value=[todo_payload.encode(), normal_payload.encode()])
    pipe = MagicMock()
    pipe.lpush = MagicMock()
    pipe.zrem = MagicMock()
    pipe.execute = AsyncMock(return_value=[1, 1])
    redis.pipeline = MagicMock(return_value=pipe)

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
    redis.zrangebyscore.assert_awaited_once()
    assert pipe.lpush.call_count == 2
    assert pipe.lpush.call_args_list[0].args == ("reproducer_queue_todo", todo_payload)
    assert pipe.lpush.call_args_list[1].args == ("reproducer_queue", normal_payload)
    # future_payload was not returned by zrangebyscore — stays delayed
    assert all(future_payload not in str(c) for c in pipe.lpush.call_args_list)


@pytest.mark.asyncio
async def test_promote_due_tasks_noop_when_empty():
    redis = MagicMock()
    redis.zrangebyscore = AsyncMock(return_value=[])
    redis.pipeline = MagicMock()

    promoted = await promote_due_tasks(redis, "reproducer_queue_delayed", lambda _: "reproducer_queue")
    assert promoted == 0
    redis.pipeline.assert_not_called()
