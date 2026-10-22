"""Compatibility exports for supervisor imports of shared HTTP sessions."""

from ymir.common.http_utils import (
    aiohttp_session,
    requests_session,
    with_aiohttp_session,
    with_http_sessions,
    with_requests_session,
)

__all__ = [
    "aiohttp_session",
    "requests_session",
    "with_aiohttp_session",
    "with_http_sessions",
    "with_requests_session",
]
