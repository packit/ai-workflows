import asyncio
from contextlib import asynccontextmanager
from enum import StrEnum
import time
from typing import AsyncGenerator, Iterable, cast
from pydantic import BaseModel
import redis.asyncio as redis

from common.utils import redis_client, fix_await


class WorkItemType(StrEnum):
    PROCESS_ISSUE = "process_issue"
    PROCESS_ERRATUM = "process_erratum"


class WorkItem(BaseModel, frozen=True):
    item_type: WorkItemType
    item_data: str

    def __str__(self):
        return f"{self.item_type}:{self.item_data}"

    @staticmethod
    def from_str(item_str: str) -> "WorkItem":
        item_type, item_data = item_str.split(":", 1)
        return WorkItem(item_type=WorkItemType(item_type), item_data=item_data)


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
WORK_ITEM_RETRY_DELAY = 15 * 60  # 15 minutes in seconds


class WorkQueue:
    def __init__(self, client: redis.Redis):
        self.client = client
        self.first_ready_script = client.register_script(FIRST_READY_SCRIPT)

    async def pop_first_ready_work_item(self) -> WorkItem | None:
        current_time = time.time()
        result = cast(
            None | tuple[bytes, float],
            await fix_await(
                self.first_ready_script(
                    keys=["supervisor_work_queue"],
                    args=[current_time, current_time + WORK_ITEM_RETRY_DELAY],
                )
            ),
        )
        if result is None:
            return None

        return WorkItem.from_str(result[0].decode())

    async def wait_first_ready_work_item(self) -> WorkItem:
        while True:
            work_item = await self.pop_first_ready_work_item()
            if work_item is not None:
                return work_item
            await asyncio.sleep(POLLING_INTERVAL)

    async def schedule_work_items(
        self, work_items: Iterable[WorkItem], delay: float = 0.0
    ) -> None:
        new_time = time.time() + delay
        to_add = {str(item): new_time for item in work_items}
        if len(to_add) == 0:
            return

        await self.client.zadd("supervisor_work_queue", to_add)

    async def remove_work_items(self, work_items: Iterable[WorkItem]) -> None:
        to_remove = [str(item) for item in work_items]
        if len(to_remove) == 0:
            return

        await self.client.zrem("supervisor_work_queue", *to_remove)

    async def remove_all_work_items(self) -> None:
        await self.client.delete("supervisor_work_queue")

    async def get_all_work_items(self) -> list[WorkItem]:
        work_items = await self.client.zrange("supervisor_work_queue", 0, -1)
        return [
            WorkItem.from_str(str(item_bytes.decode())) for item_bytes in work_items
        ]


@asynccontextmanager
async def work_queue(redis_url: str) -> AsyncGenerator[WorkQueue, None]:
    async with redis_client(redis_url) as client:
        yield WorkQueue(client)
