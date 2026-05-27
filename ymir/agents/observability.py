import atexit
import contextlib
from contextvars import ContextVar

from openinference.instrumentation.beeai import BeeAIInstrumentor
from opentelemetry import trace as trace_api
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor


class AgentSpanProcessor(SpanProcessor):
    _jira_issue_var: ContextVar[str | None] = ContextVar("jira_issue", default=None)

    def set_jira_issue(self, jira_issue: str | None) -> None:
        self._jira_issue_var.set(jira_issue)

    @contextlib.contextmanager
    def jira_issue_context(self, jira_issue: str | None):
        token = self._jira_issue_var.set(jira_issue)
        try:
            yield
        finally:
            self._jira_issue_var.reset(token)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if span.is_recording():
            jira_issue = self._jira_issue_var.get()
            if jira_issue:
                span.set_attribute("jira.issue", jira_issue)

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def setup_observability(endpoint: str) -> AgentSpanProcessor:
    resource = Resource(attributes={})
    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    processor = AgentSpanProcessor()
    tracer_provider.add_span_processor(processor)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint)))
    trace_api.set_tracer_provider(tracer_provider)
    atexit.register(tracer_provider.shutdown)
    BeeAIInstrumentor().instrument()
    return processor
