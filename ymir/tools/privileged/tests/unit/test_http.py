import asyncio
import random
from contextlib import asynccontextmanager

import aiohttp
import pytest
from flexmock import flexmock

from ymir.tools.http import aiohttp_get_with_retries


def _mock_response(status=200, **kwargs):
    return flexmock(
        status=status,
        request_info=flexmock(),
        history=(),
        **kwargs,
    )


def _make_session(responses):
    """Create a mock session whose .get() yields *responses* in order."""
    call_count = 0

    @asynccontextmanager
    async def _mock_get(url, **kwargs):
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        yield resp

    session = flexmock(get=_mock_get)
    return session, lambda: call_count


async def _mock_sleep(*_args, **_kwargs):
    return None


async def _mock_random(*_args, **_kwargs):
    return 0.5


@pytest.mark.asyncio
async def test_success_no_retry():
    flexmock(asyncio).should_receive("sleep").never()
    session, get_count = _make_session([_mock_response(200)])

    async with aiohttp_get_with_retries(session, "http://example.com") as resp:
        assert resp.status == 200

    assert get_count() == 1


@pytest.mark.asyncio
async def test_non_retryable_error_passes_through():
    flexmock(asyncio).should_receive("sleep").never()
    session, get_count = _make_session([_mock_response(404)])

    async with aiohttp_get_with_retries(session, "http://example.com") as resp:
        assert resp.status == 404

    assert get_count() == 1


@pytest.mark.asyncio
async def test_retries_on_503_then_succeeds():

    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep).once()
    session, get_count = _make_session(
        [
            _mock_response(503),
            _mock_response(200),
        ]
    )

    async with aiohttp_get_with_retries(session, "http://example.com") as resp:
        assert resp.status == 200

    assert get_count() == 2


@pytest.mark.asyncio
async def test_exhausted_retries_raises():

    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep).twice()
    session, get_count = _make_session(
        [
            _mock_response(503),
            _mock_response(503),
            _mock_response(503),
        ]
    )

    with pytest.raises(aiohttp.ClientResponseError) as exc_info:
        async with aiohttp_get_with_retries(session, "http://example.com"):
            pass

    assert exc_info.value.status == 503
    assert "after 3 retries" in exc_info.value.message
    assert get_count() == 3


@pytest.mark.asyncio
async def test_backoff_delays_increase():
    delays = []

    async def _capture_sleep(delay):
        delays.append(delay)
        return

    flexmock(asyncio).should_receive("sleep").replace_with(_capture_sleep).twice()
    flexmock(random).should_receive("uniform").and_return(0.5)
    session, _ = _make_session(
        [
            _mock_response(503),
            _mock_response(503),
            _mock_response(503),
        ]
    )

    with pytest.raises(aiohttp.ClientResponseError):
        async with aiohttp_get_with_retries(session, "http://example.com"):
            pass

    assert delays == [2.5, 4.5]  # base=2: 2*1+0.5, 2*2+0.5


@pytest.mark.asyncio
async def test_kwargs_forwarded_to_session_get():
    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep)
    captured_kwargs = {}

    @asynccontextmanager
    async def _mock_get(url, **kwargs):
        captured_kwargs.update(kwargs)
        yield _mock_response(200)

    session = flexmock(get=_mock_get)

    async with aiohttp_get_with_retries(
        session, "http://example.com", headers={"X-Test": "1"}, params={"q": "search"}
    ) as resp:
        assert resp.status == 200

    assert captured_kwargs["headers"] == {"X-Test": "1"}
    assert captured_kwargs["params"] == {"q": "search"}
