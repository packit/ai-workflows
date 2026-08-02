import atexit
import contextlib
import logging
import re
import threading
from typing import Any

import sentry_sdk
from beeai_framework.emitter import Emitter
from openinference.instrumentation.beeai import BeeAIInstrumentor
from opentelemetry import trace as trace_api
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from ymir.common.logging_setup import current_jira_issue, current_workflow

logger = logging.getLogger(__name__)


class AgentSpanProcessor(SpanProcessor):
    def __init__(self) -> None:
        self._agent_by_span: dict[int, str] = {}
        self._lock = threading.Lock()

    def set_jira_issue(self, jira_issue: str | None) -> None:
        current_jira_issue.set(jira_issue)

    @contextlib.contextmanager
    def jira_issue_context(self, jira_issue: str | None):
        """Set the jira issue attribute on all spans created within the context."""
        token = current_jira_issue.set(jira_issue)
        try:
            yield
        finally:
            current_jira_issue.reset(token)

    @contextlib.contextmanager
    def start_transaction(
        self,
        jira_issue: str | None,
        workflow: str | None,
    ):
        with sentry_sdk.start_transaction(
            op=f"agent.{workflow}", name=f"{workflow} for {jira_issue}"
        ) as transaction:
            transaction.set_data("workflow", workflow)
            transaction.set_data("jira_issue", jira_issue)

            issue_token = current_jira_issue.set(jira_issue)
            workflow_token = current_workflow.set(workflow)
            try:
                yield
            finally:
                current_jira_issue.reset(issue_token)
                current_workflow.reset(workflow_token)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if span.is_recording():
            jira_issue = current_jira_issue.get()
            if jira_issue:
                span.set_attribute("jira.issue", jira_issue)
            workflow = current_workflow.get()
            if workflow:
                span.set_attribute("workflow.name", workflow)
            agent = None
            if span.name.endswith(("Agent", "Analyst")):
                agent = span.name
            if not agent and parent_context:
                parent = trace_api.get_current_span(parent_context)
                if parent and parent.context.span_id:
                    with self._lock:
                        agent = self._agent_by_span.get(parent.context.span_id)
            if agent:
                span.set_attribute("agent.name", agent)
                with self._lock:
                    self._agent_by_span[span.context.span_id] = agent

    def on_end(self, span: ReadableSpan) -> None:
        with self._lock:
            self._agent_by_span.pop(getattr(span.context, "span_id", None), None)

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _setup_tool_error_logging() -> None:
    def on_tool_error(data: Any, meta: Any) -> None:
        if (error := getattr(data, "error", None)) is None:
            return
        if not (additional_context := (getattr(error, "context", None) or {}).get("additional_context")):
            return
        logger.error(f"Tool {meta.creator} additional context: {additional_context}")
        span = trace_api.get_current_span()
        if span.is_recording():
            for key, value in additional_context.items():
                span.set_attribute(f"metadata.{key}", str(value))

    Emitter.root().on(re.compile(r"^tool\..+\.error$"), on_tool_error)


def setup_observability(endpoint: str) -> AgentSpanProcessor:
    resource = Resource(attributes={})
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    processor = AgentSpanProcessor()
    tracer_provider.add_span_processor(processor)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint)))
    trace_api.set_tracer_provider(tracer_provider)
    atexit.register(tracer_provider.shutdown)
    BeeAIInstrumentor().instrument()
    _setup_tool_error_logging()
    return processor
