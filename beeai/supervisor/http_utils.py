from contextlib import asynccontextmanager
from contextvars import ContextVar
import aiohttp

import requests


# We use *both* aiohttp and requests in various places, so we need to
# set up sessions for both libraries. (We can't convert everything to
# aiohttp because of our usage of requests_gssapi, but aiohttp is nice
# within code that is already async.)


_aiohttp_session = ContextVar[aiohttp.ClientSession | None](
    "aiohttp_session", default=None
)


@asynccontextmanager
async def with_aiohttp_session():
    """
    Context manager that sets up a scoped aiohttp.ClientSession
    appropriate for our usage; currently it's just a plain session,
    but it could be extended in the future to, e.g, have retries
    or timeouts.

    This can also be used as a decorator on async functions.
    """
    session = _aiohttp_session.get()

    if session is None:
        async with aiohttp.ClientSession() as session:
            token = _aiohttp_session.set(session)
            try:
                yield session
            finally:
                _aiohttp_session.reset(token)
    else:
        yield session


def aiohttp_session() -> aiohttp.ClientSession:
    """
    Get the current aiohttp.ClientSession. Must be called within the
    context of an enclosing with_aiohttp_session() decorator or block.
    """
    session = _aiohttp_session.get()
    if session is None:
        raise RuntimeError("Session not initialized - use within with_aiohttp_session")

    return session


_requests_session = ContextVar[requests.Session | None](
    "requests_session", default=None
)


@asynccontextmanager
async def with_requests_session():
    """
    Context manager that sets up a scoped requests.Session
    appropriate for our usage; currently it's just a plain session,
    but it could be extended in the future to, e.g, have retries
    or timeouts.

    This can also be used as a decorator on async functions.
    """
    session = _requests_session.get()

    if session is None:
        with requests.Session() as session:
            token = _requests_session.set(session)
            try:
                yield session
            finally:
                _requests_session.reset(token)
    else:
        yield session


def requests_session() -> requests.Session:
    """
    Get the current requests.Session. Must be called within the
    context of an enclosing with_requests_session() decorator or block.
    """
    session = _requests_session.get()
    if session is None:
        raise RuntimeError("Session not initialized - use within with_requests_session")

    return session


@asynccontextmanager
async def with_http_sessions():
    """
    Convenience context manager that sets up both aiohttp and requests sessions.
    """
    async with with_aiohttp_session():
        async with with_requests_session():
            yield
