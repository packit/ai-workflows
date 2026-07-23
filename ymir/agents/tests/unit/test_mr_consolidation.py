import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from ymir.agents.mr_consolidation_agent import (
    _collect_footers_from_branches,
    _extract_cves_from_cve_footer_lines,
    _extract_jira_issues_from_resolves_footer_lines,
    _extract_resolves_from_commit,
    _files_to_stage_for_patches,
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


# -- _extract_cves_from_cve_footer_lines --------------------------------------


class TestExtractCvesFromFooterLines:
    def test_single_cve_footer(self):
        assert _extract_cves_from_cve_footer_lines("CVE: CVE-2026-12617") == ["CVE-2026-12617"]

    def test_multi_cve_footer(self):
        text = "CVE: CVE-2026-11331, CVE-2026-13204"
        assert _extract_cves_from_cve_footer_lines(text) == [
            "CVE-2026-11331",
            "CVE-2026-13204",
        ]

    def test_ignores_cves_outside_footer(self):
        text = (
            "Fix named crash on CNAME/DNAME queries (CVE-2026-12617)\n\n"
            "Backport CVE-2026-12617 fix from upstream.\n"
            "pattern of backporting (e.g. bind-9.18-CVE-2024-4076.patch, "
            "bind-9.18-CVE-2024-11187.patch).\n\n"
            "CVE: CVE-2026-12617\n"
            "Resolves: RHEL-213450\n"
        )
        assert _extract_cves_from_cve_footer_lines(text) == ["CVE-2026-12617"]

    def test_no_footer_returns_empty(self):
        assert _extract_cves_from_cve_footer_lines("No vulnerabilities here") == []
        assert (
            _extract_cves_from_cve_footer_lines(
                "Added bind-9.18-CVE-2024-4076.patch",
            )
            == []
        )

    def test_deduplication(self):
        text = "CVE: CVE-2024-1111\nCVE: CVE-2024-1111"
        assert _extract_cves_from_cve_footer_lines(text) == ["CVE-2024-1111"]

    def test_cve_in_commit_message_format(self):
        text = "Rebuild gnutls\n\nCVE: CVE-2024-55555\nResolves: RHEL-12345"
        assert _extract_cves_from_cve_footer_lines(text) == ["CVE-2024-55555"]


# -- _extract_jira_issues_from_resolves_footer_lines ---------------------------


class TestExtractJiraFromResolvesFooterLines:
    def test_single_resolves(self):
        assert _extract_jira_issues_from_resolves_footer_lines(
            "Resolves: RHEL-213450",
        ) == ["RHEL-213450"]

    def test_multi_resolves(self):
        assert _extract_jira_issues_from_resolves_footer_lines(
            "Resolves: RHEL-1, RHEL-2",
        ) == ["RHEL-1", "RHEL-2"]

    def test_related_line(self):
        assert _extract_jira_issues_from_resolves_footer_lines(
            "Related: RHEL-99999",
        ) == ["RHEL-99999"]

    def test_ignores_prose_without_footer(self):
        text = (
            "Verified that RHEL-159046 was already closed.\n"
            "The gnutls package (RHEL-154320) already shipped this.\n"
        )
        assert _extract_jira_issues_from_resolves_footer_lines(text) == []

    def test_ignores_prose_keeps_footer(self):
        text = (
            "Fix CVE-2026-12617 for bind\n\n"
            "Mentioning RHEL-99999 in triage only.\n\n"
            "CVE: CVE-2026-12617\n"
            "Resolves: RHEL-213450\n"
        )
        assert _extract_jira_issues_from_resolves_footer_lines(text) == ["RHEL-213450"]


# -- _collect_footers_from_branches --------------------------------------------


class TestCollectFootersFromBranches:
    """Failure handling when git commands fail during footer collection."""

    @pytest.mark.asyncio
    async def test_raises_when_rev_list_fails(self, tmp_path):
        async def fake_run(cmd, **kwargs):
            return 128, None, "fatal: bad revision 'missing'"

        with (
            patch(
                "ymir.agents.mr_consolidation_agent.run_subprocess",
                new=AsyncMock(side_effect=fake_run),
            ),
            pytest.raises(RuntimeError, match=r"git rev-list.*failed"),
        ):
            await _collect_footers_from_branches(
                tmp_path,
                None,
                "rhel-9.8.0",
                ["mr-branch"],
            )

    @pytest.mark.asyncio
    async def test_raises_when_git_log_fails(self, tmp_path):
        async def fake_run(cmd, **kwargs):
            if cmd[1] == "rev-list":
                return 0, "abc123def456\n", None
            return 128, None, "fatal: bad object abc123def456"

        with (
            patch(
                "ymir.agents.mr_consolidation_agent.run_subprocess",
                new=AsyncMock(side_effect=fake_run),
            ),
            pytest.raises(RuntimeError, match=r"git log -1.*failed"),
        ):
            await _collect_footers_from_branches(
                tmp_path,
                None,
                "rhel-9.8.0",
                ["mr-branch"],
            )

    @pytest.mark.asyncio
    async def test_collects_footers_from_commit_messages(self, tmp_path):
        async def fake_run(cmd, **kwargs):
            if cmd[1] == "rev-list":
                return 0, "abc123\n", None
            return (
                0,
                "Fix something\n\nCVE: CVE-2026-11111\nResolves: RHEL-12345\n",
                None,
            )

        with patch(
            "ymir.agents.mr_consolidation_agent.run_subprocess",
            new=AsyncMock(side_effect=fake_run),
        ):
            cves, jira = await _collect_footers_from_branches(
                tmp_path,
                None,
                "rhel-9.8.0",
                ["mr-branch"],
            )
        assert cves == ["CVE-2026-11111"]
        assert jira == ["RHEL-12345"]


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


# -- _extract_resolves_from_commit ---------------------------------------------


class TestExtractResolvesFromCommit:
    """Tests for extracting Jira keys from a commit's own spec changelog diff."""

    @pytest.fixture
    def git_repo(self, tmp_path):
        """Create a minimal git repo with an initial spec file."""
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        spec = repo / "test.spec"
        spec.write_text(
            "Name: test\nVersion: 1.0\nRelease: 1\n\n"
            "%changelog\n"
            "* Thu Dec 23 2021 Dev <dev@redhat.com> - 1.0-1\n"
            "- Initial build\n"
        )
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        return repo

    def _make_commit(self, repo, spec_content, message="update"):
        import subprocess

        spec = repo / "test.spec"
        spec.write_text(spec_content)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return sha.stdout.strip()

    @pytest.mark.asyncio
    async def test_extracts_resolves_from_commit_diff(self, git_repo):
        sha = self._make_commit(
            git_repo,
            "Name: test\nVersion: 1.0\nRelease: 2\n\n"
            "%changelog\n"
            "* Mon Jul 20 2026 Ymir <ymir@redhat.com> - 1.0-2\n"
            "- Fix CVE-2026-58014\n"
            "- Resolves: RHEL-190609\n"
            "\n"
            "* Thu Dec 23 2021 Dev <dev@redhat.com> - 1.0-1\n"
            "- Initial build\n",
        )
        result = await _extract_resolves_from_commit(sha, "test.spec", git_repo)
        assert result == "RHEL-190609"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_resolves(self, git_repo):
        sha = self._make_commit(
            git_repo,
            "Name: test\nVersion: 1.0\nRelease: 2\n\n"
            "%changelog\n"
            "* Mon Jul 20 2026 Ymir <ymir@redhat.com> - 1.0-2\n"
            "- General bugfix\n"
            "\n"
            "* Thu Dec 23 2021 Dev <dev@redhat.com> - 1.0-1\n"
            "- Initial build\n",
        )
        result = await _extract_resolves_from_commit(sha, "test.spec", git_repo)
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_indented_resolves(self, git_repo):
        sha = self._make_commit(
            git_repo,
            "Name: test\nVersion: 1.0\nRelease: 2\n\n"
            "%changelog\n"
            "* Mon Jul 20 2026 Ymir <ymir@redhat.com> - 1.0-2\n"
            "- Fix CVE-2026-58014\n"
            "  Resolves: RHEL-190609\n"
            "\n"
            "* Thu Dec 23 2021 Dev <dev@redhat.com> - 1.0-1\n"
            "- Initial build\n",
        )
        result = await _extract_resolves_from_commit(sha, "test.spec", git_repo)
        assert result == "RHEL-190609"

    @pytest.mark.asyncio
    async def test_normalizes_lowercase_jira_key(self, git_repo):
        sha = self._make_commit(
            git_repo,
            "Name: test\nVersion: 1.0\nRelease: 2\n\n"
            "%changelog\n"
            "* Mon Jul 20 2026 Ymir <ymir@redhat.com> - 1.0-2\n"
            "- Fix something\n"
            "- Resolves: rhel-190609\n"
            "\n"
            "* Thu Dec 23 2021 Dev <dev@redhat.com> - 1.0-1\n"
            "- Initial build\n",
        )
        result = await _extract_resolves_from_commit(sha, "test.spec", git_repo)
        assert result == "RHEL-190609"

    @pytest.mark.asyncio
    async def test_returns_first_resolves_when_multiple(self, git_repo):
        sha = self._make_commit(
            git_repo,
            "Name: test\nVersion: 1.0\nRelease: 2\n\n"
            "%changelog\n"
            "* Mon Jul 20 2026 Ymir <ymir@redhat.com> - 1.0-2\n"
            "- Fix something\n"
            "- Resolves: RHEL-190609\n"
            "- Related: RHEL-154707\n"
            "\n"
            "* Thu Dec 23 2021 Dev <dev@redhat.com> - 1.0-1\n"
            "- Initial build\n",
        )
        result = await _extract_resolves_from_commit(sha, "test.spec", git_repo)
        assert result == "RHEL-190609"


# -- _files_to_stage_for_patches -----------------------------------------------


class TestFilesToStageForPatches:
    """Tests for staging patch files after LLM renumber/rename."""

    def _write_spec_with_patches(self, repo, patch_filenames: list[str]):
        patch_tags = "\n".join(
            f"Patch{i}:          {name}" for i, name in enumerate(patch_filenames, start=1)
        )
        (repo / "test.spec").write_text(
            "Name: test\nVersion: 1.0\nRelease: 1\n"
            "Summary: Test package\nLicense: MIT\n"
            f"{patch_tags}\n\n"
            "%description\nTest package.\n\n"
            "%changelog\n"
            "* Thu Dec 23 2021 Dev <dev@redhat.com> - 1.0-1\n"
            "- Initial build\n"
        )

    def test_includes_renamed_and_original_patch_names(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "0018-foo.patch").write_text("diff --git a/x b/x\n")
        self._write_spec_with_patches(repo, ["0018-foo.patch"])

        result = _files_to_stage_for_patches(
            repo,
            "test",
            original_patches=["0017-foo.patch"],
        )

        assert result == ["test.spec", "0018-foo.patch", "0017-foo.patch"]

    def test_raises_when_spec_patch_file_missing(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._write_spec_with_patches(repo, ["0018-foo.patch"])

        with pytest.raises(RuntimeError, match="do not exist"):
            _files_to_stage_for_patches(repo, "test")

    def test_stages_spec_and_current_patches_without_originals(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "0017-bar.patch").write_text("diff --git a/x b/x\n")
        self._write_spec_with_patches(repo, ["0017-bar.patch"])

        result = _files_to_stage_for_patches(repo, "test")

        assert result == ["test.spec", "0017-bar.patch"]
