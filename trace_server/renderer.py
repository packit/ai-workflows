"""HTML rendering for the trace server."""

from datetime import UTC, datetime
from html import escape


def _get_val(value: dict):
    if not isinstance(value, dict):
        return None
    for k in ("stringValue", "intValue", "boolValue", "doubleValue"):
        if k in value:
            return value[k]
    return None


STATUS_LABELS = {0: "Unset", 1: "Ok", 2: "Error"}

HTML_HEAD = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<script>
try{{if(localStorage.getItem('theme')==='dark')document.documentElement.classList.add('dark')}}catch(e){{}}
</script>
<style>
  :root {{
    --bg: #fafafa; --fg: #333; --link: #0366d6;
    --table-border: #ddd; --th-bg: #f0f0f0; --row-even: #f8f8f8;
    --btn-bg: #f0f0f0; --btn-border: #ccc; --btn-hover: #e0e0e0;
    --summary-fg: #586069; --pre-bg: #f6f8fa;
    --reasoning-bg: #fff8e1; --reasoning-border: #f9a825;
    --tool-bg: #e8f5e9; --tool-border: #43a047;
    --error-bg: #fdecea; --error-border: #cb2431;
    --attr-key: #6f42c1;
    --status-ok: #22863a; --status-error: #cb2431;
  }}
  .dark {{
    --bg: #1e1e1e; --fg: #d4d4d4; --link: #58a6ff;
    --table-border: #444; --th-bg: #2d2d2d; --row-even: #262626;
    --btn-bg: #333; --btn-border: #555; --btn-hover: #444;
    --summary-fg: #999; --pre-bg: #2d2d2d;
    --reasoning-bg: #3a3000; --reasoning-border: #f9a825;
    --tool-bg: #1a3a1a; --tool-border: #43a047;
    --error-bg: #3a1a1a; --error-border: #cb2431;
    --attr-key: #c9a0ff;
    --status-ok: #3fb950; --status-error: #f85149;
  }}
  body {{ font-family: system-ui, sans-serif; margin: 2em;
         background: var(--bg); color: var(--fg); }}
  h1 {{ color: var(--fg); }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1em; }}
  th, td {{ border: 1px solid var(--table-border); padding: 6px 10px;
            text-align: left; font-size: 0.9em; }}
  th {{ background: var(--th-bg); }}
  tr:nth-child(even) {{ background: var(--row-even); }}
  .status-ok {{ color: var(--status-ok); }}
  .status-error {{ color: var(--status-error); font-weight: bold; }}
  .attr-key {{ color: var(--attr-key); }}
  details {{ margin-top: 2px; }}
  details summary {{ cursor: pointer; color: var(--summary-fg); font-size: 0.85em; }}
  pre {{ margin: 4px 0; font-size: 0.8em; max-height: 300px; overflow: auto;
         background: var(--pre-bg); padding: 8px; border-radius: 4px; }}
  .nav {{ margin-bottom: 1em; font-size: 0.9em; }}
  .detail-reasoning {{ background: var(--reasoning-bg); border-left: 3px solid var(--reasoning-border);
                       padding: 4px 8px; margin: 2px 0; font-size: 0.85em; white-space: pre-wrap; }}
  .detail-text {{ padding: 4px 8px; margin: 2px 0; font-size: 0.85em; white-space: pre-wrap; }}
  .detail-tool-call {{ background: var(--tool-bg); border-left: 3px solid var(--tool-border);
                       padding: 4px 8px; margin: 2px 0; font-size: 0.85em;
                       font-family: monospace; word-break: break-all; }}
  .detail-tool-name {{ font-weight: bold; font-size: 0.85em; margin-bottom: 2px; }}
  .detail-error {{ background: var(--error-bg); border-left: 3px solid var(--error-border);
                   padding: 4px 8px; margin: 2px 0; font-size: 0.85em; white-space: pre-wrap; }}
  .toolbar {{ margin-top: 0.5em; font-size: 0.85em; display: flex; gap: 6px; }}
  .toolbar button {{ background: var(--btn-bg); border: 1px solid var(--btn-border);
                     border-radius: 4px; padding: 3px 10px; cursor: pointer;
                     font-size: 0.85em; color: var(--fg); }}
  .toolbar button:hover {{ background: var(--btn-hover); }}
  .collapsible {{ display: none; }}
  .cols-expanded .collapsible {{ display: table-cell; }}
</style></head><body>
"""

HTML_FOOT = """\
<script>
function toggleTheme(){{document.documentElement.classList.toggle('dark');
try{{localStorage.setItem('theme',document.documentElement.classList.contains('dark')?'dark':'light')}}catch(e){{}}}}
</script></body></html>"""


def _fmt_time(nanos: int | None) -> str:
    if not nanos:
        return "-"
    return datetime.fromtimestamp(nanos / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_duration(start: int, end: int | None) -> str:
    if not end or not start:
        return "-"
    ms = (end - start) / 1e6
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.1f}s"


def _status_class(code: int) -> str:
    if code == 2:
        return "status-error"
    if code == 1:
        return "status-ok"
    return ""


def _extract_detail(attrs: dict, span_name: str = "") -> str | None:
    span_kind = _get_val(attrs.get("openinference.span.kind"))

    if span_kind == "LLM":
        parts = []
        i = 0
        while True:
            ctype = _get_val(attrs.get(f"llm.output_messages.0.message.contents.{i}.message_content.type"))
            if ctype is None:
                break
            if ctype == "reasoning":
                text = _get_val(attrs.get(f"llm.output_messages.0.message.contents.{i}.message_content.text"))
                if text:
                    parts.append(("reasoning", text))
            elif ctype == "text":
                text = _get_val(attrs.get(f"llm.output_messages.0.message.contents.{i}.message_content.text"))
                if text:
                    parts.append(("text", text))
            i += 1

        i = 0
        while True:
            name = _get_val(
                attrs.get(f"llm.output_messages.0.message.tool_calls.{i}.tool_call.function.name")
            )
            if name is None:
                break
            args = (
                _get_val(
                    attrs.get(f"llm.output_messages.0.message.tool_calls.{i}.tool_call.function.arguments")
                )
                or ""
            )
            parts.append(("tool_call", f"{name}({args})"))
            i += 1

        if not parts:
            return None

        html = []
        for kind, content in parts:
            if kind == "reasoning":
                html.append(f'<div class="detail-reasoning">{escape(content)}</div>')
            elif kind == "text":
                html.append(f'<div class="detail-text">{escape(content)}</div>')
            elif kind == "tool_call":
                html.append(f'<div class="detail-tool-call">{escape(content)}</div>')
        return "".join(html)

    if span_name == "error":
        output_val = _get_val(attrs.get("output.value"))
        if output_val:
            output_str = str(output_val)
            truncated = output_str[:500] + ("..." if len(output_str) > 500 else "")
            return f'<div class="detail-error">{escape(truncated)}</div>'
        return None

    if span_kind == "TOOL":
        tool_name = _get_val(attrs.get("tool.name"))
        input_val = _get_val(attrs.get("input.value"))
        output_val = _get_val(attrs.get("output.value"))
        if not tool_name:
            return None
        html = [f'<div class="detail-tool-name">{escape(tool_name)}</div>']
        if input_val is not None:
            input_str = str(input_val)
            truncated = input_str[:500] + ("..." if len(input_str) > 500 else "")
            html.append(f"<details><summary>input</summary><pre>{escape(truncated)}</pre></details>")
        if output_val is not None:
            output_str = str(output_val)
            truncated = output_str[:500] + ("..." if len(output_str) > 500 else "")
            html.append(f"<details><summary>output</summary><pre>{escape(truncated)}</pre></details>")
        return "".join(html)

    return None


def _render_attrs(attrs: dict) -> str:
    if not attrs:
        return ""
    rows = []
    for k, v in sorted(attrs.items()):
        val = _get_val(v) if isinstance(v, dict) else v
        rows.append(f'<span class="attr-key">{escape(str(k))}</span>: {escape(str(val))}')
    content = "<br>".join(rows)
    return f"<details><summary>{len(attrs)} attributes</summary><pre>{content}</pre></details>"


def render_issues_html(issues: list[str]) -> str:
    parts = [HTML_HEAD.format(title="Traces")]
    parts.append('<div class="toolbar"><button onclick="toggleTheme()">Toggle dark mode</button></div>')
    parts.append("<h1>Traced Issues</h1>")
    if not issues:
        parts.append("<p>No traces recorded yet.</p>")
    else:
        parts.append("<ul>")
        parts.extend(f'<li><a href="/traces/{escape(issue)}">{escape(issue)}</a></li>' for issue in issues)
        parts.append("</ul>")
    parts.append(HTML_FOOT)
    return "".join(parts)


def render_spans_html(issue: str, spans: list[dict], params: dict) -> str:
    parts = [HTML_HEAD.format(title=f"Traces — {escape(issue)}")]
    parts.append('<div class="nav"><a href="/traces/">&larr; All issues</a></div>')
    parts.append(f"<h1>{escape(issue)}</h1>")

    filters = [f"{k}={escape(v)}" for k in ("agent_type", "trace_id", "name", "last") if (v := params.get(k))]
    if filters:
        parts.append(f"<p>Filters: {', '.join(filters)}</p>")

    parts.append(f"<p>{len(spans)} span(s)</p>")

    if spans:
        parts.append('<div class="toolbar">')
        parts.append(
            "<button onclick=\"document.getElementById('spans').classList.toggle('cols-expanded')\">"
            "Toggle additional columns</button>"
        )
        parts.append('<button onclick="toggleTheme()">Toggle dark mode</button>')
        parts.append("</div>")
        parts.append('<table id="spans"><tr>')
        parts.append("<th>Detail</th><th>Status</th>")
        parts.append('<th class="collapsible">Start</th><th class="collapsible">Duration</th>')
        parts.append('<th class="collapsible">Agent</th><th class="collapsible">Name</th>')
        parts.append("<th>Trace ID</th><th>Attributes</th>")
        parts.append("</tr>")
        for s in spans:
            sc = s.get("status_code", 0)
            detail = _extract_detail(s.get("attributes", {}), s["name"])
            tid = s["trace_id"]
            parts.append("<tr>")
            parts.append(f"<td>{detail or ''}</td>")
            parts.append(f'<td class="{_status_class(sc)}">{STATUS_LABELS.get(sc, sc)}</td>')
            parts.append(f'<td class="collapsible">{_fmt_time(s["start_time"])}</td>')
            parts.append(f'<td class="collapsible">{_fmt_duration(s["start_time"], s.get("end_time"))}</td>')
            parts.append(f'<td class="collapsible">{escape(s.get("agent_type") or "-")}</td>')
            parts.append(f'<td class="collapsible">{escape(s["name"])}</td>')
            parts.append(
                f'<td><a href="/traces/{escape(issue)}?trace_id={escape(tid)}">'
                f"{escape(tid[:12])}&hellip;</a></td>"
            )
            parts.append(f"<td>{_render_attrs(s.get('attributes', {}))}</td>")
            parts.append("</tr>")
        parts.append("</table>")

    parts.append(HTML_FOOT)
    return "".join(parts)
