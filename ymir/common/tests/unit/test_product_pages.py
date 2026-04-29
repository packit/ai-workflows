"""
Unit tests for ``ymir.common.product_pages``.

HTTP is simulated by replacing ``requests.Session`` with a small fake that returns
fixed status codes and JSON bodies — no network calls.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests.exceptions
from beeai_framework.tools import ToolError

import ymir.common.product_pages as pp
from ymir.common.utils import KerberosError


async def _fake_init_kerberos_ok() -> str:
    return "user@EXAMPLE.COM"


async def _fake_init_kerberos_fail() -> None:
    raise KerberosError("ticket unavailable")


class _JsonResponse:
    __slots__ = ("status_code", "_data")

    def __init__(self, status_code: int, data: object | None = None) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> object:
        if self._data is None:
            raise ValueError("no json payload")
        return self._data


class _FakeSession:
    """Minimal session stub: one POST (OIDC), then two GETs (active releases, z-stream list)."""

    def __init__(
        self,
        *,
        post_response: _JsonResponse,
        get_responses: list[_JsonResponse],
    ) -> None:
        self.verify: bool | str | None = None
        self._post_response = post_response
        self._get_responses = list(get_responses)

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> _JsonResponse:
        return self._post_response

    def get(self, url: str, **kwargs: object) -> _JsonResponse:
        return self._get_responses.pop(0)


@pytest.fixture(autouse=True)
def _clear_product_pages_verify_cache() -> None:
    pp._product_pages_verify.cache_clear()
    yield
    pp._product_pages_verify.cache_clear()


def test_product_pages_verify_prefers_redhat_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDHAT_IT_CA_BUNDLE", "/ca/redhat.pem")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/ca/requests.pem")
    assert pp._product_pages_verify() == "/ca/redhat.pem"


def test_product_pages_verify_falls_back_to_requests_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDHAT_IT_CA_BUNDLE", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/ca/requests.pem")
    assert pp._product_pages_verify() == "/ca/requests.pem"


def test_product_pages_verify_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDHAT_IT_CA_BUNDLE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    assert pp._product_pages_verify() is True


def test_fetch_rhel_streams_snapshot_sync_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDHAT_IT_CA_BUNDLE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    active = [
        {"shortname": "rhel-9.5"},
        {"shortname": "rhel-9.6"},
    ]
    z_rows = [
        {
            "shortname": "rhel-9.5",
            "name_incl_maint": "RHEL 9.5 (GA/ZStream)",
            "name": "RHEL 9.5",
        },
    ]
    fake = _FakeSession(
        post_response=_JsonResponse(200),
        get_responses=[
            _JsonResponse(200, active),
            _JsonResponse(200, z_rows),
        ],
    )

    with patch.object(pp.requests, "Session", return_value=fake):
        result = pp._fetch_rhel_streams_snapshot_sync()

    assert fake.verify is True
    assert result == {
        "current_y_streams": {"9": "rhel-9.6"},
        "current_z_streams": {"9": "rhel-9.5.z"},
        "upcoming_z_streams": {"9": "rhel-9.5.z"},
    }


def test_fetch_rhel_streams_snapshot_sync_oidc_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDHAT_IT_CA_BUNDLE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    fake = _FakeSession(
        post_response=_JsonResponse(401),
        get_responses=[],
    )

    with patch.object(pp.requests, "Session", return_value=fake), pytest.raises(
        ToolError,
        match="OIDC authenticate",
    ):
        pp._fetch_rhel_streams_snapshot_sync()


def test_fetch_rhel_streams_snapshot_sync_ssl_error_includes_ca_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REDHAT_IT_CA_BUNDLE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    class _SslSession(_FakeSession):
        def post(self, url: str, **kwargs: object) -> _JsonResponse:
            raise requests.exceptions.SSLError("certificate verify failed")

    fake = _SslSession(
        post_response=_JsonResponse(200),
        get_responses=[],
    )

    with patch.object(pp.requests, "Session", return_value=fake), pytest.raises(
        ToolError,
        match="REDHAT_IT_CA_BUNDLE",
    ):
        pp._fetch_rhel_streams_snapshot_sync()


@pytest.mark.asyncio
async def test_fetch_rhel_streams_snapshot_kerberos_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pp, "init_kerberos_ticket", _fake_init_kerberos_fail)

    with pytest.raises(ToolError, match="Failed to initialize Kerberos ticket"):
        await pp.fetch_rhel_streams_snapshot()


@pytest.mark.asyncio
async def test_fetch_rhel_streams_snapshot_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDHAT_IT_CA_BUNDLE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.setattr(pp, "init_kerberos_ticket", _fake_init_kerberos_ok)

    active = [{"shortname": "rhel-10.0"}]
    z_rows: list[dict] = []

    fake = _FakeSession(
        post_response=_JsonResponse(200),
        get_responses=[
            _JsonResponse(200, active),
            _JsonResponse(200, z_rows),
        ],
    )

    with patch.object(pp.requests, "Session", return_value=fake):
        result = await pp.fetch_rhel_streams_snapshot()

    assert result["current_y_streams"] == {"10": "rhel-10.0"}
    assert result["current_z_streams"] == {}
    assert result["upcoming_z_streams"] == {}
