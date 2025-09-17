from contextlib import asynccontextmanager
from contextvars import ContextVar
import aiohttp
import os
import ssl

import requests
import requests.adapters


# We use *both* aiohttp and requests in various places, so we need to
# set up sessions for both libraries. (We can't convert everything to
# aiohttp because of our usage of requests_gssapi, but aiohttp is nice
# within code that is already async.)


def _create_ssl_context_with_extra_ca() -> ssl.SSLContext:
    """
    Create an SSL context that includes extra CA certificates from the
    REDHAT_IT_CA_BUNDLE environment variable, if set.
    """
    context = ssl.create_default_context()
    redhat_it_ca_bundle = os.getenv("REDHAT_IT_CA_BUNDLE")
    if redhat_it_ca_bundle:
        context.load_verify_locations(cafile=redhat_it_ca_bundle)
    return context


_aiohttp_session = ContextVar[aiohttp.ClientSession | None](
    "aiohttp_session", default=None
)


@asynccontextmanager
async def with_aiohttp_session():
    """
    Context manager that sets up a scoped aiohttp.ClientSession
    appropriate for our usage, including using any extra CA certificates
    specified in the REDHAT_IT_CA_BUNDLE environment variable.

    This can also be used as a decorator on async functions.
    """
    session = _aiohttp_session.get()

    if session is None:
        connector = aiohttp.TCPConnector(ssl=_create_ssl_context_with_extra_ca())

        async with aiohttp.ClientSession(connector=connector) as session:
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


class ExtraCAHTTPAdapter(requests.adapters.HTTPAdapter):
    def __init__(self):
        self._ssl_context = _create_ssl_context_with_extra_ca()
        super().__init__()

    def init_poolmanager(self, *args, **kwargs) -> None:
        kwargs["ssl_context"] = self._ssl_context
        return super().init_poolmanager(*args, **kwargs)


_requests_session = ContextVar[requests.Session | None](
    "requests_session", default=None
)


@asynccontextmanager
async def with_requests_session():
    """
    Context manager that sets up a scoped requests.Session
    appropriate for our usage, including using any extra CA certificates
    specified in the REDHAT_IT_CA_BUNDLE environment variable.

    This can also be used as a decorator on async functions.
    """
    session = _requests_session.get()

    if session is None:
        with requests.Session() as session:
            session.mount("https://", ExtraCAHTTPAdapter())
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
