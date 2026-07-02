from unittest.mock import AsyncMock, patch

import pytest

from ymir.agents.tasks import (
    _CONSOLIDATION_HASH_KEY as HASH_KEY,
)
from ymir.agents.tasks import (
    InvalidConsolidationConfigError,
    complete_job,
    fetch_consolidation_config,
    pick_next_job,
    submit_merge_job,
)
from ymir.agents.tasks import (
    _consolidation_field_key as _field_key,
)
from ymir.common.models import MergeConsolidationJob


class FakeRedis:
    """Minimal in-memory Redis mock for hash operations and Lua eval."""

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
        """Simulate the pick-next-job Lua script atomically."""
        hash_key = args[0]
        bucket = self._data.get(hash_key, {})
        for field, value in list(bucket.items()):
            field_str = field.decode() if isinstance(field, bytes) else field
            if not field_str.endswith(":pending"):
                continue
            prefix = field_str.removesuffix(":pending")
            active_key = f"{prefix}:active"
            if active_key not in bucket:
                del bucket[field]
                bucket[active_key] = value
                return [field.encode() if isinstance(field, str) else field, value]
        return None


@pytest.fixture
def fake_redis():
    return FakeRedis()


# -- submit_merge_job ---------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_new_job(fake_redis):
    result = await submit_merge_job(fake_redis, "bash", "c10s")
    assert result is True
    pending_key = _field_key("bash", "c10s", "pending")
    assert await fake_redis.hget(HASH_KEY, pending_key) is not None


@pytest.mark.asyncio
async def test_submit_duplicate_pending_noop(fake_redis):
    assert await submit_merge_job(fake_redis, "bash", "c10s") is True
    assert await submit_merge_job(fake_redis, "bash", "c10s") is False


@pytest.mark.asyncio
async def test_submit_while_active_creates_pending(fake_redis):
    active_key = _field_key("bash", "c10s", "active")
    job = MergeConsolidationJob(package="bash", target_branch="c10s", active=True)
    await fake_redis.hset(HASH_KEY, active_key, job.model_dump_json())

    result = await submit_merge_job(fake_redis, "bash", "c10s")
    assert result is True


@pytest.mark.asyncio
async def test_submit_while_active_and_pending_noop(fake_redis):
    active_key = _field_key("bash", "c10s", "active")
    pending_key = _field_key("bash", "c10s", "pending")
    job_a = MergeConsolidationJob(package="bash", target_branch="c10s", active=True)
    job_p = MergeConsolidationJob(package="bash", target_branch="c10s", active=False)
    await fake_redis.hset(HASH_KEY, active_key, job_a.model_dump_json())
    await fake_redis.hset(HASH_KEY, pending_key, job_p.model_dump_json())

    result = await submit_merge_job(fake_redis, "bash", "c10s")
    assert result is False


@pytest.mark.asyncio
async def test_different_packages_independent(fake_redis):
    assert await submit_merge_job(fake_redis, "bash", "c10s") is True
    assert await submit_merge_job(fake_redis, "curl", "c10s") is True


# -- pick_next_job ------------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_from_empty_returns_none(fake_redis):
    result = await pick_next_job(fake_redis)
    assert result is None


@pytest.mark.asyncio
async def test_pick_promotes_pending_to_active(fake_redis):
    await submit_merge_job(fake_redis, "bash", "c10s")

    job = await pick_next_job(fake_redis)
    assert job is not None
    assert job.package == "bash"
    assert job.target_branch == "c10s"
    assert job.active is True

    pending_key = _field_key("bash", "c10s", "pending")
    assert await fake_redis.hget(HASH_KEY, pending_key) is None

    active_key = _field_key("bash", "c10s", "active")
    assert await fake_redis.hget(HASH_KEY, active_key) is not None


@pytest.mark.asyncio
async def test_pick_skips_if_active_exists(fake_redis):
    active_key = _field_key("bash", "c10s", "active")
    job = MergeConsolidationJob(package="bash", target_branch="c10s", active=True)
    await fake_redis.hset(HASH_KEY, active_key, job.model_dump_json())

    pending_key = _field_key("bash", "c10s", "pending")
    pending = MergeConsolidationJob(package="bash", target_branch="c10s", active=False)
    await fake_redis.hset(HASH_KEY, pending_key, pending.model_dump_json())

    result = await pick_next_job(fake_redis)
    assert result is None


# -- complete_job -------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_removes_active(fake_redis):
    active_key = _field_key("bash", "c10s", "active")
    job = MergeConsolidationJob(package="bash", target_branch="c10s", active=True)
    await fake_redis.hset(HASH_KEY, active_key, job.model_dump_json())

    await complete_job(fake_redis, "bash", "c10s")
    assert await fake_redis.hget(HASH_KEY, active_key) is None


@pytest.mark.asyncio
async def test_complete_leaves_pending_intact(fake_redis):
    active_key = _field_key("bash", "c10s", "active")
    pending_key = _field_key("bash", "c10s", "pending")
    job_a = MergeConsolidationJob(package="bash", target_branch="c10s", active=True)
    job_p = MergeConsolidationJob(package="bash", target_branch="c10s", active=False)
    await fake_redis.hset(HASH_KEY, active_key, job_a.model_dump_json())
    await fake_redis.hset(HASH_KEY, pending_key, job_p.model_dump_json())

    await complete_job(fake_redis, "bash", "c10s")
    assert await fake_redis.hget(HASH_KEY, active_key) is None
    assert await fake_redis.hget(HASH_KEY, pending_key) is not None


# -- full cycle ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_cycle_submit_pick_complete_repeat(fake_redis):
    await submit_merge_job(fake_redis, "bash", "c10s")
    job = await pick_next_job(fake_redis)
    assert job is not None

    await submit_merge_job(fake_redis, "bash", "c10s")

    await complete_job(fake_redis, "bash", "c10s")

    job2 = await pick_next_job(fake_redis)
    assert job2 is not None
    assert job2.package == "bash"

    await complete_job(fake_redis, "bash", "c10s")

    job3 = await pick_next_job(fake_redis)
    assert job3 is None


# -- fetch_consolidation_config -----------------------------------------------


@pytest.mark.asyncio
async def test_fetch_config_returns_default_when_not_found():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "No maintainer rules found for package 'bash' (file 'ymir.yaml' not found)"
        config = await fetch_consolidation_config("bash", [])

    assert config.merge_mrs is True
    assert config.release_strategy.value == "per_commit"


@pytest.mark.asyncio
async def test_fetch_config_parses_valid_yaml():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "consolidation:\n  merge_mrs: false\n  release_strategy: merged\n"
        config = await fetch_consolidation_config("bash", [])

    assert config.merge_mrs is False
    assert config.release_strategy.value == "merged"


@pytest.mark.asyncio
async def test_fetch_config_returns_default_on_exception():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = RuntimeError("network error")
        config = await fetch_consolidation_config("bash", [])

    assert config.merge_mrs is True


@pytest.mark.asyncio
async def test_fetch_config_raises_on_malformed_yaml():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "consolidation:\n  merge_mrs: not_a_bool\n"
        with pytest.raises(InvalidConsolidationConfigError, match="malformed"):
            await fetch_consolidation_config("bash", [])


@pytest.mark.asyncio
async def test_fetch_config_raises_on_invalid_yaml_syntax():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "consolidation:\n  merge_mrs: [\n"
        with pytest.raises(InvalidConsolidationConfigError, match="not valid YAML"):
            await fetch_consolidation_config("bash", [])


@pytest.mark.asyncio
async def test_fetch_config_returns_default_when_no_consolidation_key():
    with patch("ymir.agents.tasks.run_tool", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "some_other_setting: true\n"
        config = await fetch_consolidation_config("bash", [])

    assert config.merge_mrs is True
    assert config.release_strategy.value == "per_commit"
