"""Exercise write recovery with a scripted agent, mock Jira, Temporal, and Valkey."""

import asyncio
import json
import os
import socket
from collections import Counter
from datetime import timedelta
from uuid import uuid4

import pytest
import uvicorn
from flexmock import flexmock
from mcp.server.fastmcp import FastMCP
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from ymir.common.base_utils import fix_await, redis_client
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import Resolution, Task

pytest.importorskip("temporalio")

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from ymir.agents import temporal_triage_publication as publication
from ymir.agents.temporal_triage_analysis import TriageAnalysisRequest, create_analysis_worker

ADDRESS = os.getenv("TEMPORAL_TEST_ADDRESS")
REDIS_URL = os.getenv("TEMPORAL_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not ADDRESS or not REDIS_URL,
    reason="Set TEMPORAL_TEST_ADDRESS and TEMPORAL_TEST_REDIS_URL to test servers",
)
ISSUE = "RHEL-904001"
FIX_QUEUE = RedisQueues.BACKPORT_QUEUE_C9S_TODO.value


@pytest.mark.asyncio
async def test_publication_recovers_accepted_writes(monkeypatch, tmp_path):
    # TEMPORAL_TEST_REDIS_URL must point to a disposable instance because
    # this exercises the actual consumer queue names.
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("JIRA_DRY_RUN", "false")
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    config = tmp_path / "rhel-config.json"
    config.write_text(json.dumps({"current_y_streams": {"9": "rhel-9.9"}, "current_z_streams": {}}))
    monkeypatch.setenv("RHEL_CONFIG_PATH", str(config))

    labels = {"fixture-label"}
    comments = {}
    jira_fields = {}
    accepted = Counter()
    attempts = Counter()
    popped = []
    model_calls = []
    blocked_operations = set()
    model_started = asyncio.Event()
    continue_first_model = asyncio.Event()
    continue_preview_model = asyncio.Event()
    gateway = FastMCP("Temporal publication fixtures")

    @gateway.tool()
    def check_cve_triage_eligibility(issue_key: str) -> dict:
        return {"is_cve": False, "eligibility": "immediately", "reason": "Fixture"}

    @gateway.tool()
    def get_jira_details(issue_key: str) -> dict:
        assert issue_key == ISSUE
        return {
            "key": ISSUE,
            "fields": {
                "summary": "nginx maintenance fixture",
                "status": {"name": "New"},
                "labels": sorted(labels),
                "fixVersions": [{"name": "rhel-9.9"}],
            },
        }

    @gateway.tool()
    def find_jira_comment_operation(issue_key: str, operation_id: str) -> dict:
        attempts[operation_id] += 1
        return {"exists": operation_id in comments}

    @gateway.tool()
    def add_jira_comment(issue_key: str, comment: str, private: bool) -> str:
        assert issue_key == ISSUE and private
        marker = next(line for line in comment.splitlines() if line.startswith("Ymir operation: "))
        operation_id = marker.removeprefix("Ymir operation: ")
        accepted[operation_id] += 1
        comments[operation_id] = comment
        raise TimeoutError("Injected loss of the Jira comment response")

    @gateway.tool()
    def set_jira_fields(issue_key: str, severity: str | None = None) -> str:
        assert issue_key == ISSUE and severity == "Moderate"
        attempts["fields"] += 1
        if "severity" not in jira_fields:
            jira_fields["severity"] = severity
            accepted["fields"] += 1
            raise TimeoutError("Injected loss of the Jira field response")
        return "No fields needed updating"

    @gateway.tool()
    def edit_jira_labels(issue_key: str, labels_to_add: list[str], labels_to_remove: list[str]) -> str:
        assert issue_key == ISSUE
        attempts["labels"] += 1
        labels.update(labels_to_add)
        labels.difference_update(labels_to_remove)
        raise TimeoutError("Injected loss of the Jira label response")

    async def model_response(messages, info):
        if len(model_calls) == 2:
            await continue_preview_model.wait()
        model_calls.append(info)
        if len(model_calls) == 1:
            model_started.set()
            await continue_first_model.wait()
        proposal = next(
            (part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)), None
        )
        if proposal is None:
            assert "set_jira_fields" in {tool.name for tool in info.function_tools}
            return ModelResponse(
                [ToolCallPart("set_jira_fields", {"issue_key": ISSUE, "severity": "Moderate"})]
            )
        assert proposal.content == {"issue_key": ISSUE, "severity": "Moderate"}
        return ModelResponse(
            [
                TextPart(
                    json.dumps(
                        {
                            "resolution": "backport",
                            "data": {
                                "package": "nginx",
                                "patch_urls": [],
                                "justification": "Fixture fix",
                                "jira_issue": ISSUE,
                                "fix_version": "rhel-9.9",
                            },
                        }
                    )
                )
            ]
        )

    actual_enqueue = publication._enqueue_with_receipt

    async def enqueue_then_lose_response(item):
        attempts[item.operation_id] += 1
        if item.operation_id in blocked_operations:
            raise ApplicationError("Injected terminal handoff failure", non_retryable=True)
        written = await actual_enqueue(item)
        if written == 1:
            async with redis_client(REDIS_URL) as redis:
                popped.append(Task.model_validate_json(await fix_await(redis.rpop(item.destination))))
            raise TimeoutError("Injected loss of the Redis response after the consumer took the task")
        assert written == 0
        return written

    flexmock(publication).should_receive("_enqueue_with_receipt").replace_with(enqueue_then_lose_response)
    client = await Client.connect(ADDRESS, plugins=[PydanticAIPlugin()])
    queue = f"triage-publication-test-{uuid4().hex}"
    request = TriageAnalysisRequest(ISSUE, {}, 10, user_triggered=True)
    first = publication.TriagePublicationRequest(request, uuid4().hex, dry_run=False)
    triage_only = publication.TriagePublicationRequest(
        TriageAnalysisRequest(ISSUE, {}, 10, user_triggered=True, triage_only_requested=True),
        uuid4().hex,
        dry_run=False,
    )
    preview = publication.TriagePublicationRequest(request, uuid4().hex)

    async def start(command):
        return await client.start_workflow(
            publication.TriagePublicationWorkflow.run,
            command,
            id=command.workflow_id(),
            task_queue=queue,
            execution_timeout=timedelta(seconds=70),
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        host, port = listener.getsockname()
        monkeypatch.setenv("MCP_GATEWAY_URL", f"http://{host}:{port}/sse")
        server = uvicorn.Server(uvicorn.Config(gateway.sse_app(), log_level="warning"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with await create_analysis_worker(
                client, queue, FunctionModel(model_response), publication=True
            ):
                first_handle = await start(first)
                await asyncio.wait_for(model_started.wait(), timeout=15)
                assert first_handle.id == f"triage/{ISSUE}"
                assert preview.workflow_id() == triage_only.workflow_id() == first_handle.id
                # Temporal rejects the competing start even on a different
                # task queue while the issue's workflow is running.
                with pytest.raises(WorkflowAlreadyStartedError):
                    await client.start_workflow(
                        publication.TriagePublicationWorkflow.run,
                        preview,
                        id=preview.workflow_id(),
                        task_queue=f"{queue}-second-worker",
                        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                    )
                assert len(model_calls) == 1
                next_preview = asyncio.create_task(publication.run_triage_request(client, preview, queue))
                continue_first_model.set()
                result = await first_handle.result()
                assert result.analysis.state.triage_result.resolution == Resolution.BACKPORT
                assert result.destinations == [FIX_QUEUE]
                assert len(model_calls) == 2
                assert attempts["fields"] == 2 and accepted["fields"] == 1
                assert attempts["labels"] == 1
                assert labels == {"fixture-label", JiraLabels.TRIAGED_BACKPORT.value}
                assert attempts[first.operation_id(f"handoff:{FIX_QUEUE}")] == 2
                assert len(popped) == 1
                assert popped[0].user_triggered is True
                assert popped[0].metadata["jira_issue"] == ISSUE
                assert popped[0].metadata["target_branch"] == "c9s"
                assert accepted[first.operation_id("acknowledgement")] == 1
                assert accepted[first.operation_id("finding")] == 1
                assert '"mode": "full"' in comments[first.operation_id("acknowledgement")]
                assert '"planned_follow_up": ["backport"]' in comments[first.operation_id("finding")]

                repeated = await publication.run_triage_request(client, first, queue)
                assert repeated == result
                assert len(model_calls) == 2

                continue_preview_model.set()
                preview_result = await next_preview
                assert preview_result.dry_run and preview_result.destinations == [FIX_QUEUE]
                assert len(comments) == 2 and len(popped) == 1
                assert attempts["fields"] == 2 and attempts["labels"] == 1

                later_result = await (await start(triage_only)).result()
                assert later_result.destinations == []
                assert '"mode": "triage-only"' in comments[triage_only.operation_id("acknowledgement")]
                assert accepted[triage_only.operation_id("finding")] == 1
                assert labels == {
                    "fixture-label",
                    JiraLabels.TRIAGE_ONLY.value,
                    JiraLabels.TRIAGED_DEFERRED_BACKPORT.value,
                }
                assert len(popped) == 1 and len(model_calls) == 6
                assert await publication.run_triage_request(client, first, queue) == result
                assert len(model_calls) == 6

                # Even a low-level start that bypasses the receipt lookup
                # cannot execute a completed request again.
                duplicate = await start(first)
                with pytest.raises(WorkflowFailureError) as duplicate_failure:
                    await duplicate.result()
                assert duplicate_failure.value.cause.type == publication.REQUEST_ALREADY_STARTED
                assert len(model_calls) == 6

                blocked = publication.TriagePublicationRequest(request, uuid4().hex, dry_run=False)
                operation = blocked.operation_id(f"handoff:{FIX_QUEUE}")
                blocked_operations.add(operation)
                blocked_handle = await start(blocked)
                with pytest.raises(WorkflowFailureError) as failure:
                    await blocked_handle.result()
                assert "Injected terminal handoff failure" in str(failure.value.cause.cause)
                assert len(model_calls) == 8 and len(popped) == 1
                assert JiraLabels.TRIAGE_ONLY.value not in labels
                assert '"mode": "full"' in comments[blocked.operation_id("acknowledgement")]
                assert accepted[blocked.operation_id("finding")] == 1

                history = await blocked_handle.fetch_history()
                scheduled = next(
                    event.activity_task_scheduled_event_attributes
                    for event in history.events
                    if event.activity_task_scheduled_event_attributes.activity_id == operation
                )
                blocked_operations.remove(operation)
                await client.workflow_service.reset_workflow_execution(
                    ResetWorkflowExecutionRequest(
                        namespace=client.namespace,
                        workflow_execution=WorkflowExecution(
                            workflow_id=blocked_handle.id, run_id=blocked_handle.first_execution_run_id
                        ),
                        workflow_task_finish_event_id=scheduled.workflow_task_completed_event_id,
                        request_id=uuid4().hex,
                        reason="Recover after the injected handoff failure",
                    )
                )
                resumed = await publication.run_triage_request(client, blocked, queue)
                assert resumed.destinations == [FIX_QUEUE]
                assert len(popped) == 2 and popped[0].execution_id != popped[1].execution_id
                assert len(model_calls) == 8
                assert accepted[blocked.operation_id("finding")] == 1

            async with redis_client(REDIS_URL) as redis:
                assert await fix_await(redis.llen(FIX_QUEUE)) == 0
        finally:
            server.should_exit = True
            await serving
