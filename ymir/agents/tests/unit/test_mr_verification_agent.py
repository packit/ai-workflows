import pytest
from flexmock import flexmock

from ymir.agents import tasks as agent_tasks
from ymir.agents.mr_verification_agent import (
    already_reviewed,
    format_review_comment,
    record_reviewed_head,
)
from ymir.agents.tasks import (
    InvalidVerificationConfigError,
    fetch_verification_config,
    try_submit_verification_job,
)
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import (
    MRFindingSeverity,
    MRVerificationFinding,
    MRVerificationOutputSchema,
    MRVerificationVerdict,
    Task,
)


class FakeRedis:
    """Minimal in-memory Redis stand-in for the operations used here."""

    def __init__(self):
        self.lists: dict[str, list[bytes]] = {}
        self.strings: dict[str, bytes] = {}
        self.ttls: dict[str, int | None] = {}

    async def lpush(self, name: str, value: str | bytes):
        payload = value.encode() if isinstance(value, str) else value
        self.lists.setdefault(name, []).insert(0, payload)
        return len(self.lists[name])

    async def set(self, name: str, value: str | bytes, ex: int | None = None):
        self.strings[name] = value.encode() if isinstance(value, str) else value
        self.ttls[name] = ex
        return True

    async def get(self, name: str):
        # Real client is created without decode_responses, so it returns bytes.
        return self.strings.get(name)


# -- queue routing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("branch", "user_triggered", "expected"),
    [
        ("rhel-9.4.0", False, "mr_verification_queue_c9s"),
        ("c9s", False, "mr_verification_queue_c9s"),
        ("rhel-8.10.0", False, "mr_verification_queue_c9s"),
        ("rhel-10.0", False, "mr_verification_queue_c10s"),
        ("c10s", False, "mr_verification_queue_c10s"),
        (None, False, "mr_verification_queue_c10s"),
        ("rhel-9.4.0", True, "mr_verification_queue_c9s_todo"),
        ("rhel-10.0", True, "mr_verification_queue_c10s_todo"),
    ],
)
def test_queue_for_branch(branch, user_triggered, expected):
    assert RedisQueues.get_mr_verification_queue_for_branch(branch, user_triggered) == expected


def test_verification_queues_are_registered():
    """A queue missing from these sets is invisible to the fetcher and the CLI."""
    all_queues = RedisQueues.all_queues()
    for queue in (
        RedisQueues.MR_VERIFICATION_QUEUE_C9S,
        RedisQueues.MR_VERIFICATION_QUEUE_C10S,
        RedisQueues.MR_VERIFICATION_QUEUE_C9S_TODO,
        RedisQueues.MR_VERIFICATION_QUEUE_C10S_TODO,
    ):
        assert queue.value in all_queues
        assert queue.value in RedisQueues.input_queues()
    assert RedisQueues.COMPLETED_MR_VERIFICATION_LIST.value in RedisQueues.data_queues()


# -- fetch_verification_config -------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_config_defaults_to_enabled_when_file_missing():
    async def _mock_run_tool(*_args, **_kwargs):
        return "No maintainer rules found for package 'bash' (file 'ymir.yaml' not found)"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    config = await fetch_verification_config("bash", [])

    assert config.verify_mrs is True
    assert config.block_on_findings is False


@pytest.mark.asyncio
async def test_fetch_config_parses_opt_out():
    async def _mock_run_tool(*_args, **_kwargs):
        return "verification:\n  verify_mrs: false\n  block_on_findings: true\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    config = await fetch_verification_config("bash", [])

    assert config.verify_mrs is False
    assert config.block_on_findings is True


@pytest.mark.asyncio
async def test_fetch_config_ignores_unrelated_sections():
    async def _mock_run_tool(*_args, **_kwargs):
        return "consolidation:\n  merge_mrs: false\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    config = await fetch_verification_config("bash", [])

    assert config.verify_mrs is True


@pytest.mark.asyncio
async def test_fetch_config_raises_on_malformed_section():
    async def _mock_run_tool(*_args, **_kwargs):
        return "verification:\n  verify_mrs: not_a_bool\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    with pytest.raises(InvalidVerificationConfigError, match="malformed"):
        await fetch_verification_config("bash", [])


@pytest.mark.asyncio
async def test_fetch_config_raises_on_invalid_yaml():
    async def _mock_run_tool(*_args, **_kwargs):
        return "verification:\n  verify_mrs: [\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    with pytest.raises(InvalidVerificationConfigError, match="not valid YAML"):
        await fetch_verification_config("bash", [])


# -- try_submit_verification_job -----------------------------------------------


async def _submit(redis, **overrides):
    kwargs = {
        "package": "bash",
        "dist_git_branch": "rhel-9.4.0",
        "merge_request_url": "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/1",
        "jira_issue": "RHEL-1234",
        "source_agent": "Backport",
        "gateway_tools": [],
        "redis_conn": redis,
    }
    kwargs.update(overrides)
    await try_submit_verification_job(**kwargs)


@pytest.mark.asyncio
async def test_submit_enqueues_task_on_the_branch_queue(monkeypatch):
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)

    async def _mock_run_tool(*_args, **_kwargs):
        return "ymir.yaml not found"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    redis = FakeRedis()
    await _submit(redis, cve_id="CVE-2024-0001")

    queued = redis.lists["mr_verification_queue_c9s"]
    assert len(queued) == 1
    task = Task.model_validate_json(queued[0])
    assert task.metadata["merge_request_url"].endswith("/merge_requests/1")
    assert task.metadata["package"] == "bash"
    assert task.metadata["jira_issue"] == "RHEL-1234"
    assert task.metadata["source_agent"] == "Backport"
    assert task.metadata["cve_id"] == "CVE-2024-0001"


@pytest.mark.asyncio
async def test_submit_inherits_user_triggered_priority(monkeypatch):
    """A ymir_todo run's MR must be reviewed on the priority twin queue."""
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)

    async def _mock_run_tool(*_args, **_kwargs):
        return "ymir.yaml not found"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    redis = FakeRedis()
    await _submit(redis, user_triggered=True)

    assert list(redis.lists) == ["mr_verification_queue_c9s_todo"]
    task = Task.model_validate_json(redis.lists["mr_verification_queue_c9s_todo"][0])
    assert task.user_triggered is True


@pytest.mark.asyncio
async def test_submit_respects_per_package_opt_out(monkeypatch):
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)

    async def _mock_run_tool(*_args, **_kwargs):
        return "verification:\n  verify_mrs: false\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    redis = FakeRedis()
    await _submit(redis)

    assert redis.lists == {}


@pytest.mark.asyncio
async def test_submit_respects_global_kill_switch(monkeypatch):
    monkeypatch.setenv("MR_VERIFICATION_ENABLED", "false")
    redis = FakeRedis()
    await _submit(redis)

    assert redis.lists == {}


@pytest.mark.asyncio
async def test_submit_is_a_noop_without_redis(monkeypatch):
    """Direct mode has no queue; submitting must not raise."""
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)
    await _submit(None)


@pytest.mark.asyncio
async def test_submit_tolerates_malformed_config(monkeypatch):
    """A broken rules file must not stop the MR from being reviewed."""
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)

    async def _mock_run_tool(*_args, **_kwargs):
        return "verification:\n  verify_mrs: not_a_bool\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    redis = FakeRedis()
    await _submit(redis)

    assert len(redis.lists["mr_verification_queue_c9s"]) == 1


@pytest.mark.asyncio
async def test_submit_never_raises_when_redis_fails(monkeypatch):
    """Queueing a review must never fail the agent run that produced the MR."""
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)

    async def _mock_run_tool(*_args, **_kwargs):
        return "ymir.yaml not found"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)

    class BrokenRedis(FakeRedis):
        async def lpush(self, name, value):
            raise ConnectionError("redis is down")

    await _submit(BrokenRedis())


# -- already-reviewed dedup ----------------------------------------------------

_MR = "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/1"


@pytest.mark.asyncio
async def test_reviewed_head_round_trip_and_ttl():
    redis = FakeRedis()
    assert await already_reviewed(redis, _MR, "abc123") is False

    await record_reviewed_head(redis, _MR, "abc123")
    assert await already_reviewed(redis, _MR, "abc123") is True
    # A new push to the MR branch must be reviewed again.
    assert await already_reviewed(redis, _MR, "def456") is False
    # The marker must expire; the namespace has no eviction policy.
    assert next(iter(redis.ttls.values())) > 0


@pytest.mark.asyncio
async def test_dry_run_does_not_suppress_the_real_review():
    """A DRY_RUN pass publishes nothing, so it must not mark the MR reviewed."""
    redis = FakeRedis()
    await record_reviewed_head(redis, _MR, "abc123", dry_run=True)

    assert redis.strings == {}
    assert await already_reviewed(redis, _MR, "abc123") is False


@pytest.mark.asyncio
async def test_reviewed_head_helpers_tolerate_missing_redis_and_sha():
    assert await already_reviewed(None, _MR, "abc123") is False
    assert await already_reviewed(FakeRedis(), _MR, None) is False
    await record_reviewed_head(None, _MR, "abc123")
    await record_reviewed_head(FakeRedis(), _MR, None)


@pytest.mark.asyncio
async def test_dedup_failure_falls_back_to_reviewing():
    """Losing the dedup record costs a duplicate review, never a missed one."""

    class BrokenRedis(FakeRedis):
        async def get(self, name):
            raise ConnectionError("redis is down")

    assert await already_reviewed(BrokenRedis(), _MR, "abc123") is False


# -- output schema -------------------------------------------------------------


def test_has_blockers():
    warning_only = MRVerificationOutputSchema(
        verdict=MRVerificationVerdict.APPROVED,
        summary="ok",
        findings=[
            MRVerificationFinding(
                severity=MRFindingSeverity.WARNING,
                category="changelog",
                description="wording",
            )
        ],
    )
    assert warning_only.has_blockers is False

    with_blocker = MRVerificationOutputSchema(
        verdict=MRVerificationVerdict.CHANGES_REQUESTED,
        summary="nope",
        findings=[
            MRVerificationFinding(
                severity=MRFindingSeverity.BLOCKER,
                category="patch-provenance",
                description="no upstream reference",
            )
        ],
    )
    assert with_blocker.has_blockers is True


# -- comment rendering ---------------------------------------------------------


def test_format_review_comment_approved():
    comment = format_review_comment(
        MRVerificationOutputSchema(
            verdict=MRVerificationVerdict.APPROVED,
            summary="The patch matches the CVE and the changelog is correct.",
            checks_performed=["spec correctness", "patch provenance"],
        ),
        "Backport",
    )
    assert "no problems found" in comment
    assert "spec correctness" in comment
    assert "### Findings" not in comment


def test_format_review_comment_changes_requested_lists_findings():
    comment = format_review_comment(
        MRVerificationOutputSchema(
            verdict=MRVerificationVerdict.CHANGES_REQUESTED,
            summary="The release bump is missing.",
            findings=[
                MRVerificationFinding(
                    severity=MRFindingSeverity.BLOCKER,
                    category="release-bump",
                    file="bash.spec",
                    description="Release stayed at 3.",
                    suggestion="Bump Release to 4.",
                ),
                MRVerificationFinding(
                    severity=MRFindingSeverity.NITPICK,
                    category="changelog",
                    description="Trailing whitespace.",
                ),
            ],
        ),
        "Backport",
    )
    assert "changes requested" in comment
    assert "`bash.spec`" in comment
    assert "Bump Release to 4." in comment
    assert "Trailing whitespace." in comment
    # Severity ordering is the agent's job; the renderer must not reorder.
    assert comment.index("release-bump") < comment.index("changelog")


def test_format_review_comment_inconclusive():
    comment = format_review_comment(
        MRVerificationOutputSchema(
            verdict=MRVerificationVerdict.INCONCLUSIVE,
            summary="Sources could not be downloaded.",
            error="download_sources failed",
        ),
        "Rebase",
    )
    assert "inconclusive" in comment


# -- labels --------------------------------------------------------------------


def test_verification_labels_are_distinct_and_prefixed():
    labels = {
        JiraLabels.MR_VERIFIED.value,
        JiraLabels.MR_CHANGES_REQUESTED.value,
        JiraLabels.MR_VERIFICATION_ERRORED.value,
    }
    assert len(labels) == 3
    assert all(label.startswith("ymir_") for label in labels)
    assert labels <= JiraLabels.all_labels()
