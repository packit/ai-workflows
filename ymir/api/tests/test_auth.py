"""Unit tests for the OIDC authentication middleware."""

import json
import time
from unittest.mock import patch

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from jwcrypto import jwk, jwt

# ---------------------------------------------------------------------------
# Helpers: generate a key pair + sign a test token
# ---------------------------------------------------------------------------


def _make_keyset() -> jwk.JWKSet:
    """Generate an RSA key pair and return it as a JWKSet."""
    key = jwk.JWK.generate(kty="RSA", size=2048, kid="test-key")
    keyset = jwk.JWKSet()
    keyset.add(key)
    return keyset


def _sign_token(keyset: jwk.JWKSet, claims: dict, kid: str = "test-key") -> str:
    """Sign *claims* with the first key in *keyset* and return the compact JWS."""
    key = keyset.get_key(kid)
    token = jwt.JWT(
        header={"alg": "RS256", "kid": kid},
        claims=json.dumps(claims),
    )
    token.make_signed_token(key)
    return token.serialize()


def _make_valid_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": "http://localhost:8084/realms/ymir",
        "aud": "ymir-trace-ui",
        "sub": "test-user-id",
        "preferred_username": "testuser",
        "email": "testuser@example.com",
        "iat": now - 60,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def keyset():
    return _make_keyset()


@pytest.fixture
def valid_token(keyset):
    return _sign_token(keyset, _make_valid_claims())


@pytest_asyncio.fixture
async def app_and_client(keyset):
    """Create a minimal aiohttp app with the OIDC middleware and a test route."""
    from ymir.api import auth

    # Patch env vars and JWKS cache for the middleware.
    with (
        patch.object(auth, "OIDC_PROVIDER_URL", "http://localhost:8084/realms/ymir"),
        patch.object(auth, "OIDC_ISSUER", "http://localhost:8084/realms/ymir"),
        patch.object(auth, "OIDC_CLIENT_ID", "ymir-trace-ui"),
        patch.object(auth, "OIDC_CORS_ALLOWED_ORIGIN", "http://localhost:8082"),
        patch.object(auth, "OIDC_CORS_ALLOWED_ORIGIN_ALT", "DISABLED"),
        patch.object(auth, "_jwks_cache", keyset),
        patch.object(auth, "_jwks_fetched_at", time.monotonic()),
    ):

        async def protected_handler(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "user": request.get("remote_user", "unknown"),
                }
            )

        async def public_handler(request: web.Request) -> web.Response:
            return web.json_response({"status": "ok"})

        app = web.Application(middlewares=[auth.oidc_middleware])
        app.router.add_get("/healthz", public_handler)
        app.router.add_get("/readyz", public_handler)
        app.router.add_post("/api/jira/webhook", public_handler)
        app.router.add_post("/api/consolidation", protected_handler)

        async with TestClient(TestServer(app)) as client:
            yield app, client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_path_no_auth_needed(app_and_client):
    _, client = app_and_client
    resp = await client.get("/healthz")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_readyz_no_auth_needed(app_and_client):
    _, client = app_and_client
    resp = await client.get("/readyz")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_webhook_no_auth_needed(app_and_client):
    _, client = app_and_client
    resp = await client.post("/api/jira/webhook", json={})
    assert resp.status == 200


@pytest.mark.asyncio
async def test_protected_path_missing_token(app_and_client):
    _, client = app_and_client
    resp = await client.post("/api/consolidation", json={})
    assert resp.status == 401
    body = await resp.json()
    assert "Authorization" in body["error"]


@pytest.mark.asyncio
async def test_protected_path_invalid_token(app_and_client):
    _, client = app_and_client
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_protected_path_valid_token(app_and_client, keyset):
    _, client = app_and_client
    token = _sign_token(keyset, _make_valid_claims())
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["user"] == "testuser"


@pytest.mark.asyncio
async def test_expired_token_rejected(app_and_client, keyset):
    _, client = app_and_client
    expired_claims = _make_valid_claims(exp=int(time.time()) - 60)
    token = _sign_token(keyset, expired_claims)
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_wrong_key_rejected(app_and_client):
    """A token signed with a different key must be rejected."""
    _, client = app_and_client
    other_keyset = _make_keyset()
    token = _sign_token(other_keyset, _make_valid_claims())
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_wrong_issuer_rejected(app_and_client, keyset):
    """A token with a non-matching issuer must be rejected."""
    _, client = app_and_client
    token = _sign_token(keyset, _make_valid_claims(iss="http://evil.example.com/realms/other"))
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_wrong_audience_rejected(app_and_client, keyset):
    """A token with a non-matching audience must be rejected."""
    _, client = app_and_client
    token = _sign_token(keyset, _make_valid_claims(aud="other-client"))
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_audience_accepted_in_list(app_and_client, keyset):
    """When ``aud`` is a list containing the expected audience, accept it."""
    _, client = app_and_client
    token = _sign_token(keyset, _make_valid_claims(aud=["account", "ymir-trace-ui"]))
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status == 200


@pytest.mark.asyncio
async def test_audience_list_missing_expected_rejected(app_and_client, keyset):
    """When ``aud`` is a list that does *not* contain the expected audience, reject."""
    _, client = app_and_client
    token = _sign_token(keyset, _make_valid_claims(aud=["account", "other-client"]))
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_cors_preflight_returns_204(app_and_client):
    _, client = app_and_client
    resp = await client.options(
        "/api/consolidation",
        headers={
            "Origin": "http://localhost:8082",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status == 204
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost:8082"
    assert "POST" in resp.headers["Access-Control-Allow-Methods"]


@pytest.mark.asyncio
async def test_cors_preflight_unknown_origin(app_and_client):
    _, client = app_and_client
    resp = await client.options(
        "/api/consolidation",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status == 204
    assert "Access-Control-Allow-Origin" not in resp.headers


@pytest.mark.asyncio
async def test_cors_headers_on_authenticated_response(app_and_client, keyset):
    _, client = app_and_client
    token = _sign_token(keyset, _make_valid_claims())
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://localhost:8082",
        },
    )
    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "http://localhost:8082"


@pytest.mark.asyncio
async def test_no_cors_for_unknown_origin_on_auth_response(app_and_client, keyset):
    _, client = app_and_client
    token = _sign_token(keyset, _make_valid_claims())
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://evil.example.com",
        },
    )
    assert resp.status == 200
    assert "Access-Control-Allow-Origin" not in resp.headers


@pytest.mark.asyncio
async def test_user_identity_preferred_username(app_and_client, keyset):
    _, client = app_and_client
    token = _sign_token(keyset, _make_valid_claims(preferred_username="jdoe"))
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = await resp.json()
    assert body["user"] == "jdoe"


@pytest.mark.asyncio
async def test_user_identity_falls_back_to_email(app_and_client, keyset):
    """When preferred_username is absent, fall back to email."""
    _, client = app_and_client
    claims = _make_valid_claims()
    del claims["preferred_username"]
    token = _sign_token(keyset, claims)
    resp = await client.post(
        "/api/consolidation",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = await resp.json()
    assert body["user"] == "testuser@example.com"


@pytest.mark.asyncio
async def test_oidc_disabled_passes_through():
    """When OIDC_PROVIDER_URL is empty, all requests pass through."""
    from ymir.api import auth

    with (
        patch.object(auth, "OIDC_PROVIDER_URL", ""),
        patch.object(auth, "OIDC_CORS_ALLOWED_ORIGIN", ""),
        patch.object(auth, "OIDC_CORS_ALLOWED_ORIGIN_ALT", ""),
    ):

        async def handler(request: web.Request) -> web.Response:
            return web.json_response({"ok": True})

        app = web.Application(middlewares=[auth.oidc_middleware])
        app.router.add_post("/api/consolidation", handler)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/consolidation", json={})
            assert resp.status == 200
