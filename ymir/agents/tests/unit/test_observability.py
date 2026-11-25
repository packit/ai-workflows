"""Unit tests for the agent observability setup."""

from flexmock import flexmock

from ymir.agents import observability


def test_setup_observability_raises_span_attribute_limit():
    captured = {}

    def fake_tracer_provider(resource, span_limits):
        captured["span_limits"] = span_limits
        return flexmock(add_span_processor=lambda processor: None, shutdown=lambda: None)

    flexmock(observability.trace_sdk).should_receive("TracerProvider").replace_with(fake_tracer_provider)
    flexmock(observability).should_receive("BatchSpanProcessor")
    flexmock(observability).should_receive("OTLPSpanExporter")
    flexmock(observability.trace_api).should_receive("set_tracer_provider")
    flexmock(observability.atexit).should_receive("register")
    flexmock(observability.BeeAIInstrumentor).should_receive("instrument")
    flexmock(observability).should_receive("_setup_tool_error_logging")

    observability.setup_observability("http://localhost:4318/v1/traces")

    assert captured["span_limits"].max_span_attributes == observability.SPAN_ATTRIBUTE_LIMIT
    assert observability.SPAN_ATTRIBUTE_LIMIT > 128
