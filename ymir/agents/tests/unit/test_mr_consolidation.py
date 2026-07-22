import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from ymir.agents.mr_consolidation_agent import (
    _build_cve_to_jira_map,
    _extract_cves_from_text,
    _extract_jira_issues_from_description,
    _fix_changelog_resolves,
    _mr_type_from_labels,
)
from ymir.agents.tasks import (
    _CONSOLIDATION_HASH_KEY as HASH_KEY,
)
from ymir.agents.tasks import (
    InvalidConsolidationConfigError,
    complete_job,
    fetch_consolidation_config,
    pick_next_job,
    submit_merge_job,
    sweep_stale_active_jobs,
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
        """Dispatch to the correct Lua-script simulation based on args.

        pick_next_job: eval(script, 1, hash_key) — 1 arg after num_keys
        conditional HDEL: eval(script, 1, hash_key, field, expected) — 3 args
        """
        hash_key = args[0]
        if len(args) == 1:
            return self._eval_pick_next_job(hash_key)
        if len(args) == 3:
            return self._eval_conditional_hdel(hash_key, args[1], args[2])
        return None

    def _eval_pick_next_job(self, hash_key):
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

    def _eval_conditional_hdel(self, hash_key, field, expected_value):
        bucket = self._data.get(hash_key, {})
        field_str = field.decode() if isinstance(field, bytes) else field
        expected = expected_value.decode() if isinstance(expected_value, bytes) else expected_value
        current = bucket.get(field_str)
        if current is None:
            return 0
        current_str = current.decode() if isinstance(current, bytes) else current
        if current_str == expected:
            del bucket[field_str]
            return 1
        return 0


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
    assert job.activated_at is not None

    pending_key = _field_key("bash", "c10s", "pending")
    assert await fake_redis.hget(HASH_KEY, pending_key) is None

    active_key = _field_key("bash", "c10s", "active")
    stored = await fake_redis.hget(HASH_KEY, active_key)
    assert stored is not None
    stored_job = MergeConsolidationJob.model_validate_json(stored)
    assert stored_job.activated_at is not None


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


# -- _extract_jira_issues_from_description ------------------------------------


def test_extract_from_resolves_line():
    desc = "Some backport description.\n\nResolves: RHEL-154342\n"
    assert _extract_jira_issues_from_description(desc) == ["RHEL-154342"]


def test_extract_from_resolves_line_multiple():
    desc = "Description.\n\nResolves: RHEL-100, RHEL-200, RHEL-300\n"
    result = _extract_jira_issues_from_description(desc)
    assert sorted(result) == ["RHEL-100", "RHEL-200", "RHEL-300"]


def test_extract_from_resolved_jira_issues_section():
    desc = (
        "## Consolidated Backport MR\n\n"
        "### Resolved Jira Issues\n\n"
        "- [RHEL-159051](https://issues.redhat.com/browse/RHEL-159051)\n"
        "- [RHEL-159070](https://issues.redhat.com/browse/RHEL-159070)\n\n"
        "### Source Merge Requests\n\n"
        "Some other text mentioning RHEL-999999\n"
    )
    result = _extract_jira_issues_from_description(desc)
    assert sorted(result) == ["RHEL-159051", "RHEL-159070"]


def test_ignores_issues_in_triage_details():
    desc = (
        "Backport fix for CVE-2026-3833.\n\n"
        "<details>\n"
        "<summary>Triage Details</summary>\n\n"
        "The gnutls RHEL 8 package (RHEL-154320, version 3.6.16) "
        "already shipped this fix.\n"
        "Verified that RHEL-159046 was already closed.\n"
        "</details>\n\n"
        "Resolves: RHEL-154342\n"
    )
    result = _extract_jira_issues_from_description(desc)
    assert result == ["RHEL-154342"]


def test_ignores_issues_in_prose_text():
    desc = (
        "This is related to RHEL-99999 which was fixed upstream.\n"
        "See also RHEL-88888 for context.\n\n"
        "Resolves: RHEL-11111\n"
    )
    result = _extract_jira_issues_from_description(desc)
    assert result == ["RHEL-11111"]


def test_consolidated_mr_with_nested_triage_details():
    """The exact scenario from the production bug: a consolidated MR whose
    sub-MR descriptions contain triage details referencing other issues."""
    desc = (
        "## Consolidated Backport MR\n\n"
        "### Resolved Jira Issues\n\n"
        "- [RHEL-159051](https://issues.redhat.com/browse/RHEL-159051)\n"
        "- [RHEL-159075](https://issues.redhat.com/browse/RHEL-159075)\n\n"
        "### Source Merge Requests\n\n"
        "#### MR 1: [Fix CVE-2026-33845](https://gitlab.com/example/-/merge_requests/1)\n\n"
        "<details><summary>Original description</summary>\n\n"
        "Backport fix for CVE-2026-33845.\n"
        "Verified that the gnutls package (RHEL-159046) was already closed.\n\n"
        "Resolves: RHEL-159051\n"
        "</details>\n\n"
        "#### MR 2: [Fix CVE-2026-3833](https://gitlab.com/example/-/merge_requests/2)\n\n"
        "<details><summary>Original description</summary>\n\n"
        "The gnutls RHEL 8 package (RHEL-154320) already shipped this.\n\n"
        "Resolves: RHEL-159075\n"
        "</details>\n"
    )
    result = _extract_jira_issues_from_description(desc)
    assert sorted(result) == ["RHEL-159051", "RHEL-159075"]


def test_extract_from_related_line():
    desc = "Some description.\n\nRelated: RHEL-12345\n"
    assert _extract_jira_issues_from_description(desc) == ["RHEL-12345"]


def test_empty_description():
    assert _extract_jira_issues_from_description("") == []


def test_no_structured_issues():
    desc = "Just a plain description with no Resolves line."
    assert _extract_jira_issues_from_description(desc) == []


def test_deduplicates_issues():
    desc = (
        "## Consolidated Backport MR\n\n"
        "### Resolved Jira Issues\n\n"
        "- [RHEL-100](https://issues.redhat.com/browse/RHEL-100)\n\n"
        "### Source Merge Requests\n\n"
        "<details><summary>Original description</summary>\n\n"
        "Resolves: RHEL-100\n"
        "</details>\n"
    )
    result = _extract_jira_issues_from_description(desc)
    assert result == ["RHEL-100"]


# -- process_task cancellation behavior ----------------------------------------


@pytest.mark.asyncio
async def test_cancelled_task_does_not_call_complete_job(fake_redis):
    """Regression: on CancelledError (from shutdown), process_task must NOT
    call complete_job() — doing so silently erases the :active hash entry,
    losing the job entirely.  Instead it should leave :active intact and
    re-raise, matching pre-PR SIGTERM behavior (process killed, no cleanup).

    process_task is a closure inside main(), so we can't import it directly.
    We replicate its error-handling structure here as a contract test — if
    the structure in the source file changes, this should be updated too."""
    await submit_merge_job(fake_redis, "bash", "c10s")
    job = await pick_next_job(fake_redis)
    assert job is not None

    active_key = _field_key("bash", "c10s", "active")
    assert await fake_redis.hget(HASH_KEY, active_key) is not None

    mock_complete = AsyncMock()

    async def simulate_workflow():
        raise asyncio.CancelledError

    # Replicate process_task's error-handling structure: CancelledError must
    # propagate without calling complete_job (mock_complete here).
    with pytest.raises(asyncio.CancelledError):
        try:
            await simulate_workflow()
        except asyncio.CancelledError:
            raise
        except Exception:
            await mock_complete(fake_redis, job.package, job.target_branch)
        else:
            await mock_complete(fake_redis, job.package, job.target_branch)

    mock_complete.assert_not_called()
    assert await fake_redis.hget(HASH_KEY, active_key) is not None


# -- sweep_stale_active_jobs ---------------------------------------------------


def _make_active_job(package, branch, activated_hours_ago):
    """Create a MergeConsolidationJob with activated_at set."""
    return MergeConsolidationJob(
        package=package,
        target_branch=branch,
        active=True,
        activated_at=datetime.now(UTC) - timedelta(hours=activated_hours_ago),
    )


@pytest.mark.asyncio
async def test_sweep_removes_stale_active_entry(fake_redis):
    """An :active entry whose activated_at exceeds the threshold is
    removed, unblocking pick_next_job for that package/branch."""
    job = _make_active_job("bash", "c10s", activated_hours_ago=7)
    active_key = _field_key("bash", "c10s", "active")
    await fake_redis.hset(HASH_KEY, active_key, job.model_dump_json())

    removed = await sweep_stale_active_jobs(fake_redis, threshold=timedelta(hours=6))

    assert removed == 1
    assert await fake_redis.hget(HASH_KEY, active_key) is None


@pytest.mark.asyncio
async def test_sweep_leaves_fresh_active_entry(fake_redis):
    """An :active entry whose activated_at is within the threshold must
    not be touched."""
    job = _make_active_job("bash", "c10s", activated_hours_ago=1)
    active_key = _field_key("bash", "c10s", "active")
    await fake_redis.hset(HASH_KEY, active_key, job.model_dump_json())

    removed = await sweep_stale_active_jobs(fake_redis, threshold=timedelta(hours=6))

    assert removed == 0
    assert await fake_redis.hget(HASH_KEY, active_key) is not None


@pytest.mark.asyncio
async def test_sweep_ignores_entry_without_activated_at(fake_redis):
    """An :active entry with no activated_at (promoted before sweep support
    was deployed) is skipped — we can't know when it actually started."""
    job = MergeConsolidationJob(
        package="bash",
        target_branch="c10s",
        active=True,
        submitted_at=datetime.now(UTC) - timedelta(hours=24),
    )
    active_key = _field_key("bash", "c10s", "active")
    await fake_redis.hset(HASH_KEY, active_key, job.model_dump_json())

    removed = await sweep_stale_active_jobs(fake_redis, threshold=timedelta(hours=6))

    assert removed == 0
    assert await fake_redis.hget(HASH_KEY, active_key) is not None


@pytest.mark.asyncio
async def test_sweep_does_not_touch_pending(fake_redis):
    """:pending entries must never be removed by the sweep, regardless of age."""
    old_time = datetime.now(UTC) - timedelta(hours=24)
    job = MergeConsolidationJob(
        package="bash",
        target_branch="c10s",
        active=False,
        submitted_at=old_time,
    )
    pending_key = _field_key("bash", "c10s", "pending")
    await fake_redis.hset(HASH_KEY, pending_key, job.model_dump_json())

    removed = await sweep_stale_active_jobs(fake_redis, threshold=timedelta(hours=6))

    assert removed == 0
    assert await fake_redis.hget(HASH_KEY, pending_key) is not None


@pytest.mark.asyncio
async def test_sweep_unblocks_pending_promotion(fake_redis):
    """After sweeping a stale :active entry, pick_next_job should be able
    to promote a :pending entry for the same package/branch."""
    active_job = _make_active_job("bash", "c10s", activated_hours_ago=10)
    active_key = _field_key("bash", "c10s", "active")
    await fake_redis.hset(HASH_KEY, active_key, active_job.model_dump_json())

    await submit_merge_job(fake_redis, "bash", "c10s")

    # Before sweep: pick_next_job can't promote because :active blocks it
    assert await pick_next_job(fake_redis) is None

    await sweep_stale_active_jobs(fake_redis, threshold=timedelta(hours=6))

    # After sweep: :active is gone, pick_next_job can promote
    job = await pick_next_job(fake_redis)
    assert job is not None
    assert job.package == "bash"
    assert job.target_branch == "c10s"


@pytest.mark.asyncio
async def test_sweep_handles_unparseable_entry(fake_redis):
    """If an :active entry has corrupted JSON, the sweep logs a warning
    and skips it rather than crashing."""
    active_key = _field_key("bash", "c10s", "active")
    await fake_redis.hset(HASH_KEY, active_key, b"not-valid-json")

    removed = await sweep_stale_active_jobs(fake_redis, threshold=timedelta(hours=1))

    assert removed == 0
    assert await fake_redis.hget(HASH_KEY, active_key) is not None


@pytest.mark.asyncio
async def test_sweep_multiple_packages(fake_redis):
    """Sweep should handle multiple stale entries across different
    package/branch pairs independently."""
    for pkg, hours in [("bash", 10), ("curl", 10), ("gzip", 1)]:
        job = _make_active_job(pkg, "c10s", activated_hours_ago=hours)
        active_key = _field_key(pkg, "c10s", "active")
        await fake_redis.hset(HASH_KEY, active_key, job.model_dump_json())

    removed = await sweep_stale_active_jobs(fake_redis, threshold=timedelta(hours=6))

    assert removed == 2
    assert await fake_redis.hget(HASH_KEY, _field_key("bash", "c10s", "active")) is None
    assert await fake_redis.hget(HASH_KEY, _field_key("curl", "c10s", "active")) is None
    assert await fake_redis.hget(HASH_KEY, _field_key("gzip", "c10s", "active")) is not None


@pytest.mark.asyncio
async def test_sweep_skips_if_value_changed_since_snapshot(fake_redis):
    """If the :active entry is replaced between HGETALL and the conditional
    HDEL (e.g. complete_job + pick_next_job raced), the sweep must not
    delete the fresh entry."""
    stale_job = _make_active_job("bash", "c10s", activated_hours_ago=10)
    active_key = _field_key("bash", "c10s", "active")
    await fake_redis.hset(HASH_KEY, active_key, stale_job.model_dump_json())

    # Take snapshot (simulating what sweep does internally)
    snapshot_value = await fake_redis.hget(HASH_KEY, active_key)

    # Simulate a race: between HGETALL and HDEL, the old job completes and
    # a fresh one is promoted into the same field
    fresh_job = _make_active_job("bash", "c10s", activated_hours_ago=0)
    await fake_redis.hset(HASH_KEY, active_key, fresh_job.model_dump_json())

    # The conditional HDEL should see the value mismatch and skip
    deleted = await fake_redis.eval("unused", 1, HASH_KEY, active_key.encode(), snapshot_value)
    assert deleted == 0
    assert await fake_redis.hget(HASH_KEY, active_key) is not None


@pytest.mark.asyncio
async def test_sweep_uses_activated_at_not_submitted_at(fake_redis):
    """Regression: staleness must be measured from activated_at, not
    submitted_at.  A job that was queued 8 hours ago but only activated
    1 hour ago must not be swept with a 6-hour threshold."""
    job = MergeConsolidationJob(
        package="bash",
        target_branch="c10s",
        active=True,
        submitted_at=datetime.now(UTC) - timedelta(hours=8),
        activated_at=datetime.now(UTC) - timedelta(hours=1),
    )
    active_key = _field_key("bash", "c10s", "active")
    await fake_redis.hset(HASH_KEY, active_key, job.model_dump_json())

    removed = await sweep_stale_active_jobs(fake_redis, threshold=timedelta(hours=6))

    assert removed == 0
    assert await fake_redis.hget(HASH_KEY, active_key) is not None


# -- _extract_cves_from_text ---------------------------------------------------


class TestExtractCves:
    def test_single_cve(self):
        assert _extract_cves_from_text("Fixed CVE-2024-12345") == ["CVE-2024-12345"]

    def test_multiple_cves(self):
        text = "CVE: CVE-2024-1111, CVE-2024-2222\nAlso fixes CVE-2023-99999"
        result = _extract_cves_from_text(text)
        assert result == ["CVE-2023-99999", "CVE-2024-1111", "CVE-2024-2222"]

    def test_no_cves(self):
        assert _extract_cves_from_text("No vulnerabilities here") == []

    def test_deduplication(self):
        text = "CVE-2024-1111 and CVE-2024-1111 again"
        assert _extract_cves_from_text(text) == ["CVE-2024-1111"]

    def test_cve_in_commit_message_format(self):
        text = "Rebuild gnutls\n\nCVE: CVE-2024-55555\nResolves: RHEL-12345"
        assert _extract_cves_from_text(text) == ["CVE-2024-55555"]


# -- _mr_type_from_labels ------------------------------------------------------


class TestMrTypeFromLabels:
    def test_backport_label(self):
        mr = {"labels": ["ymir_backport"]}
        assert _mr_type_from_labels(mr) == "backport"

    def test_rebuild_label(self):
        mr = {"labels": ["ymir_rebuild"]}
        assert _mr_type_from_labels(mr) == "rebuild"

    def test_both_labels_rebuild_wins(self):
        mr = {"labels": ["ymir_backport", "ymir_rebuild"]}
        assert _mr_type_from_labels(mr) == "rebuild"

    def test_no_labels_defaults_to_backport(self):
        mr = {"labels": []}
        assert _mr_type_from_labels(mr) == "backport"

    def test_none_labels_defaults_to_backport(self):
        mr = {}
        assert _mr_type_from_labels(mr) == "backport"


# -- MR selection priority (backport+backport > backport+rebuild) --------------


class TestMrSelectionPriority:
    """Verify the selection logic prefers backport+backport over backport+rebuild."""

    def _make_mr(self, branch, label):
        return {
            "source_branch": branch,
            "labels": [label],
            "url": f"https://gitlab.com/mr/{branch}",
            "title": f"MR for {branch}",
            "description": f"Resolves: RHEL-{branch[-3:]}",
        }

    def test_two_backports_selected_over_rebuild(self):
        mrs = [
            self._make_mr("bp1", "ymir_backport"),
            self._make_mr("bp2", "ymir_backport"),
            self._make_mr("rb1", "ymir_rebuild"),
        ]
        backport_mrs = [mr for mr in mrs if _mr_type_from_labels(mr) == "backport"]
        rebuild_mrs = [mr for mr in mrs if _mr_type_from_labels(mr) == "rebuild"]

        if len(backport_mrs) >= 2:
            selected = backport_mrs[:2]
        elif backport_mrs and rebuild_mrs:
            selected = [backport_mrs[0], rebuild_mrs[0]]
        else:
            selected = mrs[:2]

        assert all(_mr_type_from_labels(mr) == "backport" for mr in selected)

    def test_one_backport_one_rebuild_selected(self):
        mrs = [
            self._make_mr("bp1", "ymir_backport"),
            self._make_mr("rb1", "ymir_rebuild"),
        ]
        backport_mrs = [mr for mr in mrs if _mr_type_from_labels(mr) == "backport"]
        rebuild_mrs = [mr for mr in mrs if _mr_type_from_labels(mr) == "rebuild"]

        if len(backport_mrs) >= 2:
            selected = backport_mrs[:2]
        elif backport_mrs and rebuild_mrs:
            selected = [backport_mrs[0], rebuild_mrs[0]]
        else:
            selected = mrs[:2]

        assert _mr_type_from_labels(selected[0]) == "backport"
        assert _mr_type_from_labels(selected[1]) == "rebuild"


# -- _build_cve_to_jira_map ---------------------------------------------------


class TestBuildCveToJiraMap:
    def test_maps_cve_from_title_to_jira_from_branch(self):
        mrs = [
            {
                "title": "Fix CVE-2026-58014 in mingw-glib2",
                "source_branch": "automated-package-update-RHEL-190609",
            },
            {
                "title": "Fix CVE-2026-58016 in mingw-glib2",
                "source_branch": "automated-package-update-RHEL-190617",
            },
        ]
        result = _build_cve_to_jira_map(mrs)
        assert result == {
            "CVE-2026-58014": "RHEL-190609",
            "CVE-2026-58016": "RHEL-190617",
        }

    def test_skips_mr_without_jira_in_branch(self):
        mrs = [
            {
                "title": "Fix CVE-2026-58014",
                "source_branch": "some-other-branch",
            },
        ]
        assert _build_cve_to_jira_map(mrs) == {}

    def test_skips_mr_without_cve_in_title(self):
        mrs = [
            {
                "title": "Backport patch for mingw-glib2",
                "source_branch": "automated-package-update-RHEL-190609",
            },
        ]
        assert _build_cve_to_jira_map(mrs) == {}

    def test_multiple_cves_in_title(self):
        mrs = [
            {
                "title": "Fix CVE-2026-1111 and CVE-2026-2222",
                "source_branch": "automated-package-update-RHEL-99999",
            },
        ]
        result = _build_cve_to_jira_map(mrs)
        assert result == {
            "CVE-2026-1111": "RHEL-99999",
            "CVE-2026-2222": "RHEL-99999",
        }

    def test_empty_list(self):
        assert _build_cve_to_jira_map([]) == {}

    def test_missing_fields_handled(self):
        mrs = [
            {},
            {"title": "Fix CVE-2026-1111"},
            {"source_branch": "automated-package-update-RHEL-100"},
        ]
        assert _build_cve_to_jira_map(mrs) == {}

    def test_lowercase_branch_and_title(self):
        mrs = [
            {
                "title": "Fix cve-2026-58014 in mingw-glib2",
                "source_branch": "automated-package-update-rhel-190609",
            },
        ]
        result = _build_cve_to_jira_map(mrs)
        assert result == {"CVE-2026-58014": "RHEL-190609"}

    def test_conflicting_cve_keeps_first(self):
        """When two MRs map the same CVE to different Jira keys,
        the first mapping wins and the conflict is logged."""
        mrs = [
            {
                "title": "Fix CVE-2026-58014 in mingw-glib2",
                "source_branch": "automated-package-update-RHEL-190609",
            },
            {
                "title": "Fix CVE-2026-58014 in mingw-glib2",
                "source_branch": "automated-package-update-RHEL-999999",
            },
        ]
        result = _build_cve_to_jira_map(mrs)
        assert result == {"CVE-2026-58014": "RHEL-190609"}


# -- _fix_changelog_resolves --------------------------------------------------


class TestFixChangelogResolves:
    def _write_spec(self, tmp_path, changelog_lines):
        spec = tmp_path / "test.spec"
        content = "Name: test\nVersion: 1.0\nRelease: 1\n\n%changelog\n" + "\n".join(changelog_lines) + "\n"
        spec.write_text(content)
        return spec

    def test_corrects_mismatched_resolves(self, tmp_path):
        spec = self._write_spec(
            tmp_path,
            [
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-3",
                "- Fix CVE-2026-58014",
                "- Resolves: RHEL-154707",
                "",
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-2",
                "- Fix CVE-2026-58016",
                "- Resolves: RHEL-154707",
            ],
        )
        cve_to_jira = {
            "CVE-2026-58014": "RHEL-190609",
            "CVE-2026-58016": "RHEL-190617",
        }
        assert _fix_changelog_resolves(spec, cve_to_jira) is True

        content = spec.read_text()
        assert "Resolves: RHEL-190609" in content
        assert "Resolves: RHEL-190617" in content
        assert "RHEL-154707" not in content

    def test_leaves_correct_resolves_unchanged(self, tmp_path):
        spec = self._write_spec(
            tmp_path,
            [
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-2",
                "- Fix CVE-2026-58014",
                "- Resolves: RHEL-190609",
            ],
        )
        cve_to_jira = {"CVE-2026-58014": "RHEL-190609"}
        assert _fix_changelog_resolves(spec, cve_to_jira) is False

    def test_no_op_with_empty_map(self, tmp_path):
        spec = self._write_spec(
            tmp_path,
            [
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-2",
                "- Fix CVE-2026-58014",
                "- Resolves: RHEL-154707",
            ],
        )
        assert _fix_changelog_resolves(spec, {}) is False

    def test_no_op_when_cve_not_in_map(self, tmp_path):
        spec = self._write_spec(
            tmp_path,
            [
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-2",
                "- Fix CVE-2026-99999",
                "- Resolves: RHEL-154707",
            ],
        )
        cve_to_jira = {"CVE-2026-58014": "RHEL-190609"}
        assert _fix_changelog_resolves(spec, cve_to_jira) is False

    def test_handles_entry_without_cve(self, tmp_path):
        spec = self._write_spec(
            tmp_path,
            [
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-2",
                "- General bugfix",
                "- Resolves: RHEL-154707",
            ],
        )
        cve_to_jira = {"CVE-2026-58014": "RHEL-190609"}
        assert _fix_changelog_resolves(spec, cve_to_jira) is False

    def test_resets_cve_tracking_at_new_entry_header(self, tmp_path):
        spec = self._write_spec(
            tmp_path,
            [
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-3",
                "- Fix CVE-2026-58014",
                "- Resolves: RHEL-154707",
                "",
                "* Mon Jul 14 2026 Someone <someone@redhat.com> - 1.0-2",
                "- Resolves: RHEL-154707",
            ],
        )
        cve_to_jira = {"CVE-2026-58014": "RHEL-190609"}
        result = _fix_changelog_resolves(spec, cve_to_jira)
        assert result is True

        content = spec.read_text()
        lines = content.splitlines()
        changelog_start = lines.index("%changelog")
        assert "Resolves: RHEL-190609" in lines[changelog_start + 3]
        assert "Resolves: RHEL-154707" in lines[changelog_start + 6]

    def test_replaces_only_first_jira_key_on_resolves_line(self, tmp_path):
        """A Resolves line with multiple Jira keys should only have
        the first key (the one from the Resolves: field) replaced."""
        spec = self._write_spec(
            tmp_path,
            [
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-2",
                "- Fix CVE-2026-58014",
                "- Resolves: RHEL-154707, RHEL-999999",
            ],
        )
        cve_to_jira = {"CVE-2026-58014": "RHEL-190609"}
        assert _fix_changelog_resolves(spec, cve_to_jira) is True

        content = spec.read_text()
        assert "Resolves: RHEL-190609, RHEL-999999" in content

    def test_multiple_cves_same_mapping_applies(self, tmp_path):
        """When multiple CVEs in one entry all map to the same Jira key,
        the rewrite should proceed."""
        spec = self._write_spec(
            tmp_path,
            [
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-2",
                "- Fix CVE-2026-1111 and CVE-2026-2222",
                "- Resolves: RHEL-154707",
            ],
        )
        cve_to_jira = {
            "CVE-2026-1111": "RHEL-190609",
            "CVE-2026-2222": "RHEL-190609",
        }
        assert _fix_changelog_resolves(spec, cve_to_jira) is True

        content = spec.read_text()
        assert "Resolves: RHEL-190609" in content

    def test_multiple_cves_disagreeing_mappings_skips(self, tmp_path):
        """When multiple CVEs in one entry map to different Jira keys,
        the Resolves line should NOT be rewritten (ambiguous)."""
        spec = self._write_spec(
            tmp_path,
            [
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-2",
                "- Fix CVE-2026-1111 and CVE-2026-2222",
                "- Resolves: RHEL-154707",
            ],
        )
        cve_to_jira = {
            "CVE-2026-1111": "RHEL-190609",
            "CVE-2026-2222": "RHEL-190617",
        }
        assert _fix_changelog_resolves(spec, cve_to_jira) is False

        content = spec.read_text()
        assert "Resolves: RHEL-154707" in content

    def test_corrects_indented_resolves_line(self, tmp_path):
        spec = self._write_spec(
            tmp_path,
            [
                "* Mon Jul 21 2026 Ymir <ymir@redhat.com> - 1.0-2",
                "- Fix CVE-2026-58014",
                "  - Resolves: RHEL-154707",
            ],
        )
        cve_to_jira = {"CVE-2026-58014": "RHEL-190609"}
        assert _fix_changelog_resolves(spec, cve_to_jira) is True

        content = spec.read_text()
        assert "Resolves: RHEL-190609" in content
        assert "RHEL-154707" not in content
