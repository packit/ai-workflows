"""Exercise manual branch holds through triage workflow."""

import importlib
from contextlib import asynccontextmanager

import pytest
from beeai_framework.adapters.mcp.serve.server import MCPServer
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolError, ToolRunOptions
from beeai_framework.tools.mcp import MCPTool
from flexmock import flexmock
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import BaseModel

from ymir.agents import tasks
from ymir.agents.rebase_consolidation import get_rebase_primary_issue
from ymir.common.constants import JiraLabels, RedisQueues
from ymir.common.models import BackportData, RebaseData, RebuildData, Task
from ymir.tools.base import make_additional_context
from ymir.tools.constants import ZSTREAM_BRANCH_POLICY_REJECTED
from ymir.tools.errors import ToolErrorWithContext


@pytest.mark.parametrize(
    "reference",
    [
        "RHEL-200",
        "https://redhat.atlassian.net/browse/RHEL-200#icft=RHEL-200",
        '<custom data-type="smartlink">https://redhat.atlassian.net/browse/RHEL-200</custom>',
    ],
)
def test_primary_reference_is_read_after_marker(reference):
    details = {
        "fields": {
            "comment": {
                "comments": [{"body": f"RHEL-100: Queued for triage as potential sibling of {reference}"}]
            }
        }
    }
    assert get_rebase_primary_issue(details) == "RHEL-200"


def test_primary_reference_supports_jira_inline_card():
    details = {
        "fields": {
            "comment": {
                "comments": [
                    {
                        "body": {
                            "type": "doc",
                            "version": 1,
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "Queued for triage as potential sibling of ",
                                        },
                                        {
                                            "type": "inlineCard",
                                            "attrs": {"url": "https://example.com/browse/RHEL-200"},
                                        },
                                    ],
                                }
                            ],
                        }
                    }
                ]
            }
        }
    }
    assert get_rebase_primary_issue(details) == "RHEL-200"


def test_primary_reference_keeps_full_issue_key():
    details = {
        "fields": {"comment": {"comments": [{"body": "Queued for triage as potential sibling of RHEL-2001"}]}}
    }
    assert get_rebase_primary_issue(details) == "RHEL-2001"


@asynccontextmanager
async def _gateway(*_args, **_kwargs):
    yield []


@pytest.mark.asyncio
async def test_manual_hold_labels_group_and_keeps_primary_result_comment(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway")
    labels = []
    comments = []

    async def _record_labels(**kwargs):
        labels.append(kwargs)

    async def _record_comment(**kwargs):
        comments.append(kwargs)

    flexmock(tasks).should_receive("set_jira_labels").replace_with(_record_labels)
    flexmock(tasks).should_receive("comment_in_jira").replace_with(_record_comment)
    monkeypatch.setattr(tasks, "mcp_tools", _gateway)

    await tasks.handle_manual_branch_creation_required(
        tasks.ManualBranchCreationRequired("bash", "rhel-10.3", ["RHEL-1", "RHEL-2"]),
        agent_type="Triage",
        triaged_label=JiraLabels.TRIAGED_REBASE.value,
        dry_run=False,
        user_triggered=False,
        primary_comment="Full triage result",
    )

    assert [call["jira_issue"] for call in labels] == ["RHEL-2", "RHEL-1"]
    assert comments[0]["comment_text"] == "Full triage result"
    assert "create the branch manually" in comments[1]["comment_text"]


def _policy_tool_error(message: str, raw_stderr: str = "") -> ToolError:
    return ToolError(
        '"Error executing tool create_zstream_branch: escaped payload"',
        context={
            "additional_context": {
                "error_code": ZSTREAM_BRANCH_POLICY_REJECTED,
                "user_message": message,
                "raw_stderr": raw_stderr,
            },
            "name": "create_zstream_branch",
        },
    )


@pytest.mark.asyncio
async def test_fork_and_prepare_dist_git_unwraps_policy_rejection(monkeypatch, tmp_path):
    """The agent gets the clean wiki-markup message, not the MCP JSON envelope."""
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    clean_message = (
        "Ymir could not create the missing Z-stream branch {{rhel-9.9.0}}.\n"
        "# Add the {{ymir_todo}} label to retry."
    )
    raw_stderr = "remote: *** Checking commit deadbeef\nremote: hook declined"
    policy_error = _policy_tool_error(clean_message, raw_stderr=raw_stderr)

    async def _run_tool(name, **kwargs):
        if name == "fork_repository":
            return "https://fork.example/rpms/bash"
        if name == "create_zstream_branch":
            raise policy_error
        raise AssertionError(f"unexpected tool {name}")

    flexmock(tasks).should_receive("run_tool").replace_with(_run_tool)

    with pytest.raises(tasks.ZstreamBranchPolicyRejected) as excinfo:
        await tasks.fork_and_prepare_dist_git(
            jira_issue="RHEL-123",
            package="bash",
            dist_git_branch="rhel-9.9.0",
            available_tools=[],
            agent_type="Backport",
        )

    assert excinfo.value.message == clean_message
    assert excinfo.value.package == "bash"
    assert excinfo.value.branch == "rhel-9.9.0"
    assert excinfo.value.retryable is False
    assert excinfo.value.__cause__ is policy_error
    assert excinfo.value.raw_stderr == raw_stderr
    assert "Raw hook output:" in excinfo.value.triage_details
    assert raw_stderr in excinfo.value.triage_details
    # The cause is kept for logging, but must not leak its details through explain().
    assert "user_message" not in policy_error.context["additional_context"]
    assert "raw_stderr" not in policy_error.context["additional_context"]


class _PolicyRejectingInput(BaseModel):
    package: str


class _PolicyRejectingTool(Tool[_PolicyRejectingInput, ToolRunOptions, StringToolOutput]):
    name = "create_zstream_branch"
    description = "Raises a policy rejection like the privileged gateway tool."
    input_schema = _PolicyRejectingInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(namespace=["tool", "test", self.name], creator=self)

    async def _run(self, tool_input, options, context):
        raise ToolErrorWithContext(
            "multi-line\n# message",
            additional_context=make_additional_context(
                error_code=ZSTREAM_BRANCH_POLICY_REJECTED,
                user_message="multi-line\n# message",
                raw_stderr="remote: hook declined",
            ),
        )


@pytest.mark.asyncio
async def test_policy_rejection_details_survive_mcp_boundary():
    """The error code and message reach the agent through a real MCP server and client."""
    server = MCPServer()
    server.register(_PolicyRejectingTool())
    server._register_members()

    async with create_connected_server_and_client_session(server._server._mcp_server) as session:
        [tool] = await MCPTool.from_session(session)
        with pytest.raises(ToolError) as excinfo:
            await tool.run({"package": "bash"})

    details = tasks.policy_rejection_details(excinfo.value)
    assert details is not None
    assert details.message == "multi-line\n# message"
    assert details.raw_stderr == "remote: hook declined"


@pytest.mark.asyncio
async def test_fork_and_prepare_dist_git_keeps_unrelated_tool_errors(monkeypatch, tmp_path):
    """Non-policy tool errors still propagate unchanged."""
    monkeypatch.setenv("GIT_REPO_BASEPATH", str(tmp_path))
    unrelated_error = ToolError(
        "boom",
        context={"additional_context": {"error_code": "something_else"}},
    )

    async def _run_tool(name, **kwargs):
        if name == "fork_repository":
            return "https://fork.example/rpms/bash"
        if name == "create_zstream_branch":
            raise unrelated_error
        raise AssertionError(f"unexpected tool {name}")

    flexmock(tasks).should_receive("run_tool").replace_with(_run_tool)

    with pytest.raises(ToolError) as excinfo:
        await tasks.fork_and_prepare_dist_git(
            jira_issue="RHEL-123",
            package="bash",
            dist_git_branch="rhel-9.9.0",
            available_tools=[],
            agent_type="Backport",
        )

    assert excinfo.value is unrelated_error


async def _async_noop(*_args, **_kwargs):
    pass


@asynccontextmanager
async def _always_acquired_lock(*_args, **_kwargs):
    yield "test-lock-token"


@asynccontextmanager
async def _fake_mcp_tools(*_args, **_kwargs):
    yield []


def _task_payload(data, *, branch: str = "rhel-9.9.0") -> bytes:
    task = Task.from_issue("RHEL-99999")
    task.metadata["target_branch"] = branch
    task.metadata["triage_result"] = {"data": data.model_dump()}
    return task.model_dump_json().encode()


async def _run_policy_process_task(agent_module, payload, monkeypatch):
    """Run one agent's process_task and capture labels/comments/queue pushes."""
    monkeypatch.setenv("COLLECTOR_ENDPOINT", "http://localhost:6006")
    monkeypatch.setenv("REDIS_URL", "redis://localhost")
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://mcp-gateway:8000/sse")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("CONTAINER_VERSION", "c10s")
    monkeypatch.setenv("MAX_RETRIES", "3")

    transaction = flexmock()
    transaction.should_receive("__enter__").and_return(None)
    transaction.should_receive("__exit__").and_return(False)
    span_processor = flexmock()
    span_processor.should_receive("start_transaction").and_return(transaction)

    process_results = []

    async def _mock_run_task_loop(_redis, _queues, process_fn, **_kwargs):
        process_results.append(await process_fn(payload))

    redis_calls = []
    redis_mock = flexmock()

    async def _record_lpush(key, value):
        redis_calls.append((key, value))

    async def _mock_incr(_key):
        return 1

    redis_mock.should_receive("lpush").replace_with(_record_lpush)
    redis_mock.should_receive("incr").replace_with(_mock_incr)

    @asynccontextmanager
    async def _mock_redis_client(*_args, **_kwargs):
        yield redis_mock

    flexmock(agent_module).should_receive("init_sentry")
    flexmock(agent_module).should_receive("configure_logging")
    flexmock(agent_module).should_receive("resolve_chat_model_override")
    flexmock(agent_module).should_receive("setup_observability").and_return(span_processor)
    flexmock(agent_module).should_receive("run_task_loop").replace_with(_mock_run_task_loop)
    flexmock(agent_module).should_receive("redis_client").replace_with(_mock_redis_client)
    flexmock(agent_module).should_receive("issue_lock").replace_with(_always_acquired_lock)
    flexmock(agent_module).should_receive("mcp_tools").replace_with(_fake_mcp_tools)
    flexmock(agent_module).should_receive("create_log_agent").and_return(flexmock())
    if hasattr(agent_module, "create_rebase_agent"):
        flexmock(agent_module).should_receive("create_rebase_agent").and_return(flexmock())
    if hasattr(agent_module, "create_backport_agent"):

        async def _create_backport_agent(*_args, **_kwargs):
            return flexmock()

        flexmock(agent_module).should_receive("create_backport_agent").replace_with(_create_backport_agent)
    if hasattr(agent_module, "find_triaged_rebase_siblings"):

        async def _no_siblings(*_args, **_kwargs):
            return [], None

        flexmock(agent_module).should_receive("find_triaged_rebase_siblings").replace_with(_no_siblings)

    labels = []
    comments = []

    async def _record_labels(**kwargs):
        labels.append(kwargs)

    async def _record_comment(**kwargs):
        comments.append(kwargs)

    flexmock(tasks).should_receive("set_jira_labels").replace_with(_record_labels)
    flexmock(tasks).should_receive("post_terminal_error_comment").replace_with(_record_comment)
    flexmock(tasks).should_receive("change_jira_status").replace_with(_async_noop)
    flexmock(tasks).should_receive("comment_in_jira").replace_with(_async_noop)

    policy_error = tasks.ZstreamBranchPolicyRejected(
        "clean {{rhel-9.9.0}} message\n# Add {{ymir_todo}}",
        package="bash",
        branch="rhel-9.9.0",
        raw_stderr="raw hook output",
    )

    async def _raise_policy(*_args, **_kwargs):
        raise policy_error

    flexmock(tasks).should_receive("fork_and_prepare_dist_git").replace_with(_raise_policy)

    await agent_module.main()
    return policy_error, process_results, labels, comments, redis_calls


@pytest.mark.parametrize(
    "agent_name, data, errored_label",
    [
        (
            "backport_agent",
            BackportData(
                package="bash",
                patch_urls=["https://example.com/fix.patch"],
                justification="test",
                jira_issue="RHEL-99999",
            ),
            JiraLabels.BACKPORT_ERRORED,
        ),
        (
            "rebase_agent",
            RebaseData(package="bash", version="5.2", jira_issue="RHEL-99999"),
            JiraLabels.REBASE_ERRORED,
        ),
        (
            "rebuild_agent",
            RebuildData(package="bash", jira_issue="RHEL-99999"),
            JiraLabels.REBUILD_ERRORED,
        ),
    ],
)
@pytest.mark.asyncio
async def test_policy_rejection_finalizes_without_requeue(monkeypatch, agent_name, data, errored_label):
    agent_module = importlib.import_module(f"ymir.agents.{agent_name}")
    policy_error, process_results, labels, comments, redis_calls = await _run_policy_process_task(
        agent_module, _task_payload(data), monkeypatch
    )

    assert process_results == [None]
    assert any(errored_label.value in call["labels_to_add"] for call in labels)
    assert [call["comment_text"] for call in comments] == [policy_error.message]
    assert [key for key, _value in redis_calls] == [RedisQueues.ERROR_LIST.value]
    assert "raw hook output" in redis_calls[0][1]
