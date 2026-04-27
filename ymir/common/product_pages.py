"""
Product Pages helpers for RHEL y-stream and z-stream labels.

This module authenticates to the internal Product Pages API (Kerberos via
requests-gssapi) and derives current y-streams, current z-streams, and upcoming
z-streams from active releases and GA/ZStream release metadata.

Public API: ``await fetch_rhel_streams_snapshot()`` (async coroutine). Blocking
HTTP (``requests``) runs in a thread pool so the event loop is not blocked.
Everything else in this module is an implementation detail.
"""

import asyncio
import json
import re
from collections import defaultdict

import requests
import requests_gssapi
from beeai_framework.tools import ToolError

_PLAIN_SHORTNAME_RE = re.compile(r"^rhel-(\d+)\.(\d+)$")
_GA_ZSTREAM_RE = re.compile(r"\(GA\/ZStream\)")

_OIDC_AUTHENTICATE_URL = "https://pp.engineering.redhat.com/oidc/authenticate"
_RELEASES_API_URL = "https://pp.engineering.redhat.com/api/v7/releases/"

# ``requests`` accepts ``(connect, read)`` in seconds. OIDC/GSSAPI can be slow to
# establish; the releases listing can return a large JSON payload.
_PRODUCT_PAGES_TIMEOUT = (30.0, 120.0)


def _rhel_sort_key(shortname: str) -> tuple[int, ...]:
    """Sort key for RHEL shortnames by numeric major.minor (not lexicographic).

    Example: rhel-10.3 sorts after rhel-9.9.

    Returns:
        Tuple of ints for lexical comparison ordering (major, minor, ...).
    """
    body = shortname.removeprefix("rhel-").removesuffix(".z")
    parts = body.split(".")
    return tuple(int(p) for p in parts)


def _parse_plain_rhel_minor(shortname: str) -> tuple[int, int] | None:
    """
    Parse rhel-M.m shortname (optional .z stripped).

    Args:
        shortname: Release shortname such as ``rhel-9.6`` or ``rhel-9.6.z``.

    Returns:
        ``(major, minor)`` or None if the pattern does not match.
    """
    base = shortname.removesuffix(".z")
    m = _PLAIN_SHORTNAME_RE.match(base)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _format_z_label(shortname_or_stem: str) -> str:
    """
    Display form for z-stream maps (e.g. ``rhel-9.6`` -> ``rhel-9.6.z``).

    Args:
        shortname_or_stem: Shortname or stem; ``.z`` is appended when missing.

    Returns:
        Canonical z-stream label string.
    """
    s = shortname_or_stem.strip()
    if s.endswith(".z"):
        return s
    return f"{s}.z"


def _build_current_y_streams(active_releases: list[dict]) -> dict[str, str]:
    """
    Best current y-stream shortname per RHEL major.

    Args:
        active_releases: Active release records (must include ``shortname``).

    Returns:
        Mapping major version string -> highest ``rhel-M.m`` shortname among
        active plain y-style names.
    """
    best: dict[int, tuple[tuple[int, ...], str]] = {}
    for item in active_releases:
        sn = item.get("shortname") or ""
        parsed = _parse_plain_rhel_minor(sn)
        if not parsed:
            continue
        maj, _ = parsed
        key = _rhel_sort_key(sn)
        prev = best.get(maj)
        if prev is None or key > prev[0]:
            best[maj] = (key, sn)
    return {str(m): sn for m, (_, sn) in sorted(best.items())}


def _build_upcoming_z_streams(active_releases: list[dict]) -> dict[str, str]:
    """
    Upcoming z-stream label per major when multiple active streams exist.

    If a major has more than one active release stream, the lower version is
    treated as the upcoming z-stream; otherwise that major is omitted.

    Args:
        active_releases: Active release records (must include ``shortname``).

    Returns:
        Mapping major version string -> upcoming z-stream label (with ``.z``).
    """
    by_major: defaultdict[int, list[str]] = defaultdict(list)
    for item in active_releases:
        sn = item.get("shortname") or ""
        parsed = _parse_plain_rhel_minor(sn)
        if not parsed:
            continue
        maj, _ = parsed
        by_major[maj].append(sn)

    out: dict[str, str] = {}
    for maj in sorted(by_major):
        sns = by_major[maj]
        if len(sns) <= 1:
            continue
        lower = min(sns, key=_rhel_sort_key)
        out[str(maj)] = _format_z_label(lower)
    return out


def _build_current_z_streams_ga_zstream(ga_zstream_rows: list[dict]) -> dict[str, str]:
    """
    Current z-stream labels from GA/ZStream maintenance releases.

    Rows should be releases whose ``name_incl_maint`` matches (GA/ZStream).
    If several exist per major, the highest version is used.

    Args:
        ga_zstream_rows: Filtered release dicts with ``shortname`` set.

    Returns:
        Mapping major version string -> current z-stream label (with ``.z``).
    """
    by_major: defaultdict[int, list[str]] = defaultdict(list)
    for item in ga_zstream_rows:
        sn = item.get("shortname") or ""
        parsed = _parse_plain_rhel_minor(sn)
        if not parsed:
            continue
        maj, _ = parsed
        by_major[maj].append(sn)

    out: dict[str, str] = {}
    for maj in sorted(by_major):
        sns = by_major[maj]
        top = max(sns, key=_rhel_sort_key)
        out[str(maj)] = _format_z_label(top)
    return out


def _require_ok(response: requests.Response, what: str) -> None:
    """Raise ToolError unless *response* is HTTP 200."""
    if response.status_code != 200:
        raise ToolError(f"Product Pages API error ({what}): expected HTTP 200, got {response.status_code}")


def _fetch_rhel_streams_snapshot_sync() -> dict[str, dict[str, str]]:
    """Blocking implementation: HTTP via ``requests`` / GSSAPI."""
    timeout = _PRODUCT_PAGES_TIMEOUT
    try:
        with requests.Session() as s:
            auth = requests_gssapi.HTTPSPNEGOAuth(mutual_authentication=requests_gssapi.OPTIONAL)
            auth_resp = s.post(_OIDC_AUTHENTICATE_URL, auth=auth, timeout=timeout)
            _require_ok(auth_resp, "OIDC authenticate")

            # Multiple active releases per major: lower stream is finishing; higher is main y-stream.
            response_active = s.get(
                _RELEASES_API_URL,
                params={
                    "fields": "shortname",
                    "active": "",
                    "product__shortname": "rhel",
                },
                timeout=timeout,
            )
            _require_ok(response_active, "active releases")
            active_data = response_active.json()

            current_y_streams = _build_current_y_streams(active_data)
            upcoming_z_streams = _build_upcoming_z_streams(active_data)

            response_zstream = s.get(
                _RELEASES_API_URL,
                params={
                    "fields": "shortname,name_incl_maint,name",
                    "product__shortname": "rhel",
                },
                timeout=timeout,
            )
            _require_ok(response_zstream, "releases for z-stream filtering")
            z_data = response_zstream.json()

            fields = [
                "shortname",
                "name_incl_maint",
                "name",
            ]
            filtered = [
                {k: item[k] for k in fields}
                for item in z_data
                if _GA_ZSTREAM_RE.search(item.get("name_incl_maint") or "")
            ]

            current_z_streams = _build_current_z_streams_ga_zstream(filtered)

            return {
                "current_y_streams": current_y_streams,
                "current_z_streams": current_z_streams,
                "upcoming_z_streams": upcoming_z_streams,
            }
    except requests.Timeout as e:
        raise ToolError(
            f"Product Pages API request timed out (connect {timeout[0]}s, read {timeout[1]}s)"
        ) from e
    except requests.RequestException as e:
        raise ToolError(f"Product Pages API network error: {e}") from e
    except json.JSONDecodeError as e:
        raise ToolError(
            "Product Pages API returned a response body that is not valid JSON"
        ) from e
    except ValueError as e:
        raise ToolError(f"Product Pages API response could not be processed: {e}") from e


async def fetch_rhel_streams_snapshot() -> dict[str, dict[str, str]]:
    """
    Query Product Pages and return y-stream and z-stream snapshot maps.

    Uses GSSAPI session authentication, then loads active releases and
    GA/ZStream-filtered releases to compute stream labels.

    Returns:
        Dict with keys ``current_y_streams``, ``current_z_streams``, and
        ``upcoming_z_streams``; each value maps major version strings to
        shortname labels.

    Raises:
        ToolError: On non-success HTTP responses, timeouts, transport errors
            (``requests.RequestException``), invalid JSON, or unexpected response
            shape (``ValueError``).
    """
    return await asyncio.to_thread(_fetch_rhel_streams_snapshot_sync)
