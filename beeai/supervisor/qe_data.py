import asyncio
from collections.abc import Callable
from functools import wraps
from typing import Awaitable, Coroutine, TypeVar
from pydantic import BaseModel

from .http_utils import aiohttp_session

QE_DATA_REPO = "https://gitlab.cee.redhat.com/otaylor/jotnar-qe-data"
QE_DATA_URL = (
    f"{QE_DATA_REPO}/-/raw/main/jotnar_qe_data.json?ref_type=heads&inline=false"
)


class TestLocationInfo(BaseModel):
    component: str
    qa_contact: str
    tests_location: str | None = None
    test_config_location: str | None = None
    test_trigger_method: str | None = None
    test_result_location: str | None = None
    test_docs_url: str | None = None
    notes: str | None = None


R = TypeVar("R")


def cache_async(
    *,
    max_age: float | None,
) -> Callable[[Callable[[], Coroutine[None, None, R]]], Callable[[], Awaitable[R]]]:
    """
    Decorator to cache the result of an async function for a specified duration.
    (For simplicity, max_age is required, and we don't need the standard decorator pattern
    where it can be called with or without parentheses when no arguments are passed.)

    Args:
        func: The async function to be cached.
        max_age: The maximum age (in seconds) for which the cached result is valid.
    Returns:
        An async function that returns the cached result if valid, otherwise calls the original function.
    """

    def decorator(
        func: Callable[[], Coroutine[None, None, R]],
    ) -> Callable[[], Awaitable[R]]:
        cache_task: asyncio.Task[R] | None = None
        cache_time: float | None = None

        @wraps(func)
        def wrapper() -> Awaitable[R]:
            nonlocal cache_task, cache_time
            now = asyncio.get_running_loop().time()

            if (
                cache_task is None
                or cache_task.cancelled()
                or (cache_task.done() and cache_task.exception() is not None)
                or cache_time is None
                or (max_age is not None and (now - cache_time) > max_age)
            ):
                cache_task = asyncio.create_task(func())
                cache_time = now

            return cache_task

        return wrapper

    return decorator


@cache_async(max_age=10 * 60)  # cache for 10 minutes
async def get_qe_data_map() -> dict[str, dict[str, dict[str, str]]]:
    async with aiohttp_session().get(QE_DATA_URL) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


async def get_qe_data(component: str) -> TestLocationInfo:
    map = await get_qe_data_map()
    component_values = map["components"][component]
    team_values = map["teams"][component_values["assigned_team"]]
    organization_values = map["organizations"][team_values["organization"]]

    combined_values = {
        k: v.replace("$c", component)
        for k, v in {**organization_values, **team_values, **component_values}.items()
        if k not in {"assigned_team", "organization"}
    }

    return TestLocationInfo(**combined_values)
