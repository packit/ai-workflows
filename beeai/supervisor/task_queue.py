import asyncio
from contextlib import asynccontextmanager
from enum import StrEnum
import time
from typing import AsyncGenerator, Iterable, cast
from pydantic import BaseModel
import redis.asyncio as redis

from agents.utils import redis_client, fix_await


class TaskType(StrEnum):
    PROCESS_ISSUE = "process_issue"
    PROCESS_ERRATUM = "process_errata"


class Task(BaseModel, frozen=True):
    task_type: TaskType
    task_data: str

    def __str__(self):
        return f"{self.task_type}:{self.task_data}"

    @staticmethod
    def from_str(task_str: str) -> "Task":
        task_type, task_data = task_str.split(":", 1)
        return Task(task_type=TaskType(task_type), task_data=task_data)


FIRST_READY_SCRIPT = """
local key = KEYS[1]
local maxScore = tonumber(ARGV[1])
local newScore = tonumber(ARGV[2])

local res = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")

if #res == 0 then
    return nil
end

local member = res[1]
local score = tonumber(res[2])

if score <= maxScore then
    redis.call("ZADD", key, newScore, member)
    return {member, score}
else
    return nil
end
"""

POLLING_INTERVAL = 1 * 60  # 1 minute
TASK_RETRY_DELAY = 15 * 60  # 15 minutes in seconds


class TaskQueue:
    def __init__(self, client: redis.Redis):
        self.client = client
        self.first_ready_script = client.register_script(FIRST_READY_SCRIPT)

    async def pop_first_ready_task(self) -> Task | None:
        current_time = time.time()
        result = cast(
            None | tuple[bytes, float],
            await fix_await(
                self.first_ready_script(
                    keys=["task_queue"],
                    args=[current_time, current_time + TASK_RETRY_DELAY],
                )
            ),
        )
        if result is None:
            return None

        return Task.from_str(result[0].decode())

    async def wait_first_ready_task(self) -> Task:
        while True:
            task = await self.pop_first_ready_task()
            if task is not None:
                return task
            await asyncio.sleep(POLLING_INTERVAL)

    async def schedule_tasks(self, tasks: Iterable[Task], delay: float = 0.0) -> None:
        new_time = time.time() + delay
        to_add = {str(task): new_time for task in tasks}
        if len(to_add) == 0:
            return

        await self.client.zadd("task_queue", to_add)

    async def remove_tasks(self, tasks: Iterable[Task]) -> None:
        to_remove = [str(task) for task in tasks]
        if len(to_remove) == 0:
            return

        await self.client.zrem("task_queue", *to_remove)

    async def get_all_tasks(self) -> list[Task]:
        tasks = await self.client.zrange("task_queue", 0, -1)
        return [Task.from_str(str(task_bytes.decode())) for task_bytes in tasks]


@asynccontextmanager
async def task_queue(redis_url: str) -> AsyncGenerator[TaskQueue, None]:
    async with redis_client(redis_url) as client:
        yield TaskQueue(client)
