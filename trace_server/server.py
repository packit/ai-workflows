#!/usr/bin/env python3
"""Trace server: receives OTLP spans, stores in SQLite, serves filtered queries.

Receives spans from the OTel Collector via OTLP HTTP and stores them in a
SQLite database. Spans are indexed by Jira issue key and agent type for
efficient querying.

Endpoints
---------
POST /v1/traces
    OTLP HTTP receiver. Accepts OTLP JSON (application/json) trace data.
    The OTel Collector is configured to export here.

GET /traces/
    List all Jira issues that have recorded spans.

    Example: curl https://trace-server.example.com/traces/

GET /traces/<issue>
    Query spans for a specific Jira issue. Supports filtering via query
    parameters (all are optional and combinable):

    agent_type  Filter by agent type (triage, rebase, backport, rebuild,
                merge_request, preliminary_testing).
    trace_id    Return only spans belonging to a specific trace.
    name        Comma-separated span names to include
                (e.g. TriageAgent,think,final_answer).
    last        Return only the N most recent traces (by earliest span
                start time).

    Examples:
        curl https://trace-server.example.com/traces/RHEL-12345
        curl 'https://trace-server.example.com/traces/RHEL-12345?agent_type=triage&last=1'
        curl 'https://trace-server.example.com/traces/RHEL-12345?name=think,final_answer&last=3'

Environment variables
---------------------
TRACE_DB_PATH       Path to the SQLite database file (default: /data/traces.db).
TRACE_SERVER_PORT   Port to listen on (default: 8080).
"""

import gzip
import io
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from renderer import render_issues_html, render_spans_html

DB_PATH = os.environ.get("TRACE_DB_PATH", "/data/traces.db")
PORT = int(os.environ.get("TRACE_SERVER_PORT", "8080"))
MAX_PAYLOAD_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_LAST_TRACES = 900
_STATUS_CODE_NAMES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}

_local = threading.local()


def get_db() -> sqlite3.Connection:
    if not hasattr(_local, "db"):
        _local.db = sqlite3.connect(DB_PATH, timeout=30.0)
        _local.db.row_factory = sqlite3.Row
        _local.db.execute("PRAGMA journal_mode=TRUNCATE")
    return _local.db


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS spans (
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            parent_span_id TEXT,
            name TEXT NOT NULL,
            start_time INTEGER NOT NULL,
            end_time INTEGER,
            status_code INTEGER,
            jira_issue TEXT,
            agent_type TEXT,
            attributes TEXT NOT NULL,
            PRIMARY KEY (trace_id, span_id)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_jira_issue ON spans(jira_issue)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_agent_type ON spans(agent_type)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_start_time ON spans(start_time)")
    db.commit()
    db.close()


def _get_val(value: dict):
    if not isinstance(value, dict):
        return None
    for k in ("stringValue", "intValue", "boolValue", "doubleValue"):
        if k in value:
            return value[k]
    return None


def _extract_spans(otlp_data: dict) -> list[tuple]:
    rows = []
    for rs in otlp_data.get("resourceSpans") or []:
        resource = rs.get("resource") or {}
        resource_attrs = {
            a["key"]: a.get("value") or {}
            for a in resource.get("attributes") or []
            if isinstance(a, dict) and "key" in a
        }
        for ss in rs.get("scopeSpans") or []:
            for span in ss.get("spans") or []:
                span_attrs = {
                    a["key"]: a.get("value") or {}
                    for a in span.get("attributes") or []
                    if isinstance(a, dict) and "key" in a
                }
                all_attrs = {**resource_attrs, **span_attrs}
                jira_issue = _get_val(all_attrs.get("jira.issue"))
                agent_type = _get_val(all_attrs.get("agent.type"))
                status = span.get("status") or {}
                status_code = status.get("code")
                if status_code is None:
                    status_code = 0
                elif isinstance(status_code, str):
                    status_code = _STATUS_CODE_NAMES.get(status_code, 0)
                rows.append(
                    (
                        span.get("traceId", ""),
                        span.get("spanId", ""),
                        span.get("parentSpanId", ""),
                        span.get("name", ""),
                        int(span.get("startTimeUnixNano") or 0),
                        int(span.get("endTimeUnixNano") or 0) or None,
                        int(status_code),
                        jira_issue,
                        agent_type,
                        json.dumps(all_attrs),
                    )
                )
    return rows


def ingest_spans(otlp_data: dict) -> int:
    rows = _extract_spans(otlp_data)
    if not rows:
        return 0
    db = get_db()
    db.executemany(
        """INSERT OR REPLACE INTO spans
           (trace_id, span_id, parent_span_id, name, start_time, end_time,
            status_code, jira_issue, agent_type, attributes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    db.commit()
    return len(rows)


def query_issues() -> list[str]:
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT jira_issue FROM spans WHERE jira_issue IS NOT NULL ORDER BY jira_issue"
    ).fetchall()
    return [r[0] for r in rows]


def query_spans(issue: str, params: dict) -> list[dict]:
    db = get_db()

    # First, find trace IDs that belong to this issue (using jira_issue + filters)
    conditions = ["jira_issue = ?"]
    bindings: list = [issue]

    if agent_type := params.get("agent_type"):
        conditions.append("agent_type = ?")
        bindings.append(agent_type)

    if trace_id := params.get("trace_id"):
        conditions.append("trace_id = ?")
        bindings.append(trace_id)

    if names := params.get("name"):
        name_list = [n.strip() for n in names.split(",")]
        placeholders = ",".join("?" * len(name_list))
        conditions.append(f"name IN ({placeholders})")
        bindings.extend(name_list)

    where = " AND ".join(conditions)

    if last := params.get("last"):
        try:
            n = max(0, min(int(last), MAX_LAST_TRACES))
        except ValueError:
            return []
        subquery = f"""
            SELECT trace_id FROM (
                SELECT trace_id, MIN(start_time) as first_start
                FROM spans WHERE {where}
                GROUP BY trace_id
                ORDER BY first_start DESC
                LIMIT ?
            )
        """
        query_bindings = [*bindings, n]
    else:
        subquery = f"SELECT DISTINCT trace_id FROM spans WHERE {where}"
        query_bindings = bindings

    # Fetch ALL spans from matching traces (including ones without jira_issue)
    rows = db.execute(
        f"""SELECT trace_id, span_id, parent_span_id, name, start_time,
                   end_time, status_code, jira_issue, agent_type, attributes
            FROM spans
            WHERE trace_id IN ({subquery})
            ORDER BY start_time""",
        query_bindings,
    ).fetchall()

    return [
        {
            "trace_id": r["trace_id"],
            "span_id": r["span_id"],
            "parent_span_id": r["parent_span_id"],
            "name": r["name"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "status_code": r["status_code"],
            "jira_issue": r["jira_issue"],
            "agent_type": r["agent_type"],
            "attributes": json.loads(r["attributes"]),
        }
        for r in rows
    ]


class TraceHandler(BaseHTTPRequestHandler):
    def finish(self):
        try:
            db = getattr(_local, "db", None)
            if db is not None:
                db.close()
                del _local.db
        finally:
            super().finish()

    def do_POST(self):
        if self.path == "/v1/traces":
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self._send_json(400, {"error": "invalid content length"})
                return
            if length <= 0:
                self._send_json(400, {"error": "missing content length"})
                return
            if length > MAX_PAYLOAD_SIZE:
                self._send_json(413, {"error": "payload too large"})
                return
            body = self.rfile.read(length)
            if self.headers.get("Content-Encoding", "").lower() == "gzip":
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as f:
                        body = f.read(MAX_PAYLOAD_SIZE + 1)
                except (gzip.BadGzipFile, OSError, EOFError):
                    self._send_json(400, {"error": "invalid gzip payload"})
                    return
                if len(body) > MAX_PAYLOAD_SIZE:
                    self._send_json(413, {"error": "decompressed payload too large"})
                    return
            try:
                data = json.loads(body)
            except ValueError:
                self._send_json(400, {"error": "invalid JSON"})
                return
            try:
                count = ingest_spans(data)
            except Exception as e:
                self._send_json(500, {"error": f"failed to ingest spans: {e}"})
                return
            self._send_json(200, {"accepted": count})
        else:
            self._send_json(404, {"error": "not found"})

    def _wants_html(self) -> bool:
        accept = self.headers.get("Accept", "")
        return "text/html" in accept

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if path == "/health":
            self._send_json(200, {"status": "ok"})
        elif path == "/traces" or path == "":
            issues = query_issues()
            if self._wants_html():
                self._send_html(200, render_issues_html(issues))
            else:
                self._send_json(200, {"issues": issues})
        elif path.startswith("/traces/"):
            issue = path[len("/traces/") :]
            spans = query_spans(issue, params)
            if self._wants_html():
                self._send_html(200, render_spans_html(issue, spans, params))
            else:
                self._send_json(200, {"spans": spans, "count": len(spans)})
        else:
            self._send_json(404, {"error": "not found"})

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, html: str):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code="-", size="-"):
        pass


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), TraceHandler)
    print(f"Trace server listening on port {PORT}, db: {DB_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
