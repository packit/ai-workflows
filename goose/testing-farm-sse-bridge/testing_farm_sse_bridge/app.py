#!/usr/bin/env python3
"""
Testing Farm SSE Bridge

Usage:
    export TESTING_FARM_API_TOKEN=xxxx
    # With uv
    uv run testing-farm-sse-bridge --host 0.0.0.0 --port 10000 --log-level info
    # Or with uvicorn
    uvicorn testing_farm_sse_bridge.app:app --host 0.0.0.0 --port 10000 --log-level info

Clients control stream lifetime using the 'until' query parameter:
  - /v0.1/requests/stream?state=queued&until=closed     (default; stream indefinitely)
  - /v0.1/requests/stream?state=running&until=complete   (close when all requests are finished)

Exit codes:
  1 = missing token
  2 = failed token validation
  3 = unexpected fatal error during startup

OpenAPI (excerpt):
openapi: 3.1.0
info:
  title: Testing Farm SSE Bridge
  version: '0.1.0'
servers:
  - url: http://127.0.0.1:10000
paths:
  /v0.1/requests/stream:
    get:
      summary: Stream Testing Farm requests via SSE
      description: |
        Streams server-sent events (SSE) with an initial snapshot followed by deltas and periodic pings.
        Any query parameters other than 'until' and 'id' are forwarded upstream to Testing Farm's /v0.1/requests.
        If 'until=complete' is used, an 'all_complete' event is emitted just before the server closes the stream.
        Error events include a 'ts' field for correlation.
      parameters:
        - in: query
          name: until
          required: false
          schema:
            type: string
            enum: [closed, complete]
            default: closed
          description: |
            closed: stream indefinitely.
            complete: end the SSE stream once all requests are in terminal states.
        - in: query
          name: id
          required: false
          schema:
            type: array
            items:
              type: string
          style: form
          explode: true
          description: |
            Filter results to only include requests with the specified IDs.
            Can be specified multiple times to filter for multiple IDs.
            Example: ?id=request1&id=request2&id=request3
        - in: query
          name: token_id
          required: false
          schema:
            type: string
            format: uuid
          description: |
            Filter requests by token ID. If omitted, the bridge injects the
            token_id derived from the authenticated token via /whoami.
      responses:
        '200':
          description: |
            SSE stream of events. Known event types:
            - snapshot
            - request_created
            - request_updated
            - request_deleted
            - ping
            - error
            - all_complete
          content:
            text/event-stream:
              schema:
                type: string
        '400':
          description: Bad request
        '500':
          description: Internal server error
  /healthz:
    get:
      summary: Liveness probe
      responses:
        '200':
          description: OK
          content:
            application/json:
              schema:
                type: object
                properties:
                  status:
                    type: string
                    example: ok
                required: [status]
"""

import asyncio
import json
import logging
import os
import random
import sys
import time
import uuid
import traceback
from typing import Any, AsyncGenerator, Dict, Optional, Union, cast

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

# -------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------
SNAPSHOT_EVENT = "snapshot"
REQUEST_CREATED_EVENT = "request_created"
REQUEST_UPDATED_EVENT = "request_updated"
REQUEST_DELETED_EVENT = "request_deleted"
PING_EVENT = "ping"
ERROR_EVENT = "error"
ALL_COMPLETE_EVENT = "all_complete"

# Request states
REQUEST_STATE_NEW = 'new'
REQUEST_STATE_QUEUED = 'queued'
REQUEST_STATE_RUNNING = 'running'
REQUEST_STATE_ERROR = 'error'
REQUEST_STATE_COMPLETE = 'complete'
REQUEST_STATE_CANCEL_REQUESTED = 'cancel-requested'
REQUEST_STATE_CANCELED = 'canceled'

TESTING_FARM_API_URL: str = os.environ.get("TESTING_FARM_API_URL", "https://api.testing-farm.io")
API_TOKEN: Optional[str] = os.environ.get("TESTING_FARM_API_TOKEN")
POLL_INTERVAL_SECONDS: float = float(os.environ.get("TESTING_FARM_POLL_INTERVAL", "5.0"))
PING_INTERVAL_SECONDS: float = 30.0
REQUEST_TIMEOUT_SECONDS: float = float(os.environ.get("TESTING_FARM_TIMEOUT", "30.0"))

# Terminal states
TERMINAL_STATES: set[str] = {
    REQUEST_STATE_ERROR,
    REQUEST_STATE_COMPLETE,
    REQUEST_STATE_CANCELED,
}

# -------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("testing-farm-sse-bridge")


def _mask_token(value: Optional[str]) -> Optional[str]:
    if not value or not API_TOKEN:
        return value
    return value.replace(API_TOKEN, "<redacted>")


class SecretMaskingFilter(logging.Filter):
    """Filter that masks sensitive tokens in log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask sensitive tokens in the log record's message and arguments."""
        try:
            if API_TOKEN:
                # LogRecord.msg can be str or any other type
                if isinstance(record.msg, str):
                    record.msg = cast(str, _mask_token(record.msg))
                # Handle both tuple and dict args
                if record.args:
                    if isinstance(record.args, tuple):
                        record.args = tuple(_mask_token(a) if isinstance(a, str) else a for a in record.args)
                    elif isinstance(record.args, dict):
                        record.args = {k: _mask_token(v) if isinstance(v, str) else v for k, v in record.args.items()}
        except (ValueError, AttributeError, TypeError):
            # Do not break logging on masking errors
            pass
        return True


# Attach masking filter to root and module logger
logging.getLogger().addFilter(SecretMaskingFilter())
logger.addFilter(SecretMaskingFilter())

# -------------------------------------------------------------------
# Globals
# -------------------------------------------------------------------
app = FastAPI()
bearer_token: Optional[str] = None
bearer_token_lock: asyncio.Lock = asyncio.Lock()
shutdown_event: asyncio.Event = asyncio.Event()

# -------------------------------------------------------------------
# Helper Functions
# -------------------------------------------------------------------

def format_sse_event(event_type: str, data: Any, event_id: Optional[str] = None) -> str:
    """Format a proper SSE event line."""
    sse = ""
    if event_id:
        sse += f"id: {event_id}\n"
    sse += f"event: {event_type}\n"
    sse += f"data: {json.dumps(data)}\n\n"
    return sse


def log_deltas(connection_id: str, created: int, updated: int, deleted: int) -> None:
    """Log request state changes."""
    if created or updated or deleted:
        logger.info("[%s] Deltas: created=%d updated=%d deleted=%d", connection_id, created, updated, deleted)


def is_terminal_state(state: Optional[str]) -> bool:
    """Check if a request state is terminal."""
    if state is None:
        return False
    return state.lower() in TERMINAL_STATES


# -------------------------------------------------------------------
# Token Management
# -------------------------------------------------------------------

async def fetch_bearer_token() -> str:
    """Validate API token with /whoami."""
    if not API_TOKEN:
        logger.error("Missing TESTING_FARM_API_TOKEN")
        sys.exit(1)
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            headers = {"Authorization": f"Bearer {API_TOKEN}"}
            resp = await client.get(f"{TESTING_FARM_API_URL}/v0.1/whoami", headers=headers)
            resp.raise_for_status()
            logger.info("Token validated successfully")
            return API_TOKEN  # type: ignore[return-value]
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        logger.error("Token validation failed with HTTP status: %s", status)
        sys.exit(2)
    except (httpx.RequestError, httpx.HTTPError) as e:
        logger.error("Token validation failed: %s", e.__class__.__name__)
        sys.exit(2)


async def get_bearer_token() -> str:
    """Get a cached bearer token or fetch a new one."""
    global bearer_token
    async with bearer_token_lock:
        if bearer_token is None:
            bearer_token = await fetch_bearer_token()
        return bearer_token


async def fetch_token_id_for_auth_header(auth_header_value: str) -> Optional[str]:
    """Fetch token_id via /whoami using the provided Authorization header."""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{TESTING_FARM_API_URL}/v0.1/whoami",
                headers={"Authorization": auth_header_value},
            )
            resp.raise_for_status()
            try:
                meta: Any = resp.json()
                token_obj = meta.get("token") if isinstance(meta, dict) else None
                token_id_val = token_obj.get("id") if isinstance(token_obj, dict) else None
                if isinstance(token_id_val, str) and token_id_val:
                    return token_id_val
            except json.JSONDecodeError:
                return None
    except (httpx.HTTPError, ValueError):
        return None
    return None

# -------------------------------------------------------------------
# Polling Helpers
# -------------------------------------------------------------------

async def fetch_requests(client: httpx.AsyncClient, params: Dict[str, Any], headers: Dict[str, str]) -> list[Dict[str, Any]]:
    """Fetch and normalize Testing Farm requests."""
    # Build request to capture the final encoded URL for logging/debugging
    req = client.build_request("GET", f"{TESTING_FARM_API_URL}/v0.1/requests", params=params, headers=headers)
    logger.info(f"Upstream GET {req.url}")
    resp = await client.send(req)
    resp.raise_for_status()

    try:
        data: Any = resp.json()
    except json.JSONDecodeError:
        return []
    if data is None:
        return []

    # Only accept the canonical list shape for /v0.1/requests
    if isinstance(data, list):
        return data

    # Any other JSON type or envelope → empty
    return []


def emit_snapshot(requests: list[Dict[str, Any]]) -> str:
    """Emit a snapshot event with all current requests."""
    return format_sse_event(SNAPSHOT_EVENT, requests)


def emit_deltas(prev: Dict[str, str], curr: list[Dict[str, Any]], conn_id: str) -> list[str]:
    """Calculate and emit delta events between previous and current state."""
    out, created, updated, deleted = [], 0, 0, 0
    curr_ids = {r["id"] for r in curr}
    for r in curr:
        if r["id"] not in prev:
            out.append(format_sse_event(REQUEST_CREATED_EVENT, r, r["id"]))
            prev[r["id"]] = r["state"]
            created += 1
        elif prev[r["id"]] != r["state"]:
            out.append(format_sse_event(REQUEST_UPDATED_EVENT, r, r["id"]))
            prev[r["id"]] = r["state"]
            updated += 1
    for rid in list(prev.keys()):
        if rid not in curr_ids:
            out.append(format_sse_event(REQUEST_DELETED_EVENT, {"id": rid}, rid))
            prev.pop(rid)
            deleted += 1
    log_deltas(conn_id, created, updated, deleted)
    return out


def emit_ping() -> str:
    """Emit a ping event with current timestamp."""
    return format_sse_event(PING_EVENT, {"ts": int(time.time())})


def emit_error(err_type: str, detail: str, trace: Optional[str] = None) -> str:
    """Emit an error event with optional traceback."""
    payload: Dict[str, Any] = {"ts": int(time.time()), "type": err_type, "detail": _mask_token(detail) or detail}
    if trace:
        # Limit trace size to avoid overwhelming clients
        masked_trace = _mask_token(trace) or trace
        payload["trace"] = masked_trace[-4000:]
    return format_sse_event(ERROR_EVENT, payload)


# -------------------------------------------------------------------
# Poll Loop
# -------------------------------------------------------------------

async def poll_requests(params: Dict[str, Any], conn_id: str, until_mode: str, request_ids: Optional[list[str]] = None, headers: Optional[Dict[str, str]] = None) -> AsyncGenerator[str, None]:
    """Poll Testing Farm requests and emit SSE events for changes."""
    seen: Dict[str, str] = {}
    snapshot_sent = False
    last_ping = time.monotonic()
    backoff = POLL_INTERVAL_SECONDS
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        while not shutdown_event.is_set():
            try:
                effective_headers = headers or {"Authorization": f"Bearer {await get_bearer_token()}"}
                requests_list = await fetch_requests(client, params, effective_headers)
                # Local filtering when client specified one or more ids
                if request_ids:
                    requests_list = [r for r in requests_list if r.get("id") in request_ids]
                logger.info("[%s] Poll OK: %d requests", conn_id, len(requests_list))
                backoff = POLL_INTERVAL_SECONDS
            except httpx.HTTPStatusError as e:
                logger.exception("[%s] HTTP error while polling", conn_id)
                status = e.response.status_code if e.response is not None else "unknown"
                yield emit_error("http", f"status={status}", traceback.format_exc())
                await asyncio.sleep(backoff + random.uniform(0, 1))
                backoff = min(backoff * 2, 60.0)
                continue
            except httpx.RequestError:
                logger.exception("[%s] Network error while polling", conn_id)
                yield emit_error("network", "request_error", traceback.format_exc())
                await asyncio.sleep(backoff + random.uniform(0, 1))
                backoff = min(backoff * 2, 60.0)
                continue
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
                logger.exception("[%s] Data error while polling", conn_id)
                yield emit_error("data", str(e), traceback.format_exc())
                await asyncio.sleep(backoff + random.uniform(0, 1))
                backoff = min(backoff * 2, 60.0)
                continue

            if not snapshot_sent:
                yield emit_snapshot(requests_list)
                for r in requests_list:
                    seen[r["id"]] = r["state"]
                snapshot_sent = True
            else:
                for evt in emit_deltas(seen, requests_list, conn_id):
                    yield evt

            # If until=complete, close this stream once no active (non-terminal) requests remain
            if until_mode == "complete":
                active = [r for r in requests_list if not is_terminal_state(r.get("state"))]
                if not active:
                    # Inform clients explicitly before closing the stream
                    yield format_sse_event(
                        ALL_COMPLETE_EVENT,
                        {"ts": int(time.time()), "requests": [r["id"] for r in requests_list]},
                    )
                    logger.info("[%s] until=complete satisfied: closing stream", conn_id)
                    break

            if time.monotonic() - last_ping > PING_INTERVAL_SECONDS:
                yield emit_ping()
                last_ping = time.monotonic()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

# -------------------------------------------------------------------
# FastAPI Routes
# -------------------------------------------------------------------

@app.on_event("startup")
async def startup_event() -> None:
    """Initialize the application on startup."""
    try:
        # Validate environment token if present
        if API_TOKEN:
            _ = await fetch_bearer_token()
    except SystemExit:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
        logger.error("Unexpected error in startup: %s", e.__class__.__name__)
        sys.exit(3)


@app.on_event("shutdown")
async def shutdown() -> None:
    """Clean up on application shutdown."""
    shutdown_event.set()


@app.get("/v0.1/requests/stream")
async def stream_requests(request: Request) -> StreamingResponse:
    """Stream Testing Farm requests via SSE."""
    params = dict(request.query_params)
    # Pull bridge-specific control parameter and avoid forwarding it upstream
    until_mode = params.pop("until", "closed")
    # Extract and remove repeated id params so we do not forward them upstream
    request_ids = request.query_params.getlist("id")
    if "id" in params:
        params.pop("id")

    # Determine Authorization header to use upstream
    client_auth_header = request.headers.get("Authorization")
    if client_auth_header:
        upstream_headers = {"Authorization": client_auth_header}
    else:
        upstream_headers = {"Authorization": f"Bearer {await get_bearer_token()}"}

    # Inject token_id just-in-time if caller did not provide it
    if "token_id" not in params:
        token_id = await fetch_token_id_for_auth_header(upstream_headers["Authorization"])
        if token_id:
            params["token_id"] = token_id

    conn_id = str(uuid.uuid4())[:8]
    logger.info("[%s] Client connected params=%s until=%s request_ids=%s", conn_id, params, until_mode, request_ids)

    async def event_gen() -> AsyncGenerator[str, None]:
        try:
            async for evt in poll_requests(params, conn_id, until_mode, request_ids, headers=upstream_headers):
                if await request.is_disconnected() or shutdown_event.is_set():
                    break
                yield evt
        finally:
            logger.info("[%s] Client disconnected", conn_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "ok"})

@app.head("/healthz")
async def healthz_head(request: Request):
    return Response(status_code=200)

__all__ = ["app"]
