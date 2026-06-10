import asyncio
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp

from ymir.tools.constants import (
    AIOHTTP_MAX_RETRIES,
    AIOHTTP_RETRY_BACKOFF_BASE,
    AIOHTTP_RETRYABLE_STATUS_CODES,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def aiohttp_get_with_retries(
    session: aiohttp.ClientSession,
    url: str,
    **kwargs,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """Drop-in replacement for ``session.get()`` that retries on transient HTTP errors.

    Retries up to ``AIOHTTP_MAX_RETRIES`` times on status codes listed in
    ``AIOHTTP_RETRYABLE_STATUS_CODES`` using exponential back-off.  Raises
    ``aiohttp.ClientResponseError`` when all retries are exhausted.
    """
    for attempt in range(AIOHTTP_MAX_RETRIES):
        async with session.get(url, **kwargs) as response:
            if response.status not in AIOHTTP_RETRYABLE_STATUS_CODES:
                yield response
                return
            if attempt >= AIOHTTP_MAX_RETRIES - 1:
                raise aiohttp.ClientResponseError(
                    response.request_info,
                    response.history,
                    status=response.status,
                    message=f"after {AIOHTTP_MAX_RETRIES} retries",
                )
            retry_status = response.status
        # Response context is closed here — sleep without holding the connection
        delay = AIOHTTP_RETRY_BACKOFF_BASE * (2**attempt) + random.uniform(0, 1)  # noqa: S311
        logger.warning(
            "Transient HTTP %d on %s, retrying in %.1fs (attempt %d/%d)",
            retry_status,
            url,
            delay,
            attempt + 1,
            AIOHTTP_MAX_RETRIES,
        )
        await asyncio.sleep(delay)
