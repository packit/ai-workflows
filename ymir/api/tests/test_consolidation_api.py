"""Unit tests for the consolidation API server."""

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from ymir.api.server import create_app


class FakeRedis:
    """Minimal in-memory Redis mock for hash operations."""

    def __init__(self):
        self._data: dict[str, dict[str, bytes]] = {}

    async def hget(self, name: str, key: str):
        return self._data.get(name, {}).get(key)

    async def hset(self, name: str, key: str, value: str | bytes):
        self._data.setdefault(name, {})[key] = value.encode() if isinstance(value, str) else value

    async def hdel(self, name: str, *keys: str):
        bucket = self._data.get(name, {})
        for k in keys:
            bucket.pop(k, None)

    async def hgetall(self, name: str):
        return dict(self._data.get(name, {}))

    async def eval(self, script: str, num_keys: int, *args):
        """Dispatch to the correct Lua-script simulation."""
        hash_key = args[0]
        if len(args) == 6:
            return self._eval_submit_job(hash_key, args[1], args[2], args[3], args[4], args[5])
        return None

    def _eval_submit_job(self, hash_key, pending_key, active_key, value, mode, recovery_key):
        """Simulate _SUBMIT_JOB_LUA: atomic check-and-set for submission."""
        bucket = self._data.get(hash_key, {})
        pk = pending_key.decode() if isinstance(pending_key, bytes) else pending_key
        ak = active_key.decode() if isinstance(active_key, bytes) else active_key
        rk = recovery_key.decode() if isinstance(recovery_key, bytes) else recovery_key
        m = mode.decode() if isinstance(mode, bytes) else mode
        if pk in bucket:
            return 0
        if m == "strict" and (ak in bucket or any(k == rk or k.startswith(f"{rk}:") for k in bucket)):
            return -1
        self._data.setdefault(hash_key, {})[pk] = value.encode() if isinstance(value, str) else value
        return 1

    async def ping(self):
        return True


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest_asyncio.fixture
async def client(fake_redis):
    app = create_app(redis_conn=fake_redis)
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.mark.asyncio
async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_healthy(client):
    resp = await client.get("/readyz")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_redis_down(client, fake_redis):
    """When Redis is unreachable, /readyz should return 503."""

    async def broken_ping():
        raise ConnectionError("Redis down")

    fake_redis.ping = broken_ping
    resp = await client.get("/readyz")
    assert resp.status == 503
    body = await resp.json()
    assert body["status"] == "not ready"
    assert "redis" in body["reason"]


@pytest.mark.asyncio
async def test_submit_auto_mode(client):
    resp = await client.post(
        "/api/consolidation",
        json={"package": "expat", "target_branch": "rhel-9.8.0"},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["submitted"] is True


@pytest.mark.asyncio
async def test_submit_auto_mode_duplicate(client, fake_redis):
    resp1 = await client.post(
        "/api/consolidation",
        json={"package": "expat", "target_branch": "rhel-9.8.0"},
    )
    assert resp1.status == 201

    resp2 = await client.post(
        "/api/consolidation",
        json={"package": "expat", "target_branch": "rhel-9.8.0"},
    )
    assert resp2.status == 200
    body = await resp2.json()
    assert body["submitted"] is False
    assert body["reason"] == "already_queued"


@pytest.mark.asyncio
async def test_submit_label_triggered(client):
    resp = await client.post(
        "/api/consolidation",
        json={
            "package": "expat",
            "target_branch": "rhel-9.8.0",
            "source_issues": ["RHEL-111", "RHEL-222"],
        },
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["submitted"] is True


@pytest.mark.asyncio
async def test_label_triggered_conflict_pending(client, fake_redis):
    resp1 = await client.post(
        "/api/consolidation",
        json={
            "package": "expat",
            "target_branch": "rhel-9.8.0",
            "source_issues": ["RHEL-111", "RHEL-222"],
        },
    )
    assert resp1.status == 201

    resp2 = await client.post(
        "/api/consolidation",
        json={
            "package": "expat",
            "target_branch": "rhel-9.8.0",
            "source_issues": ["RHEL-333", "RHEL-444"],
        },
    )
    assert resp2.status == 409
    body = await resp2.json()
    assert body["submitted"] is False
    assert body["reason"] == "conflict"


@pytest.mark.asyncio
async def test_label_triggered_conflict_active(client, fake_redis):
    # Simulate an active job directly in Redis
    fake_redis._data.setdefault("merge_consolidation_queue", {})["expat:rhel-9.8.0:active"] = (
        b'{"package":"expat","target_branch":"rhel-9.8.0","active":true}'
    )

    resp = await client.post(
        "/api/consolidation",
        json={
            "package": "expat",
            "target_branch": "rhel-9.8.0",
            "source_issues": ["RHEL-111", "RHEL-222"],
        },
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["reason"] == "conflict"


@pytest.mark.asyncio
async def test_auto_mode_not_blocked_by_active(client, fake_redis):
    """Auto-mode jobs are safe even when an active job exists."""
    fake_redis._data.setdefault("merge_consolidation_queue", {})["expat:rhel-9.8.0:active"] = (
        b'{"package":"expat","target_branch":"rhel-9.8.0","active":true}'
    )

    resp = await client.post(
        "/api/consolidation",
        json={"package": "expat", "target_branch": "rhel-9.8.0"},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["submitted"] is True


@pytest.mark.asyncio
async def test_different_packages_independent(client):
    resp1 = await client.post(
        "/api/consolidation",
        json={"package": "expat", "target_branch": "rhel-9.8.0"},
    )
    assert resp1.status == 201

    resp2 = await client.post(
        "/api/consolidation",
        json={"package": "curl", "target_branch": "rhel-9.8.0"},
    )
    assert resp2.status == 201


@pytest.mark.asyncio
async def test_missing_required_fields(client):
    resp = await client.post(
        "/api/consolidation",
        json={"package": "expat"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_empty_body(client):
    resp = await client.post(
        "/api/consolidation",
        json={},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_invalid_json(client):
    resp = await client.post(
        "/api/consolidation",
        data=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "invalid JSON body"


@pytest.mark.asyncio
async def test_with_release_strategy(client):
    resp = await client.post(
        "/api/consolidation",
        json={
            "package": "expat",
            "target_branch": "rhel-9.8.0",
            "release_strategy": "per_commit",
        },
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["submitted"] is True


@pytest.mark.asyncio
async def test_redis_failure(client, fake_redis):
    """Redis errors during submit_merge_job return 500."""

    async def explode(*args, **kwargs):
        raise ConnectionError("Redis down")

    fake_redis.eval = explode

    resp = await client.post(
        "/api/consolidation",
        json={"package": "expat", "target_branch": "rhel-9.8.0"},
    )
    assert resp.status == 500
    body = await resp.json()
    assert body["error"] == "internal server error"


# -- source_issues cardinality validation -------------------------------------


@pytest.mark.asyncio
async def test_source_issues_zero_rejected(client):
    """An empty source_issues list must be rejected."""
    resp = await client.post(
        "/api/consolidation",
        json={
            "package": "expat",
            "target_branch": "rhel-9.8.0",
            "source_issues": [],
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "validation failed"


@pytest.mark.asyncio
async def test_source_issues_one_rejected(client):
    """A single source_issue must be rejected."""
    resp = await client.post(
        "/api/consolidation",
        json={
            "package": "expat",
            "target_branch": "rhel-9.8.0",
            "source_issues": ["RHEL-111"],
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "validation failed"


@pytest.mark.asyncio
async def test_source_issues_three_rejected(client):
    """More than 2 source_issues must be rejected."""
    resp = await client.post(
        "/api/consolidation",
        json={
            "package": "expat",
            "target_branch": "rhel-9.8.0",
            "source_issues": ["RHEL-111", "RHEL-222", "RHEL-333"],
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "validation failed"
