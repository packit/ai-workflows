#!/usr/bin/env python3
"""Trace server: receives OTLP spans, stores in SQLite, serves filtered queries.

Receives spans from the OTel Collector via OTLP HTTP and stores them in a
SQLite database. Spans are indexed by Jira issue key and agent type for
efficient querying.

Agent types are auto-detected from span names ending in ``Agent`` or
``Analyst`` (e.g. ``BackportAgent`` -> ``backport``).  No configuration
is needed when new agent types are added.

Jira issues may be comma-delimited or array-valued in the ``jira.issue``
span attribute.  Issues are propagated across all spans in a trace so
that the entire trace is discoverable from any associated issue.

Endpoints
---------
POST /v1/traces
    OTLP HTTP receiver. Accepts OTLP JSON (application/json) trace data.
    The OTel Collector is configured to export here.

GET /traces/
    List all Jira issues that have recorded spans.

GET /traces/recent
    Return recent root workflow spans without iterating over all issues.

    since       Time window in seconds (default: 10800 = 3 h).
    workflow    Filter to a specific workflow name (e.g. BackportWorkflow).
    limit       Max traces to return (default: 100).

    Example: curl 'https://trace-server.example.com/traces/recent?since=86400'

GET /traces/<issue>
    Query spans for a specific Jira issue. Supports filtering via query
    parameters (all are optional and combinable):

    agent_type  Filter by agent type (auto-detected from span names).
    trace_id    Return only spans belonging to a specific trace.
    name        Comma-separated span names to include
                (e.g. TriageAgent,think,final_answer).
    last        Return only the N most recent traces (by earliest span
                start time).
    since       Only return spans with start_time >= this value
                (nanosecond timestamp). Useful for incremental polling.

    Examples:
        curl https://trace-server.example.com/traces/RHEL-12345
        curl 'https://trace-server.example.com/traces/RHEL-12345?agent_type=triage&last=1'
        curl 'https://trace-server.example.com/traces/RHEL-12345?name=think,final_answer&last=3'

Environment variables
---------------------
TRACE_DB_PATH       Path to the SQLite database file (default: /data/traces.db).
TRACE_SERVER_PORT   Port to listen on (default: 8080).
TRACE_LOG_LEVEL     Log level: DEBUG, INFO, WARNING, ERROR (default: INFO).
"""

import gzip
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("TRACE_DB_PATH", "/data/traces.db")
PORT = int(os.environ.get("TRACE_SERVER_PORT", "8080"))
LOG_LEVEL = os.environ.get("TRACE_LOG_LEVEL", "INFO").upper()
MAX_PAYLOAD_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_LAST_TRACES = 900
STATIC_DIR = Path(__file__).resolve().parent / "static"
_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
_STATUS_CODE_NAMES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}
_SQL_VAR_LIMIT = 500

_local = threading.local()


def get_db() -> sqlite3.Connection:
    if not hasattr(_local, "db"):
        _local.db = sqlite3.connect(DB_PATH, timeout=30.0)
        _local.db.row_factory = sqlite3.Row
        _local.db.execute("PRAGMA journal_mode=TRUNCATE")
    return _local.db


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    logger.debug("Initializing database at %s", DB_PATH)
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
    db.execute("CREATE INDEX IF NOT EXISTS idx_root_spans ON spans(parent_span_id, name)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS span_issues (
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            jira_issue TEXT NOT NULL,
            PRIMARY KEY (trace_id, span_id, jira_issue)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_span_issues_issue ON span_issues(jira_issue)")
    # One-off backfill from spans.jira_issue into the new junction table
    if not db.execute("SELECT 1 FROM span_issues LIMIT 1").fetchone():
        cursor = db.execute("SELECT trace_id, span_id, jira_issue FROM spans WHERE jira_issue IS NOT NULL")
        batch = []
        for r in cursor:
            batch.extend((r[0], r[1], stripped) for issue in r[2].split(",") if (stripped := issue.strip()))
            if len(batch) >= 1000:
                db.executemany(
                    "INSERT OR IGNORE INTO span_issues (trace_id, span_id, jira_issue) VALUES (?, ?, ?)",
                    batch,
                )
                batch.clear()
        if batch:
            db.executemany(
                "INSERT OR IGNORE INTO span_issues (trace_id, span_id, jira_issue) VALUES (?, ?, ?)",
                batch,
            )
    db.commit()
    db.close()


def _get_val(value: dict):
    if not isinstance(value, dict):
        return None
    for k in ("stringValue", "intValue", "boolValue", "doubleValue"):
        if k in value:
            return value[k]
    if "arrayValue" in value and isinstance(value["arrayValue"], dict):
        return [_get_val(v) for v in value["arrayValue"].get("values") or []]
    return None


class SpanRow:
    __slots__ = (
        "agent_type",
        "attributes",
        "end_time",
        "jira_issues",
        "name",
        "parent_span_id",
        "span_id",
        "start_time",
        "status_code",
        "trace_id",
    )

    def __init__(
        self,
        *,
        trace_id,
        span_id,
        parent_span_id,
        name,
        start_time,
        end_time,
        status_code,
        jira_issues,
        agent_type,
        attributes,
    ):
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.start_time = start_time
        self.end_time = end_time
        self.status_code = status_code
        self.jira_issues = jira_issues
        self.agent_type = agent_type
        self.attributes = attributes

    def as_tuple(self):
        return (
            self.trace_id,
            self.span_id,
            self.parent_span_id,
            self.name,
            self.start_time,
            self.end_time,
            self.status_code,
            ",".join(self.jira_issues) if self.jira_issues else None,
            self.agent_type,
            self.attributes,
        )


_CAMEL_CASE_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _agent_type_from_name(name: str) -> str:
    return _CAMEL_CASE_RE.sub("_", name.removesuffix("Agent").removesuffix("Analyst")).lower()


def _propagate_agent_types(spans: list[SpanRow]) -> None:
    """Set agent_type on descendant spans by walking down from *Agent spans."""
    agent_types = {s.span_id: s.agent_type for s in spans if s.agent_type and s.span_id}
    if not agent_types:
        return

    children: dict[str, list[SpanRow]] = {}
    for s in spans:
        if s.parent_span_id:
            children.setdefault(s.parent_span_id, []).append(s)

    visited: set[str] = set()

    for span_id, at in agent_types.items():
        if not span_id or span_id in visited:
            continue
        stack = [(span_id, at)]
        while stack:
            curr_id, curr_at = stack.pop()
            if not curr_id or curr_id in visited:
                continue
            visited.add(curr_id)
            for child in children.get(curr_id, []):
                if not child.agent_type:
                    child.agent_type = curr_at
                stack.append((child.span_id, child.agent_type))


def _parse_jira_issues(raw) -> list[str]:
    """Parse jira.issue attribute into a list of issue keys.

    Handles string (possibly comma-delimited) and array values.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [s for v in raw if isinstance(v, str) and (s := v.strip())]
    return [s for part in str(raw).split(",") if (s := part.strip())]


def _propagate_jira_issues(spans: list[SpanRow]) -> None:
    """Distribute jira_issues across all spans sharing the same trace."""
    by_trace: dict[str, list[SpanRow]] = {}
    for s in spans:
        by_trace.setdefault(s.trace_id, []).append(s)

    for trace_spans in by_trace.values():
        all_issues: set[str] = set()
        for s in trace_spans:
            all_issues.update(s.jira_issues)
        if not all_issues:
            continue
        merged = sorted(all_issues)
        for s in trace_spans:
            s.jira_issues = merged


def _extract_spans(otlp_data: dict) -> list[SpanRow]:
    spans = []
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
                name = span.get("name") or ""
                status = span.get("status") or {}
                status_code = status.get("code")
                if status_code is None:
                    status_code = 0
                elif isinstance(status_code, str):
                    status_code = _STATUS_CODE_NAMES.get(status_code, 0)
                spans.append(
                    SpanRow(
                        trace_id=span.get("traceId") or "",
                        span_id=span.get("spanId") or "",
                        parent_span_id=span.get("parentSpanId") or "",
                        name=name,
                        start_time=int(span.get("startTimeUnixNano") or 0),
                        end_time=int(span.get("endTimeUnixNano") or 0) or None,
                        status_code=int(status_code),
                        jira_issues=_parse_jira_issues(_get_val(all_attrs.get("jira.issue"))),
                        agent_type=_agent_type_from_name(name)
                        if name.endswith(("Agent", "Analyst"))
                        else None,
                        attributes=json.dumps(all_attrs),
                    )
                )

    _propagate_agent_types(spans)
    _propagate_jira_issues(spans)
    return spans


def ingest_spans(otlp_data: dict) -> int:
    spans = _extract_spans(otlp_data)
    if not spans:
        logger.debug("No spans found in OTLP payload")
        return 0
    db = get_db()
    db.executemany(
        """INSERT OR REPLACE INTO spans
           (trace_id, span_id, parent_span_id, name, start_time, end_time,
            status_code, jira_issue, agent_type, attributes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [s.as_tuple() for s in spans],
    )
    span_keys = [(s.trace_id, s.span_id) for s in spans]
    db.executemany(
        "DELETE FROM span_issues WHERE trace_id = ? AND span_id = ?",
        span_keys,
    )
    issue_rows = [(s.trace_id, s.span_id, issue) for s in spans for issue in s.jira_issues]
    if issue_rows:
        db.executemany(
            "INSERT OR IGNORE INTO span_issues (trace_id, span_id, jira_issue) VALUES (?, ?, ?)",
            issue_rows,
        )
    # Propagate agent_type to spans missing it in affected traces
    trace_ids = list({s.trace_id for s in spans})
    agent_rows: list = []
    null_rows: list = []
    for i in range(0, len(trace_ids), _SQL_VAR_LIMIT):
        chunk = trace_ids[i : i + _SQL_VAR_LIMIT]
        ph = ",".join("?" * len(chunk))
        agent_rows.extend(
            db.execute(
                f"SELECT trace_id, span_id, agent_type FROM spans "  # noqa: S608
                f"WHERE trace_id IN ({ph}) AND agent_type IS NOT NULL",
                chunk,
            ).fetchall()
        )
        null_rows.extend(
            db.execute(
                f"SELECT trace_id, span_id, parent_span_id FROM spans "  # noqa: S608
                f"WHERE trace_id IN ({ph}) AND agent_type IS NULL",
                chunk,
            ).fetchall()
        )
    if agent_rows and null_rows:
        children: dict[tuple[str, str], list[dict]] = {}
        for row in null_rows:
            if row["parent_span_id"]:
                children.setdefault((row["trace_id"], row["parent_span_id"]), []).append(row)
        updates = []
        for ar in agent_rows:
            stack = list(children.get((ar["trace_id"], ar["span_id"]), []))
            while stack:
                child = stack.pop()
                updates.append((ar["agent_type"], child["trace_id"], child["span_id"]))
                stack.extend(children.get((child["trace_id"], child["span_id"]), []))
        if updates:
            db.executemany(
                "UPDATE spans SET agent_type = ? WHERE trace_id = ? AND span_id = ?",
                updates,
            )

    # Propagate jira_issues to previously-stored spans in the same traces
    all_issues_by_trace: dict[str, set[str]] = {}
    for s in spans:
        if s.jira_issues:
            all_issues_by_trace.setdefault(s.trace_id, set()).update(s.jira_issues)
    if all_issues_by_trace:
        for trace_id, new_issues in all_issues_by_trace.items():
            for issue in new_issues:
                db.execute(
                    "INSERT OR IGNORE INTO span_issues (trace_id, span_id, jira_issue) "
                    "SELECT trace_id, span_id, ? FROM spans WHERE trace_id = ?",
                    (issue, trace_id),
                )

    db.commit()
    all_issues = {issue for s in spans for issue in s.jira_issues}
    logger.debug(
        "Ingested %d spans (%d issues)",
        len(spans),
        len(all_issues),
    )
    return len(spans)


def query_issues() -> list[str]:
    db = get_db()
    rows = db.execute("SELECT DISTINCT jira_issue FROM span_issues ORDER BY jira_issue").fetchall()
    return [r[0] for r in rows]


def query_spans(issue: str, params: dict) -> list[dict]:
    db = get_db()

    # When issue is '_', query by trace_id directly (no issue association)
    if issue == "_":
        trace_id = params.get("trace_id")
        if not trace_id:
            return []
        query_bindings: list = [trace_id]
        subquery = "SELECT ? AS trace_id"

        since_filter = ""
        if since_ns := params.get("since"):
            try:
                since_filter = " AND start_time >= ?"
                query_bindings.append(int(since_ns))
            except (ValueError, TypeError):
                pass

        rows = db.execute(
            f"""SELECT trace_id, span_id, parent_span_id, name, start_time,
                       end_time, status_code, jira_issue, agent_type, attributes
                FROM spans
                WHERE trace_id IN ({subquery}){since_filter}
                ORDER BY start_time""",  # noqa: S608
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

    # Find trace IDs via the span_issues junction table, then apply filters on spans
    issue_conditions = ["si.jira_issue = ?"]
    issue_bindings: list = [issue]
    span_conditions: list[str] = []
    span_bindings: list = []

    if agent_type := params.get("agent_type"):
        span_conditions.append("s.agent_type = ?")
        span_bindings.append(agent_type)

    if trace_id := params.get("trace_id"):
        issue_conditions.append("si.trace_id = ?")
        issue_bindings.append(trace_id)

    if names := params.get("name"):
        name_list = [n.strip() for n in names.split(",")]
        placeholders = ",".join("?" * len(name_list))
        span_conditions.append(f"s.name IN ({placeholders})")
        span_bindings.extend(name_list)

    bindings = issue_bindings + span_bindings
    join_where = " AND ".join(issue_conditions)
    span_where = " AND " + " AND ".join(span_conditions) if span_conditions else ""

    if last := params.get("last"):
        try:
            n = max(0, min(int(last), MAX_LAST_TRACES))
        except ValueError:
            return []
        subquery = f"""
            SELECT trace_id FROM (
                SELECT si.trace_id, MIN(s.start_time) as first_start
                FROM span_issues si
                JOIN spans s ON si.trace_id = s.trace_id AND si.span_id = s.span_id
                WHERE {join_where}{span_where}
                GROUP BY si.trace_id
                ORDER BY first_start DESC
                LIMIT ?
            )
        """  # noqa: S608
        query_bindings = [*bindings, n]
    else:
        if span_conditions:
            subquery = f"""
                SELECT DISTINCT si.trace_id
                FROM span_issues si
                JOIN spans s ON si.trace_id = s.trace_id AND si.span_id = s.span_id
                WHERE {join_where}{span_where}
            """  # noqa: S608
        else:
            subquery = f"SELECT DISTINCT trace_id FROM span_issues si WHERE {join_where}"  # noqa: S608
        query_bindings = bindings

    since_filter = ""
    if since_ns := params.get("since"):
        try:
            since_filter = " AND start_time >= ?"
            query_bindings.append(int(since_ns))
        except (ValueError, TypeError):
            pass

    # Fetch ALL spans from matching traces
    rows = db.execute(
        f"""SELECT trace_id, span_id, parent_span_id, name, start_time,
                   end_time, status_code, jira_issue, agent_type, attributes
            FROM spans
            WHERE trace_id IN ({subquery}){since_filter}
            ORDER BY start_time""",  # noqa: S608
        query_bindings,
    ).fetchall()

    logger.debug(
        "query_spans(%r, %r) returned %d spans",
        issue,
        params,
        len(rows),
    )
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


def query_recent_traces(since_ns: int, workflow: str | None, limit: int) -> list[dict]:
    """Return recent traces, including in-progress ones whose root span hasn't arrived yet."""
    db = get_db()
    effective_limit = min(limit, MAX_LAST_TRACES)

    # Completed traces: root workflow span exists
    root_conditions = ["parent_span_id = ''", "name LIKE '%Workflow'", "start_time >= ?"]
    root_bindings: list = [since_ns]
    if workflow:
        root_conditions.append("name = ?")
        root_bindings.append(workflow)
    root_where = " AND ".join(root_conditions)

    root_rows = db.execute(
        f"""SELECT s.trace_id, s.name, s.start_time, s.end_time, s.status_code
            FROM spans s
            WHERE {root_where}
            ORDER BY s.start_time DESC
            LIMIT ?""",  # noqa: S608
        [*root_bindings, effective_limit],
    ).fetchall()

    root_trace_ids = {r["trace_id"] for r in root_rows}

    # In-progress traces: have spans linked to jira issues in the time window
    # but no root Workflow span yet
    inprog_filter = ""
    inprog_bindings: list = [since_ns]
    if workflow:
        wf_base = workflow.removesuffix("Workflow").lower()
        inprog_filter = " AND json_extract(s.attributes, '$.\"workflow.name\".stringValue') = ?"
        inprog_bindings.append(wf_base)
    inprog_rows = db.execute(
        f"""SELECT s.trace_id, MIN(s.start_time) as first_start,
                  MAX(json_extract(s.attributes, '$."workflow.name".stringValue')) as workflow_name
            FROM spans s
            JOIN span_issues si ON s.trace_id = si.trace_id AND s.span_id = si.span_id
            WHERE s.start_time >= ?{inprog_filter}
            GROUP BY s.trace_id
            HAVING s.trace_id NOT IN (
                SELECT trace_id FROM spans
                WHERE parent_span_id = '' AND name LIKE '%Workflow'
            )
            ORDER BY first_start DESC
            LIMIT ?""",  # noqa: S608
        [*inprog_bindings, effective_limit],
    ).fetchall()

    all_trace_ids = list(root_trace_ids | {r["trace_id"] for r in inprog_rows})
    if not all_trace_ids:
        return []

    ph = ",".join("?" * len(all_trace_ids))

    issue_rows = db.execute(
        f"SELECT trace_id, jira_issue FROM span_issues WHERE trace_id IN ({ph}) "  # noqa: S608
        "GROUP BY trace_id, jira_issue",
        all_trace_ids,
    ).fetchall()
    issues_by_trace: dict[str, list[str]] = {}
    for ir in issue_rows:
        issues_by_trace.setdefault(ir["trace_id"], []).append(ir["jira_issue"])

    count_rows = db.execute(
        f"SELECT trace_id, COUNT(*) as cnt, "  # noqa: S608
        "SUM(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) as errors "
        f"FROM spans WHERE trace_id IN ({ph}) GROUP BY trace_id",
        all_trace_ids,
    ).fetchall()
    counts_by_trace = {cr["trace_id"]: cr for cr in count_rows}

    results = []
    for r in root_rows:
        tid = r["trace_id"]
        counts = counts_by_trace.get(tid)
        results.append(
            {
                "trace_id": tid,
                "workflow": r["name"],
                "issues": sorted(issues_by_trace.get(tid, [])),
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "status_code": r["status_code"],
                "num_spans": counts["cnt"] if counts else 0,
                "error_count": counts["errors"] if counts else 0,
            }
        )

    for r in inprog_rows:
        tid = r["trace_id"]
        if tid in root_trace_ids:
            continue
        counts = counts_by_trace.get(tid)
        wf_name = r["workflow_name"]
        if wf_name:
            wf_name = wf_name[0].upper() + wf_name[1:] + "Workflow"
        results.append(
            {
                "trace_id": tid,
                "workflow": wf_name or "(in progress)",
                "issues": sorted(issues_by_trace.get(tid, [])),
                "start_time": r["first_start"],
                "end_time": None,
                "status_code": 0,
                "num_spans": counts["cnt"] if counts else 0,
                "error_count": counts["errors"] if counts else 0,
            }
        )

    results.sort(key=lambda x: x["start_time"] or 0, reverse=True)
    return results[:effective_limit]


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
                logger.warning("POST /v1/traces rejected: invalid content length")
                self._send_json(400, {"error": "invalid content length"})
                return
            if length <= 0:
                logger.warning("POST /v1/traces rejected: missing content length")
                self._send_json(400, {"error": "missing content length"})
                return
            if length > MAX_PAYLOAD_SIZE:
                logger.warning("POST /v1/traces rejected: payload too large (%d bytes)", length)
                self._send_json(413, {"error": "payload too large"})
                return
            logger.debug("POST /v1/traces content-length=%d", length)
            body = self.rfile.read(length)
            if self.headers.get("Content-Encoding", "").lower() == "gzip":
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as f:
                        body = f.read(MAX_PAYLOAD_SIZE + 1)
                except (gzip.BadGzipFile, OSError, EOFError):
                    logger.warning("POST /v1/traces rejected: invalid gzip payload")
                    self._send_json(400, {"error": "invalid gzip payload"})
                    return
                if len(body) > MAX_PAYLOAD_SIZE:
                    logger.warning(
                        "POST /v1/traces rejected: decompressed payload too large (%d bytes)",
                        len(body),
                    )
                    self._send_json(413, {"error": "decompressed payload too large"})
                    return
                logger.debug("Decompressed gzip payload to %d bytes", len(body))
            try:
                data = json.loads(body)
            except ValueError:
                logger.warning("POST /v1/traces rejected: invalid JSON")
                self._send_json(400, {"error": "invalid JSON"})
                return
            try:
                count = ingest_spans(data)
            except Exception as e:
                logger.exception("Failed to ingest spans")
                self._send_json(500, {"error": f"failed to ingest spans: {e}"})
                return
            logger.info("Accepted %d spans", count)
            self._send_json(200, {"accepted": count})
        else:
            logger.debug("POST %s not found", self.path)
            self._send_json(404, {"error": "not found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        logger.debug("GET %s params=%r", self.path, params)

        if path == "" or path == "/index.html":
            self._send_file(STATIC_DIR / "index.html")
        elif path.startswith("/static/"):
            rel = path[len("/static/") :]
            filepath = (STATIC_DIR / rel).resolve()
            if not filepath.is_relative_to(STATIC_DIR.resolve()):
                self._send_json(404, {"error": "not found"})
                return
            self._send_file(filepath)
        elif path == "/health":
            self._send_json(200, {"status": "ok"})
        elif path == "/traces/recent":
            try:
                since_s = int(params.get("since", 10800))
            except ValueError:
                since_s = 10800
            since_ns = (int(time.time()) - since_s) * 1_000_000_000
            workflow = params.get("workflow")
            try:
                limit = max(1, min(int(params.get("limit", 100)), MAX_LAST_TRACES))
            except ValueError:
                limit = 100
            try:
                traces = query_recent_traces(since_ns, workflow, limit)
            except Exception as e:
                logger.exception("Failed to query recent traces")
                self._send_json(500, {"error": f"failed to query recent traces: {e}"})
                return
            logger.debug("recent_traces returned %d traces", len(traces))
            self._send_json(200, {"traces": traces, "count": len(traces)})
        elif path == "/traces":
            try:
                issues = query_issues()
            except Exception as e:
                logger.exception("Failed to query issues")
                self._send_json(500, {"error": f"failed to query issues: {e}"})
                return
            logger.debug("Listed %d issues", len(issues))
            self._send_json(200, {"issues": issues})
        elif path.startswith("/traces/"):
            issue = path[len("/traces/") :]
            try:
                spans = query_spans(issue, params)
            except Exception as e:
                logger.exception("Failed to query spans for %s", issue)
                self._send_json(500, {"error": f"failed to query spans: {e}"})
                return
            self._send_json(200, {"spans": spans, "count": len(spans)})
        else:
            logger.debug("GET %s not found", self.path)
            self._send_json(404, {"error": "not found"})

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, filepath: Path):
        if not filepath.is_file():
            self._send_json(404, {"error": "not found"})
            return
        try:
            body = filepath.read_bytes()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        ext = filepath.suffix.lower()
        content_type = _MIME_TYPES.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


def main():
    configure_logging()
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), TraceHandler)  # noqa: S104
    logger.info("Trace server listening on port %d, db: %s", PORT, DB_PATH)
    server.serve_forever()


if __name__ == "__main__":
    main()
