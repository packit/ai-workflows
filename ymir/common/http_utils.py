"""Shared HTTP sessions and retry helpers used by sweep and tool clients."""

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

import aiohttp
import requests

from ymir.common import constants

logger = logging.getLogger(__name__)

_aiohttp_session: ContextVar[aiohttp.ClientSession | None] = ContextVar("aiohttp_session", default=None)
_requests_session: ContextVar[requests.Session | None] = ContextVar("requests_session", default=None)


@asynccontextmanager
async def with_aiohttp_session():
    """Create or reuse a context-scoped aiohttp session."""
    session = _aiohttp_session.get()
    if session is None:
        async with aiohttp.ClientSession(
            headers={"User-Agent": constants.YMIR_USER_AGENT}, timeout=constants.AIOHTTP_TIMEOUT
        ) as session:
            token = _aiohttp_session.set(session)
            try:
                yield session
            finally:
                _aiohttp_session.reset(token)
    else:
        yield session


def aiohttp_session() -> aiohttp.ClientSession:
    """Return the current session from ``with_aiohttp_session``."""
    session = _aiohttp_session.get()
    if session is None:
        raise RuntimeError("Session not initialized - use within with_aiohttp_session")
    return session


@asynccontextmanager
async def with_requests_session():
    """Create or reuse a context-scoped requests session."""
    session = _requests_session.get()
    if session is None:
        with requests.Session() as session:
            session.headers.update({"User-Agent": constants.YMIR_USER_AGENT})
            token = _requests_session.set(session)
            try:
                yield session
            finally:
                _requests_session.reset(token)
    else:
        yield session


def requests_session() -> requests.Session:
    """Return the current session from ``with_requests_session``."""
    session = _requests_session.get()
    if session is None:
        raise RuntimeError("Session not initialized - use within with_requests_session")
    return session


@asynccontextmanager
async def with_http_sessions():
    """Create or reuse both supported HTTP sessions."""
    async with with_aiohttp_session(), with_requests_session():
        yield


@asynccontextmanager
async def aiohttp_get_with_retries(
    session: aiohttp.ClientSession,
    url: str,
    yield_final_retryable_response: bool = False,
    *,
    retryable_status_codes: frozenset[int] | None = None,
    max_retries: int | None = None,
    retry_backoff_base: int | None = None,
    **kwargs,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """Yield a GET response after retrying configured transient statuses.

    ``max_retries`` counts total attempts and must be positive. On the last
    retryable response, either yield it when requested or raise
    ``ClientResponseError``. Connection errors propagate without retries.
    The response remains open while the caller uses this context manager.

    Args:
        session: Session used for every GET attempt.
        url: URL to request.
        yield_final_retryable_response: Yield the last retryable response
            instead of raising, so the caller can handle its status.
        retryable_status_codes: Override the default set of retryable codes.
        max_retries: Total number of attempts, defaulting to the shared policy.
        retry_backoff_base: Base delay in seconds for exponential backoff.
        **kwargs: Additional arguments passed to ``session.get``.

    Yields:
        The first nonretryable response, or the final retryable response
        when ``yield_final_retryable_response`` is true.

    Raises:
        ValueError: ``max_retries`` is less than one.
        aiohttp.ClientResponseError: All attempts return retryable statuses
            and the final response was not requested.
    """
    statuses = (
        constants.AIOHTTP_RETRYABLE_STATUS_CODES if retryable_status_codes is None else retryable_status_codes
    )
    retries = constants.AIOHTTP_MAX_RETRIES if max_retries is None else max_retries
    if retries < 1:
        raise ValueError("max_retries must be at least 1")
    backoff_base = (
        retry_backoff_base if retry_backoff_base is not None else constants.AIOHTTP_RETRY_BACKOFF_BASE
    )
    for attempt in range(retries):
        async with session.get(url, **kwargs) as response:
            if response.status not in statuses:
                yield response
                return
            if attempt >= retries - 1:
                if yield_final_retryable_response:
                    yield response
                    return
                raise aiohttp.ClientResponseError(
                    response.request_info,
                    response.history,
                    status=response.status,
                    message=f"after {retries} retries",
                )
            retry_status = response.status
        delay = backoff_base * (2**attempt) + random.uniform(0, 1)  # noqa: S311
        logger.warning(
            "Transient HTTP %d on %s, retrying in %.1fs (attempt %d/%d)",
            retry_status,
            url,
            delay,
            attempt + 1,
            retries,
        )
        await asyncio.sleep(delay)


@asynccontextmanager
async def _session_context(session: aiohttp.ClientSession | None):
    """Yield the supplied session, or a context-scoped default session."""
    if session is None:
        async with with_aiohttp_session() as current:
            yield current
    else:
        yield session


async def aiohttp_api_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    session: aiohttp.ClientSession | None = None,
) -> object:
    """GET JSON using the shared retry policy and raise for HTTP errors."""
    kwargs = {
        key: value for key, value in {"params": params, "headers": headers}.items() if value is not None
    }
    async with (
        _session_context(session) as current,
        aiohttp_get_with_retries(current, url, **kwargs) as response,
    ):
        response.raise_for_status()
        return await response.json()


@asynccontextmanager
async def aiohttp_api_response(
    method: str,
    url: str,
    *,
    json: object | None = None,
    headers: dict[str, str] | None = None,
    params: dict | None = None,
    session: aiohttp.ClientSession | None = None,
    retry_get: bool = True,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """Yield an HTTP response, applying retries to GET requests by default.

    Args:
        method: HTTP method name; GET can use the shared retry policy.
        url: URL to request.
        json: Optional JSON request body for nonretrying requests.
        headers: Optional request headers.
        params: Optional query parameters.
        session: Session to use, or ``None`` for the context-scoped session.
        retry_get: Apply retry policy to GET requests when true.

    Yields:
        An open response whose status has passed ``raise_for_status``.
    """
    async with _session_context(session) as current:
        if method.lower() == "get" and retry_get:
            kwargs = {
                key: value
                for key, value in {"params": params, "headers": headers}.items()
                if value is not None
            }
            async with aiohttp_get_with_retries(current, url, **kwargs) as response:
                response.raise_for_status()
                yield response
        else:
            request = getattr(current, method.lower())
            kwargs = {
                key: value
                for key, value in {"json": json, "headers": headers, "params": params}.items()
                if value is not None
            }
            async with request(url, **kwargs) as response:
                response.raise_for_status()
                yield response


async def aiohttp_api_request(
    method: str,
    url: str,
    *,
    json: object | None = None,
    headers: dict[str, str] | None = None,
    params: dict | None = None,
    session: aiohttp.ClientSession | None = None,
) -> None:
    """Send a request and raise for HTTP errors, discarding the response.

    The method is passed to ``aiohttp_api_response`` with GET retries
    disabled. When ``json`` is supplied, it is sent as the request body.
    """
    async with aiohttp_api_response(
        method,
        url,
        json=json,
        headers=headers,
        params=params,
        session=session,
        retry_get=False,
    ):
        pass
