from contextlib import asynccontextmanager
from unittest.mock import patch

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
    async def fake_get(url, **kwargs):
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        yield resp

    session = flexmock(get=fake_get)
    return session, lambda: call_count


@pytest.mark.asyncio
@patch("ymir.tools.http.asyncio.sleep", return_value=None)
async def test_success_no_retry(mock_sleep):
    session, get_count = _make_session([_mock_response(200)])

    async with aiohttp_get_with_retries(session, "http://example.com") as resp:
        assert resp.status == 200

    assert get_count() == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
@patch("ymir.tools.http.asyncio.sleep", return_value=None)
async def test_non_retryable_error_passes_through(mock_sleep):
    session, get_count = _make_session([_mock_response(404)])

    async with aiohttp_get_with_retries(session, "http://example.com") as resp:
        assert resp.status == 404

    assert get_count() == 1
    mock_sleep.assert_not_called()


@pytest.mark.asyncio
@patch("ymir.tools.http.asyncio.sleep", return_value=None)
async def test_retries_on_503_then_succeeds(mock_sleep):
    session, get_count = _make_session(
        [
            _mock_response(503),
            _mock_response(200),
        ]
    )

    async with aiohttp_get_with_retries(session, "http://example.com") as resp:
        assert resp.status == 200

    assert get_count() == 2
    mock_sleep.assert_called_once()


@pytest.mark.asyncio
@patch("ymir.tools.http.asyncio.sleep", return_value=None)
async def test_exhausted_retries_raises(mock_sleep):
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
    assert mock_sleep.call_count == 2


@pytest.mark.asyncio
@patch("ymir.tools.http.random.uniform", return_value=0.5)
@patch("ymir.tools.http.asyncio.sleep", return_value=None)
async def test_backoff_delays_increase(mock_sleep, mock_uniform):
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

    delays = [call.args[0] for call in mock_sleep.call_args_list]
    assert delays == [2.5, 4.5]  # base=2: 2*1+0.5, 2*2+0.5


@pytest.mark.asyncio
@patch("ymir.tools.http.asyncio.sleep", return_value=None)
async def test_kwargs_forwarded_to_session_get(mock_sleep):
    captured_kwargs = {}

    @asynccontextmanager
    async def fake_get(url, **kwargs):
        captured_kwargs.update(kwargs)
        yield _mock_response(200)

    session = flexmock(get=fake_get)

    async with aiohttp_get_with_retries(
        session, "http://example.com", headers={"X-Test": "1"}, params={"q": "search"}
    ) as resp:
        assert resp.status == 200

    assert captured_kwargs["headers"] == {"X-Test": "1"}
    assert captured_kwargs["params"] == {"q": "search"}
