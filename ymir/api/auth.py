"""OIDC JWT authentication middleware for the Ymir API.

Validates Bearer tokens using the OIDC provider's JWKS endpoint.
Protected paths require a valid JWT; unauthenticated paths (probes,
webhooks) are passed through.  CORS headers are set for allowed
origins.

Environment variables
---------------------
OIDC_PROVIDER_URL           Keycloak realm URL (e.g. https://sso.redhat.com/auth/realms/redhat-external).
                            When empty, authentication is disabled (all requests pass through).
                            Used for JWKS fetching.
OIDC_ISSUER                 Expected ``iss`` claim in JWTs.  Defaults to ``OIDC_PROVIDER_URL``.
                            Override when the token issuer differs from the JWKS URL
                            (e.g. local dev: tokens carry the external Keycloak URL while
                            the API fetches JWKS via the container-internal URL).
OIDC_CLIENT_ID              OIDC client identifier (audience).  When set, the ``aud``
                            claim in the JWT must contain this value.
OIDC_CORS_ALLOWED_ORIGIN    Primary allowed CORS origin.
OIDC_CORS_ALLOWED_ORIGIN_ALT  Secondary allowed CORS origin (set to DISABLED if unused).
"""

import json
import logging
import os
import time

import aiohttp
from aiohttp import web
from jwcrypto import jwk, jwt

logger = logging.getLogger(__name__)

OIDC_PROVIDER_URL = os.environ.get("OIDC_PROVIDER_URL", "")
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "") or OIDC_PROVIDER_URL
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CORS_ALLOWED_ORIGIN = os.environ.get("OIDC_CORS_ALLOWED_ORIGIN", "")
OIDC_CORS_ALLOWED_ORIGIN_ALT = os.environ.get("OIDC_CORS_ALLOWED_ORIGIN_ALT", "")

# Paths that do not require authentication.
_PUBLIC_PATHS = frozenset({"/healthz", "/readyz", "/api/jira/webhook"})

# JWKS cache: refreshed when keys are older than this (seconds).
_JWKS_REFRESH_INTERVAL = 300
_jwks_cache: jwk.JWKSet | None = None
_jwks_fetched_at: float = 0


def _is_allowed_origin(origin: str) -> bool:
    """Check whether *origin* matches one of the configured allowed origins."""
    if not origin:
        return False
    return origin in (OIDC_CORS_ALLOWED_ORIGIN, OIDC_CORS_ALLOWED_ORIGIN_ALT)


def _add_cors_headers(
    response: web.StreamResponse,
    origin: str,
) -> None:
    """Add CORS headers to *response* if *origin* is allowed."""
    if _is_allowed_origin(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = "86400"
        response.headers["Vary"] = "Origin"


async def _fetch_jwks() -> jwk.JWKSet:
    """Fetch the JWKS from the OIDC provider."""
    url = f"{OIDC_PROVIDER_URL}/protocol/openid-connect/certs"
    async with (
        aiohttp.ClientSession() as session,
        session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp,
    ):
        resp.raise_for_status()
        data = await resp.text()
    return jwk.JWKSet.from_json(data)


async def _get_jwks() -> jwk.JWKSet:
    """Return cached JWKS, refreshing if stale."""
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()
    if _jwks_cache is None or (now - _jwks_fetched_at) > _JWKS_REFRESH_INTERVAL:
        try:
            _jwks_cache = await _fetch_jwks()
            _jwks_fetched_at = now
            logger.debug("Refreshed JWKS from %s", OIDC_PROVIDER_URL)
        except Exception:
            if _jwks_cache is not None:
                logger.warning("Failed to refresh JWKS, using cached keys", exc_info=True)
            else:
                raise
    return _jwks_cache


def _validate_token(token: str, keyset: jwk.JWKSet) -> dict:
    """Validate a JWT and return its claims.

    Checks the signature, expiration, issuer (must match
    ``OIDC_PROVIDER_URL``), and audience (must match
    ``OIDC_CLIENT_ID`` when configured).

    Raises ``jwt.JWTExpired``, ``ValueError``, or ``Exception`` on failure.
    """
    tok = jwt.JWT(key=keyset, jwt=token)
    claims = json.loads(tok.claims)

    # Issuer must match the configured OIDC provider.
    if claims.get("iss") != OIDC_ISSUER:
        raise ValueError(f"issuer mismatch: expected {OIDC_ISSUER!r}, got {claims.get('iss')!r}")

    # Audience validation (when configured).
    if OIDC_CLIENT_ID:
        aud = claims.get("aud")
        # The ``aud`` claim may be a single string or a list of strings.
        aud_set = {aud} if isinstance(aud, str) else set(aud or [])
        if OIDC_CLIENT_ID not in aud_set:
            raise ValueError(f"audience mismatch: expected {OIDC_CLIENT_ID!r} in {aud_set!r}")

    return claims


@web.middleware
async def oidc_middleware(request: web.Request, handler):
    """aiohttp middleware that enforces OIDC Bearer auth on protected paths."""
    origin = request.headers.get("Origin", "")

    # CORS preflight — return 204 immediately.
    if request.method == "OPTIONS" and request.path.startswith("/api"):
        response = web.Response(status=204)
        _add_cors_headers(response, origin)
        return response

    # Public paths — no auth required.
    if request.path in _PUBLIC_PATHS:
        response = await handler(request)
        _add_cors_headers(response, origin)
        return response

    # When OIDC is not configured, pass through (local dev without auth).
    if not OIDC_PROVIDER_URL:
        response = await handler(request)
        _add_cors_headers(response, origin)
        return response

    # Protected path — require valid Bearer token.
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        resp = web.json_response({"error": "missing or invalid Authorization header"}, status=401)
        _add_cors_headers(resp, origin)
        return resp

    token = auth_header[len("Bearer ") :]
    try:
        keyset = await _get_jwks()
        claims = _validate_token(token, keyset)
    except Exception:
        logger.debug("Token validation failed", exc_info=True)
        resp = web.json_response({"error": "invalid or expired token"}, status=401)
        _add_cors_headers(resp, origin)
        return resp

    # Attach user identity to the request for downstream handlers.
    request["remote_user"] = (
        claims.get("preferred_username") or claims.get("email") or claims.get("sub", "unknown")
    )

    response = await handler(request)
    _add_cors_headers(response, origin)
    return response
