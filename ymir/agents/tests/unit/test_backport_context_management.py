"""Factory and runner configuration tests for backport context management."""

import pytest
from beeai_framework.agents import AgentExecutionConfig
from beeai_framework.tools.think import ThinkTool
from flexmock import flexmock

from ymir.agents import backport_agent
from ymir.agents.reasoning_agent._runner import ReasoningAgentRunner
from ymir.agents.reasoning_agent.context_management import ManageContextTool


def _render_system(agent) -> str:
    return agent._templates.system.render(
        final_answer_name="final_answer",
        final_answer_schema=None,
        final_answer_instructions=None,
        tool_constraints=None,
        tools=[],
    )


async def _create(monkeypatch, *, enabled, parallel, reasoning=False, older=False, repair=False):
    monkeypatch.setenv("BACKPORT_CONTEXT_MANAGEMENT", "true" if enabled else "false")
    llm = flexmock(allow_parallel_tool_calls=parallel)
    flexmock(backport_agent).should_receive("get_chat_model").once().and_return(llm)
    flexmock(backport_agent).should_receive("is_reasoning_enabled").once().and_return(reasoning)
    flexmock(backport_agent).should_receive("get_tool_call_checker_config").once().and_return(False)
    flexmock(backport_agent).should_receive("get_trajectory_writeable").once().and_return(None)

    async def is_older(*_args, **_kwargs):
        return older

    flexmock(backport_agent).should_receive("is_older_zstream").replace_with(is_older)
    tools = []
    for name in [
        "get_shared_rules",
        "get_maintainer_rules",
        "clone_repository",
        "build_package",
        "download_artifacts",
        "extract_log_snippets",
    ]:
        tool = ThinkTool()
        tool.name = name
        tools.append(tool)
    return await backport_agent.create_backport_agent(
        tools,
        {"working_directory": "/tmp"},
        include_build_tools=repair,
        fix_version="rhel-8.6.z" if older else None,
    )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("reasoning", [False, True])
@pytest.mark.asyncio
async def test_factory_preserves_model_and_gates_context(monkeypatch, enabled, parallel, reasoning):
    agent = await _create(
        monkeypatch,
        enabled=enabled,
        parallel=parallel,
        reasoning=reasoning,
    )
    assert agent._llm.allow_parallel_tool_calls is parallel
    assert agent._enable_context_management is enabled
    assert agent._context_protected_tool_names == (
        ("get_shared_rules", "get_maintainer_rules") if enabled else ()
    )
    prompt = _render_system(agent)
    assert prompt.count("CUMULATIVE DURABLE SUMMARY") == (1 if enabled else 0)
    assert ("multiple independent tool calls" in prompt) is parallel
    assert any(tool.name == "think" for tool in agent._tools) is (not reasoning)


@pytest.mark.parametrize("older", [False, True])
@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.asyncio
async def test_factory_context_partial_and_tool_selection(monkeypatch, older, repair):
    agent = await _create(
        monkeypatch,
        enabled=True,
        parallel=False,
        older=older,
        repair=repair,
    )
    names = {tool.name for tool in agent._tools}
    assert ("clone_repository" in names) is older
    for name in ("build_package", "download_artifacts", "extract_log_snippets"):
        assert (name in names) is repair
    prompt = _render_system(agent)
    assert prompt.count("CUMULATIVE DURABLE SUMMARY") == 1
    assert ("DIST-GIT WORKFLOW" in prompt) is older


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.asyncio
async def test_manage_context_description_matches_parallel_setting(monkeypatch, parallel):
    agent = await _create(monkeypatch, enabled=True, parallel=parallel)
    runner = ReasoningAgentRunner(
        config=AgentExecutionConfig(),
        tool_call_cycle_checker=agent._create_tool_call_checker(),
        force_final_answer_as_tool=True,
        expected_output=None,
        run_context=flexmock(),
        tools=[],
        templates=agent._templates,
        llm=agent._llm,
        enable_context_management=True,
        context_protected_tool_names=agent._context_protected_tool_names,
    )
    context_tool = next(tool for tool in runner._all_tools if isinstance(tool, ManageContextTool))
    assert ("SAME turn" in context_tool.description) is parallel
    assert ("standalone" in context_tool.description) is (not parallel)
    assert ("Do not call it alone" in context_tool.description) is parallel


@pytest.mark.asyncio
async def test_clone_preserves_immutable_policy(monkeypatch):
    agent = await _create(monkeypatch, enabled=True, parallel=False)
    cloned_llm = flexmock(allow_parallel_tool_calls=False)

    async def clone_llm():
        return cloned_llm

    flexmock(agent._llm).should_receive("clone").replace_with(clone_llm)
    cloned = await agent.clone()
    assert cloned._context_protected_tool_names == agent._context_protected_tool_names
    assert isinstance(cloned._context_protected_tool_names, tuple)
