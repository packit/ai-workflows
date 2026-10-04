import json
from datetime import UTC, datetime, timedelta

import pytest
from flexmock import flexmock

from ymir.agents import tasks as agent_tasks
from ymir.agents.mr_verification_agent import (
    already_handled,
    format_ci_comment,
    format_job_logs,
    pipeline_fingerprint,
    record_handled_pipeline,
)
from ymir.agents.tasks import (
    InvalidVerificationConfigError,
    fetch_verification_config,
    try_submit_verification_job,
)
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import (
    CIFailureCategory,
    CIJobFailureAnalysis,
    CIVerificationVerdict,
    FailedPipelineJob,
    MergeRequestPipelineStatus,
    MRVerificationOutputSchema,
    MRVerificationTaskMetadata,
    Task,
)

_MR = "https://gitlab.com/redhat/rhel/rpms/bash/-/merge_requests/1"


class FakeRedis:
    """Minimal in-memory Redis stand-in for the operations used here."""

    def __init__(self):
        self.lists: dict[str, list[bytes]] = {}
        self.strings: dict[str, bytes] = {}
        self.ttls: dict[str, int | None] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def lpush(self, name: str, value: str | bytes):
        payload = value.encode() if isinstance(value, str) else value
        self.lists.setdefault(name, []).insert(0, payload)
        return len(self.lists[name])

    async def zadd(self, name: str, mapping: dict[str, float]):
        self.zsets.setdefault(name, {}).update(mapping)
        return len(mapping)

    async def set(self, name: str, value: str | bytes, ex: int | None = None):
        self.strings[name] = value.encode() if isinstance(value, str) else value
        self.ttls[name] = ex
        return True

    async def get(self, name: str):
        # Real client is created without decode_responses, so it returns bytes.
        return self.strings.get(name)


def _job(name: str = "build", job_id: str = "7", allow_failure: bool = False) -> FailedPipelineJob:
    return FailedPipelineJob(
        id=job_id,
        name=name,
        url=f"https://gitlab.com/redhat/rhel/rpms/bash/-/jobs/{job_id}",
        status="failed",
        stage="build",
        artifacts_url="",
        allow_failure=allow_failure,
    )


# -- queue routing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("user_triggered", "expected"),
    [(False, "mr_verification_queue"), (True, "mr_verification_queue_todo")],
)
def test_queue_selection(user_triggered, expected):
    """One queue, not a c9s/c10s pair: this agent never builds anything."""
    assert RedisQueues.get_mr_verification_queue(user_triggered) == expected


def test_verification_queues_are_registered():
    """A queue missing from these sets is invisible to the fetcher and the CLI."""
    all_queues = RedisQueues.all_queues()
    for queue in (RedisQueues.MR_VERIFICATION_QUEUE, RedisQueues.MR_VERIFICATION_QUEUE_TODO):
        assert queue.value in all_queues
        assert queue.value in RedisQueues.input_queues()
    assert RedisQueues.COMPLETED_MR_VERIFICATION_LIST.value in RedisQueues.data_queues()
    # The delayed ZSET is not a BRPOP list and must stay out of all_queues().
    assert RedisQueues.MR_VERIFICATION_DELAYED_QUEUE.value not in all_queues


# -- pipeline status model -----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "pending", "passing", "failed"),
    [
        ("none", True, False, False),
        ("created", True, False, False),
        ("running", True, False, False),
        ("pending", True, False, False),
        ("success", False, True, False),
        ("skipped", False, True, False),
        ("manual", False, True, False),
        ("failed", False, False, True),
        ("canceled", False, False, False),
    ],
)
def test_pipeline_status_classification(status, pending, passing, failed):
    pipeline = MergeRequestPipelineStatus(status=status)
    assert pipeline.is_pending is pending
    assert pipeline.is_passing is passing
    assert pipeline.is_failed is failed


def test_blocking_failed_jobs_ignores_allow_failure():
    """A job with allow_failure: true never fails the pipeline, so never blame it."""
    pipeline = MergeRequestPipelineStatus(
        status="failed",
        failed_jobs=[_job("rpminspect", "1", allow_failure=True), _job("build", "2")],
    )
    assert [job.name for job in pipeline.blocking_failed_jobs] == ["build"]


def test_pipeline_fingerprint_changes_with_a_new_push():
    first = MergeRequestPipelineStatus(status="failed", pipeline_id=1, sha="abc")
    second = MergeRequestPipelineStatus(status="failed", pipeline_id=2, sha="def")
    assert pipeline_fingerprint(first) != pipeline_fingerprint(second)
    assert pipeline_fingerprint(MergeRequestPipelineStatus(status="none")) is None


# -- fetch_verification_config -------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_config_defaults_to_enabled_when_file_missing():
    async def _mock_run_tool(*_args, **_kwargs):
        return "No maintainer rules found for package 'bash' (file 'ymir.yaml' not found)"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    config = await fetch_verification_config("bash", [])

    assert config.analyze_ci_failures is True


@pytest.mark.asyncio
async def test_fetch_config_parses_opt_out():
    async def _mock_run_tool(*_args, **_kwargs):
        return "verification:\n  analyze_ci_failures: false\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    config = await fetch_verification_config("bash", [])

    assert config.analyze_ci_failures is False


@pytest.mark.asyncio
async def test_fetch_config_ignores_unrelated_sections():
    async def _mock_run_tool(*_args, **_kwargs):
        return "consolidation:\n  merge_mrs: false\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    config = await fetch_verification_config("bash", [])

    assert config.analyze_ci_failures is True


@pytest.mark.asyncio
async def test_fetch_config_raises_on_malformed_section():
    async def _mock_run_tool(*_args, **_kwargs):
        return "verification:\n  analyze_ci_failures: not_a_bool\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    with pytest.raises(InvalidVerificationConfigError, match="malformed"):
        await fetch_verification_config("bash", [])


@pytest.mark.asyncio
async def test_fetch_config_raises_on_invalid_yaml():
    async def _mock_run_tool(*_args, **_kwargs):
        return "verification:\n  analyze_ci_failures: [\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    with pytest.raises(InvalidVerificationConfigError, match="not valid YAML"):
        await fetch_verification_config("bash", [])


# -- try_submit_verification_job -----------------------------------------------


async def _submit(redis, **overrides):
    kwargs = {
        "package": "bash",
        "dist_git_branch": "rhel-9.4.0",
        "merge_request_url": _MR,
        "jira_issue": "RHEL-1234",
        "source_agent": "Backport",
        "gateway_tools": [],
        "redis_conn": redis,
    }
    kwargs.update(overrides)
    await try_submit_verification_job(**kwargs)


def _only_delayed_task(redis: FakeRedis) -> Task:
    delayed = redis.zsets[RedisQueues.MR_VERIFICATION_DELAYED_QUEUE.value]
    assert len(delayed) == 1
    return Task.model_validate_json(next(iter(delayed)))


@pytest.mark.asyncio
async def test_submit_schedules_a_delayed_first_check(monkeypatch):
    """The pipeline cannot have finished yet, so the first poll is delayed."""
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)
    monkeypatch.setenv("MR_VERIFICATION_INITIAL_DELAY", "120")

    async def _mock_run_tool(*_args, **_kwargs):
        return "ymir.yaml not found"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    redis = FakeRedis()
    await _submit(redis, cve_id="CVE-2024-0001")

    # Nothing goes straight onto a BRPOP list.
    assert redis.lists == {}
    task = _only_delayed_task(redis)
    metadata = MRVerificationTaskMetadata.model_validate(task.metadata)
    assert metadata.merge_request_url == _MR
    assert metadata.package == "bash"
    assert metadata.cve_id == "CVE-2024-0001"
    assert metadata.poll_count == 0
    assert metadata.deadline_at is not None and metadata.deadline_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_submit_stamps_a_deadline_from_max_wait(monkeypatch):
    """The wait budget is fixed at submission so re-queued polls cannot extend it."""
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)
    monkeypatch.setenv("MR_VERIFICATION_MAX_WAIT", "3600")

    async def _mock_run_tool(*_args, **_kwargs):
        return "ymir.yaml not found"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    redis = FakeRedis()
    await _submit(redis)

    metadata = MRVerificationTaskMetadata.model_validate(_only_delayed_task(redis).metadata)
    assert metadata.deadline_at < datetime.now(UTC) + timedelta(seconds=3700)
    assert metadata.deadline_at > datetime.now(UTC) + timedelta(seconds=3500)


@pytest.mark.asyncio
async def test_submit_inherits_user_triggered_priority(monkeypatch):
    """A ymir_todo run's MR must be promoted onto the priority twin queue."""
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)

    async def _mock_run_tool(*_args, **_kwargs):
        return "ymir.yaml not found"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    redis = FakeRedis()
    await _submit(redis, user_triggered=True)

    assert _only_delayed_task(redis).user_triggered is True


@pytest.mark.asyncio
async def test_submit_respects_per_package_opt_out(monkeypatch):
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)

    async def _mock_run_tool(*_args, **_kwargs):
        return "verification:\n  analyze_ci_failures: false\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    redis = FakeRedis()
    await _submit(redis)

    assert redis.zsets == {}


@pytest.mark.asyncio
async def test_submit_respects_global_kill_switch(monkeypatch):
    monkeypatch.setenv("MR_VERIFICATION_ENABLED", "false")
    redis = FakeRedis()
    await _submit(redis)

    assert redis.zsets == {}


@pytest.mark.asyncio
async def test_submit_is_a_noop_without_redis(monkeypatch):
    """Direct mode has no queue; submitting must not raise."""
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)
    await _submit(None)


@pytest.mark.asyncio
async def test_submit_tolerates_malformed_config(monkeypatch):
    """A broken rules file must not stop the MR's CI from being watched."""
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)

    async def _mock_run_tool(*_args, **_kwargs):
        return "verification:\n  analyze_ci_failures: not_a_bool\n"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)
    redis = FakeRedis()
    await _submit(redis)

    assert len(redis.zsets[RedisQueues.MR_VERIFICATION_DELAYED_QUEUE.value]) == 1


@pytest.mark.asyncio
async def test_submit_never_raises_when_redis_fails(monkeypatch):
    """Queueing the watcher must never fail the agent run that produced the MR."""
    monkeypatch.delenv("MR_VERIFICATION_ENABLED", raising=False)

    async def _mock_run_tool(*_args, **_kwargs):
        return "ymir.yaml not found"

    flexmock(agent_tasks).should_receive("run_tool").replace_with(_mock_run_tool)

    class BrokenRedis(FakeRedis):
        async def zadd(self, name, mapping):
            raise ConnectionError("redis is down")

    await _submit(BrokenRedis())


# -- already-handled dedup -----------------------------------------------------


@pytest.mark.asyncio
async def test_handled_pipeline_round_trip_and_ttl():
    redis = FakeRedis()
    assert await already_handled(redis, _MR, "1:abc") is False

    await record_handled_pipeline(redis, _MR, "1:abc")
    assert await already_handled(redis, _MR, "1:abc") is True
    # A new push to the MR branch starts a new pipeline, which must be handled.
    assert await already_handled(redis, _MR, "2:def") is False
    # The marker must expire; the namespace has no eviction policy.
    assert next(iter(redis.ttls.values())) > 0


@pytest.mark.asyncio
async def test_dry_run_does_not_suppress_the_real_run():
    """A DRY_RUN pass publishes nothing, so it must not mark the pipeline handled."""
    redis = FakeRedis()
    await record_handled_pipeline(redis, _MR, "1:abc", dry_run=True)

    assert redis.strings == {}
    assert await already_handled(redis, _MR, "1:abc") is False


@pytest.mark.asyncio
async def test_dedup_helpers_tolerate_missing_redis_and_fingerprint():
    assert await already_handled(None, _MR, "1:abc") is False
    assert await already_handled(FakeRedis(), _MR, None) is False
    await record_handled_pipeline(None, _MR, "1:abc")
    await record_handled_pipeline(FakeRedis(), _MR, None)


@pytest.mark.asyncio
async def test_dedup_failure_falls_back_to_handling():
    """Losing the dedup record costs a duplicate comment, never a missed one."""

    class BrokenRedis(FakeRedis):
        async def get(self, name):
            raise ConnectionError("redis is down")

    assert await already_handled(BrokenRedis(), _MR, "1:abc") is False


# -- output schema -------------------------------------------------------------


def test_caused_by_mr_follows_the_verdict():
    assert (
        MRVerificationOutputSchema(
            merge_request_url=_MR,
            verdict=CIVerificationVerdict.CI_FAILED_MR,
            summary="patch does not apply",
        ).caused_by_mr
        is True
    )
    assert (
        MRVerificationOutputSchema(
            merge_request_url=_MR,
            verdict=CIVerificationVerdict.CI_FAILED_INFRASTRUCTURE,
            summary="runner died",
        ).caused_by_mr
        is False
    )


def test_all_failures_retryable():
    infra = CIJobFailureAnalysis(
        job_name="build",
        category=CIFailureCategory.INFRASTRUCTURE,
        caused_by_mr=False,
        evidence="Could not resolve host",
        retryable=True,
    )
    real = CIJobFailureAnalysis(
        job_name="build",
        category=CIFailureCategory.BUILD,
        caused_by_mr=True,
        evidence="Hunk #2 FAILED",
    )
    base = {
        "merge_request_url": _MR,
        "verdict": CIVerificationVerdict.CI_FAILED_INFRASTRUCTURE,
        "summary": "s",
    }
    assert MRVerificationOutputSchema(**base, job_analyses=[infra]).all_failures_retryable is True
    assert MRVerificationOutputSchema(**base, job_analyses=[infra, real]).all_failures_retryable is False
    # No jobs means nothing to retry, not "everything is retryable".
    assert MRVerificationOutputSchema(**base).all_failures_retryable is False


# -- comment rendering ---------------------------------------------------------


def test_format_ci_comment_blames_the_mr():
    comment = format_ci_comment(
        MRVerificationOutputSchema(
            merge_request_url=_MR,
            verdict=CIVerificationVerdict.CI_FAILED_MR,
            summary="%prep failed applying the new patch.",
            pipeline_url="https://gitlab.com/pipelines/42",
            job_analyses=[
                CIJobFailureAnalysis(
                    job_name="build-c9s",
                    job_url="https://gitlab.com/jobs/7",
                    category=CIFailureCategory.BUILD,
                    caused_by_mr=True,
                    evidence="Hunk #2 FAILED at 412",
                    suggested_action="Rebase 0003-fix-overflow.patch onto 5.1.8.",
                )
            ],
        )
    )
    assert "failed because of this change" in comment
    assert "https://gitlab.com/pipelines/42" in comment
    assert "Hunk #2 FAILED at 412" in comment
    assert "Rebase 0003-fix-overflow.patch" in comment
    assert "caused by this MR" in comment


def test_format_ci_comment_for_infrastructure_suggests_a_retry():
    comment = format_ci_comment(
        MRVerificationOutputSchema(
            merge_request_url=_MR,
            verdict=CIVerificationVerdict.CI_FAILED_INFRASTRUCTURE,
            summary="The runner lost its network.",
            job_analyses=[
                CIJobFailureAnalysis(
                    job_name="build-c9s",
                    category=CIFailureCategory.INFRASTRUCTURE,
                    caused_by_mr=False,
                    evidence="Could not resolve host: mirror.example.com",
                    retryable=True,
                )
            ],
        )
    )
    assert "not because of this change" in comment
    assert "not caused by this MR" in comment
    assert "retrying the pipeline" in comment


def test_format_ci_comment_mentions_the_opt_out_key():
    """The comment must tell a maintainer how to turn the bot off."""
    comment = format_ci_comment(
        MRVerificationOutputSchema(
            merge_request_url=_MR,
            verdict=CIVerificationVerdict.INCONCLUSIVE,
            summary="Logs were empty.",
        )
    )
    assert "cause is unclear" in comment
    assert "verification.analyze_ci_failures: false" in comment


def test_format_job_logs_handles_the_empty_case():
    assert "no job logs" in format_job_logs({})
    rendered = format_job_logs({"build": "boom"})
    assert "### build" in rendered and "boom" in rendered


# -- task metadata -------------------------------------------------------------


def test_metadata_round_trips_through_json():
    """Tasks are stored as JSON in Redis, so datetimes must survive the trip."""
    metadata = MRVerificationTaskMetadata(
        merge_request_url=_MR,
        package="bash",
        dist_git_branch="rhel-9.4.0",
        jira_issue="RHEL-1234",
        source_agent="Backport",
        deadline_at=datetime.now(UTC) + timedelta(hours=1),
    )
    task = Task(metadata=metadata.model_dump(mode="json"))
    reparsed = MRVerificationTaskMetadata.model_validate(json.loads(task.model_dump_json())["metadata"])
    assert reparsed.deadline_at == metadata.deadline_at
    assert reparsed.poll_count == 0


# -- labels --------------------------------------------------------------------


def test_verification_labels_are_distinct_and_prefixed():
    labels = {
        JiraLabels.MR_CI_PASSED.value,
        JiraLabels.MR_CI_FAILED.value,
        JiraLabels.MR_CI_INFRA_FAILURE.value,
        JiraLabels.MR_VERIFICATION_ERRORED.value,
    }
    assert len(labels) == 4
    assert all(label.startswith("ymir_") for label in labels)
    assert labels <= JiraLabels.all_labels()
