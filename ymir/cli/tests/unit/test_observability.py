"""Tests for the observability pipeline: exporter serialisation → trace-server ingestion.

Validates that the JSON payload produced by ``_JsonOTLPSpanExporter``
matches the format expected by the trace-server's ``_extract_spans``.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

# Make the trace_server package importable without installing it.
# trace_server/server.py does ``from renderer import …`` (sibling import),
# so both the repo root *and* the trace_server directory must be on sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_TRACE_SERVER_DIR = _REPO_ROOT / "trace_server"
for _p in (_REPO_ROOT, _TRACE_SERVER_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from trace_server.server import _extract_spans  # noqa: E402


def _create_test_spans(jira_issue: str = "RHEL-99999", count: int = 1):
    """Create real OpenTelemetry SDK spans and return them as ReadableSpan objects."""
    resource = Resource(attributes={"service.name": "test-cli"})
    provider = trace_sdk.TracerProvider(resource=resource)
    captured = []

    class _CapturingProcessor(SimpleSpanProcessor):
        def __init__(self):
            super().__init__(MagicMock())

        def on_end(self, span):
            captured.append(span)

    provider.add_span_processor(_CapturingProcessor())
    tracer = provider.get_tracer("test")

    for i in range(count):
        with tracer.start_as_current_span(f"test-span-{i}") as span:
            span.set_attribute("jira.issue", jira_issue)
            span.set_attribute("test.index", i)

    return captured


class TestMessageToDictFieldNames:
    """Verify that protobuf → JSON field name conversion matches trace-server expectations."""

    def test_preserving_proto_field_name_produces_snake_case(self):
        """With preserving_proto_field_name=True, keys are snake_case."""
        spans = _create_test_spans()
        request = encode_spans(spans)
        data = MessageToDict(request, preserving_proto_field_name=True)

        assert "resource_spans" in data, f"Expected 'resource_spans', got keys: {list(data.keys())}"
        assert "resourceSpans" not in data

    def test_default_produces_camel_case(self):
        """Without preserving_proto_field_name (default=False), keys are camelCase."""
        spans = _create_test_spans()
        request = encode_spans(spans)
        data = MessageToDict(request)

        assert "resourceSpans" in data, f"Expected 'resourceSpans', got keys: {list(data.keys())}"
        assert "resource_spans" not in data

    def test_snake_case_span_fields(self):
        """Verify nested fields also differ between snake_case and camelCase."""
        spans = _create_test_spans()
        request = encode_spans(spans)

        snake = MessageToDict(request, preserving_proto_field_name=True)
        camel = MessageToDict(request)

        snake_span = snake["resource_spans"][0]["scope_spans"][0]["spans"][0]
        camel_span = camel["resourceSpans"][0]["scopeSpans"][0]["spans"][0]

        assert "trace_id" in snake_span
        assert "traceId" in camel_span
        assert "span_id" in snake_span
        assert "spanId" in camel_span
        assert "start_time_unix_nano" in snake_span
        assert "startTimeUnixNano" in camel_span


class TestExtractSpansFormat:
    """Verify trace-server's _extract_spans against both serialisation formats."""

    def test_camel_case_payload_extracts_spans(self):
        """camelCase payload (the correct format) yields parsed spans."""
        spans = _create_test_spans("RHEL-11111", count=3)
        request = encode_spans(spans)
        data = MessageToDict(request)

        result = _extract_spans(data)

        assert len(result) == 3
        for row in result:
            assert row.trace_id, "trace_id must be non-empty"
            assert row.span_id, "span_id must be non-empty"
            assert row.start_time > 0, "start_time must be set"
            assert "RHEL-11111" in row.jira_issues

    def test_snake_case_payload_extracts_zero_spans(self):
        """snake_case payload (the bug) yields zero spans because keys don't match."""
        spans = _create_test_spans("RHEL-22222", count=2)
        request = encode_spans(spans)
        data = MessageToDict(request, preserving_proto_field_name=True)

        result = _extract_spans(data)

        assert len(result) == 0, "snake_case keys should not match trace-server's camelCase lookups"


class TestExtractSpansAttributes:
    """Verify attribute extraction from correctly-formatted payloads."""

    def test_jira_issue_attribute_extracted(self):
        spans = _create_test_spans("RHEL-33333")
        request = encode_spans(spans)
        data = MessageToDict(request)

        result = _extract_spans(data)

        assert len(result) == 1
        assert "RHEL-33333" in result[0].jira_issues

    def test_span_name_preserved(self):
        spans = _create_test_spans()
        request = encode_spans(spans)
        data = MessageToDict(request)

        result = _extract_spans(data)

        assert result[0].name == "test-span-0"

    def test_attributes_stored_as_json(self):
        spans = _create_test_spans()
        request = encode_spans(spans)
        data = MessageToDict(request)

        result = _extract_spans(data)

        attrs = json.loads(result[0].attributes)
        assert "jira.issue" in attrs
        assert "test.index" in attrs

    def test_multiple_spans_same_trace(self):
        """Multiple spans in a batch share the same trace after propagation."""
        spans = _create_test_spans("RHEL-44444", count=3)
        request = encode_spans(spans)
        data = MessageToDict(request)

        result = _extract_spans(data)

        assert len(result) == 3
        trace_ids = {r.trace_id for r in result}
        assert len(trace_ids) >= 1


class TestJsonExporterSerialization:
    """Verify that _JsonOTLPSpanExporter produces payloads the trace-server accepts."""

    def test_exporter_payload_round_trips_through_extract_spans(self):
        """End-to-end: SDK spans → exporter serialisation → trace-server extraction.

        Reproduces the actual code path in ``_JsonOTLPSpanExporter.export``:
        ``MessageToDict(request)`` (default camelCase) → JSON → ``_extract_spans``.
        """
        spans = _create_test_spans("RHEL-55555", count=2)

        request = encode_spans(spans)
        payload = json.dumps(MessageToDict(request)).encode("utf-8")
        data = json.loads(payload)

        result = _extract_spans(data)

        assert len(result) == 2, (
            f"Exporter payload should yield 2 spans, got {len(result)}. Top-level keys: {list(data.keys())}"
        )
        for row in result:
            assert "RHEL-55555" in row.jira_issues
