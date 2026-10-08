"""Consolidation descriptions link to the exported run, including concurrent jobs."""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from beeai_framework.workflows import Workflow
from flexmock import flexmock
from openinference.instrumentation.beeai import BeeAIInstrumentor
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ymir.agents import mr_consolidation_agent as agent


@pytest.mark.parametrize("tracing_enabled", [False, True])
@pytest.mark.asyncio
async def test_consolidation_run_links_match_exported_traces(monkeypatch, tracing_enabled):
    monkeypatch.setenv("MCP_GATEWAY_URL", "http://gateway.invalid/sse")
    monkeypatch.setenv("TRACE_VIEWER_URL", "https://trace.example")

    @contextlib.asynccontextmanager
    async def mcp_tools(*args, **kwargs):
        yield []

    flexmock(agent).should_receive("mcp_tools").replace_with(mcp_tools)
    flexmock(agent).should_receive("create_log_agent").and_return(SimpleNamespace())
    flexmock(agent).should_receive("get_mock_local_tool_env").and_return(None)

    # Exercise the real publication step in dry-run mode; no clones, LLMs,
    # GitLab writes, or Jira writes are needed to render the description.
    run = Workflow.run

    def start_at_publication(workflow, state, options=None):
        state.mr_titles = ["First fix", "Second fix"]
        state.mr_descriptions = ["First description", "Second description"]
        state.jira_issues_collected = []
        workflow.set_start("push_and_open_mr")
        return run(workflow, state, options)

    monkeypatch.setattr(Workflow, "run", start_at_publication)
    descriptions = {}
    build_description = agent._build_consolidated_description

    def capture_description(*args, **kwargs):
        description = build_description(*args, **kwargs)
        descriptions[args[4]] = description
        return description

    flexmock(agent).should_receive("_build_consolidated_description").replace_with(capture_description)

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    active_provider = provider if tracing_enabled else trace.NoOpTracerProvider()
    instrumentor = BeeAIInstrumentor()
    instrumentor.instrument(tracer_provider=active_provider)
    flexmock(agent.trace_api).should_receive("get_tracer").with_args(agent.__name__).and_return(
        active_provider.get_tracer(agent.__name__)
    )
    try:
        await asyncio.gather(
            *(
                agent.run_workflow(
                    package=package,
                    dist_git_branch="c10s",
                    dry_run=True,
                    consolidation_agent_factory=lambda *args: SimpleNamespace(),
                )
                for package in ("bash", "zsh")
            )
        )

        assert set(descriptions) == {"bash", "zsh"}
        spans = exporter.get_finished_spans()
        if not tracing_enabled:
            assert not spans
            assert all("Execution traces" not in description for description in descriptions.values())
            return

        roots = [span for span in spans if span.parent is None]
        workflows = [span for span in spans if span.name == "MRConsolidationWorkflow"]
        assert len(roots) == len(workflows) == 2
        assert {span.name for span in roots} == {"MRConsolidationJobWorkflow"}
        urls = {
            f"https://trace.example/#/trace/_/{trace.format_trace_id(span.context.trace_id)}"
            for span in roots
        }
        assert len(urls) == 2
        for description in descriptions.values():
            assert sum(url in description for url in urls) == 1
        for url in urls:
            assert sum(url in description for description in descriptions.values()) == 1
        for workflow in workflows:
            assert any(
                workflow.context.trace_id == root.context.trace_id
                and workflow.parent.span_id == root.context.span_id
                for root in roots
            )
        assert not trace.get_current_span().get_span_context().is_valid
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
