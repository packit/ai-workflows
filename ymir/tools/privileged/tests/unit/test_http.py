import asyncio
import logging
from contextlib import asynccontextmanager

import aiohttp
import pytest
from flexmock import flexmock

from ymir.common import constants, http_utils
from ymir.common.http_utils import aiohttp_get_with_retries


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
async def test_retry_warnings_log_sanitized_url(caplog):
    """Retry warnings must not leak credentials embedded in the URL."""

    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep).once()
    session, _ = _make_session(
        [
            _mock_response(503),
            _mock_response(200),
        ]
    )
    url = "https://oauth2:super-secret-token@gitlab.com/api/v4/projects"  # pragma: allowlist secret

    with caplog.at_level(logging.WARNING, logger="ymir.common.http_utils"):
        async with aiohttp_get_with_retries(session, url, statuses={503}, retries=2) as resp:
            assert resp.status == 200

    messages = [record.getMessage() for record in caplog.records]
    assert any("oauth2:***@gitlab.com/api/v4/projects" in message for message in messages)
    assert not any("super-secret-token" in message for message in messages)


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
async def test_exhausted_retries_yields_final_response_when_requested():
    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep).twice()
    session, get_count = _make_session(
        [
            _mock_response(503),
            _mock_response(503),
            _mock_response(503),
        ]
    )

    async with aiohttp_get_with_retries(
        session,
        "http://example.com",
        yield_final_retryable_response=True,
    ) as response:
        assert response.status == 503

    assert get_count() == 3


@pytest.mark.asyncio
async def test_backoff_delays_increase():
    delays = []

    async def _capture_sleep(delay):
        delays.append(delay)
        return

    flexmock(asyncio).should_receive("sleep").replace_with(_capture_sleep).twice()
    flexmock(http_utils.random).should_receive("uniform").and_return(0.5)
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


def _make_mixed_session(items):
    """Create a mock session whose .get() yields responses or raises exceptions in order.

    Each item is either a response (yielded) or an exception class/instance
    (raised while acquiring the response).
    """
    call_count = 0

    @asynccontextmanager
    async def _mock_get(url, **kwargs):
        nonlocal call_count
        item = items[call_count]
        call_count += 1
        if isinstance(item, Exception) or (isinstance(item, type) and issubclass(item, Exception)):
            raise item
        yield item

    session = flexmock(get=_mock_get)
    return session, lambda: call_count


@pytest.mark.asyncio
async def test_retries_connection_error_then_succeeds():
    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep).once()
    session, get_count = _make_mixed_session(
        [
            aiohttp.ClientConnectionError("connection reset"),
            _mock_response(200),
        ]
    )

    async with aiohttp_get_with_retries(session, "http://example.com") as resp:
        assert resp.status == 200

    assert get_count() == 2


@pytest.mark.asyncio
async def test_retries_timeout_then_succeeds():
    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep).once()
    session, get_count = _make_mixed_session(
        [
            TimeoutError(),
            _mock_response(200),
        ]
    )

    async with aiohttp_get_with_retries(session, "http://example.com") as resp:
        assert resp.status == 200

    assert get_count() == 2


@pytest.mark.asyncio
async def test_exhausted_connection_errors_raise():
    flexmock(asyncio).should_receive("sleep").replace_with(_mock_sleep).twice()
    session, get_count = _make_mixed_session(
        [
            aiohttp.ClientConnectionError("reset"),
            aiohttp.ClientConnectionError("reset"),
            aiohttp.ClientConnectionError("reset"),
        ]
    )

    with pytest.raises(aiohttp.ClientConnectionError):
        async with aiohttp_get_with_retries(session, "http://example.com"):
            pass

    assert get_count() == 3


@pytest.mark.asyncio
async def test_non_connection_client_error_propagates_without_retry():
    flexmock(asyncio).should_receive("sleep").never()
    session, get_count = _make_mixed_session([aiohttp.ClientError("boom")])

    with pytest.raises(aiohttp.ClientError):
        async with aiohttp_get_with_retries(session, "http://example.com"):
            pass

    assert get_count() == 1


@pytest.mark.asyncio
async def test_error_from_caller_body_is_not_retried():
    """A read error raised by the caller must propagate, not restart the loop."""
    flexmock(asyncio).should_receive("sleep").never()
    session, get_count = _make_mixed_session([_mock_response(200)])

    with pytest.raises(UnicodeDecodeError):
        async with aiohttp_get_with_retries(session, "http://example.com") as resp:
            assert resp.status == 200
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    assert get_count() == 1


def test_shared_timeout_stays_tight_for_other_clients():
    """Only the privileged GitHub tools get the generous cap; everything else
    (upstream_search, upstream_tools, ...) runs under its own overall deadline
    that a 120s-per-request cap with retries would blow through."""
    assert constants.AIOHTTP_TIMEOUT.total == 30
    assert constants.GITHUB_AIOHTTP_TIMEOUT.total == 120
    assert constants.GITHUB_AIOHTTP_TIMEOUT.connect == 10
