#!/usr/bin/env python3
"""
Parse and summarize the Ymir `error_list` (or any Valkey list).

Intended to be fed by the `make show-error-list` target, which pipes
`oc exec deployment/valkey -- valkey-cli --no-raw LRANGE error_list 0 -1` into
it. It can also fetch the list itself, or read a saved dump.

The list is awkward to read with `jq` because:
  * valkey-cli/redis-cli framing (`1) "<C-escaped value>"` with `--no-raw` or a
    TTY, or raw newline-separated values when piped) wraps each element, and
  * error payloads are double-JSON-encoded, with the real tool error buried in a
    `details` traceback or a `{"type":"text","text":"Error executing tool ..."}`
    object.

This script strips the framing, decodes the payloads, extracts the issue key and
the tool error, and prints a per-issue summary plus a per-tool-error breakdown
(the list is cumulative across runs, so the breakdown is the useful signal).

Usage:
    make show-error-list                               # from openshift/ (preferred)
    python3 scripts/error_list.py                       # fetch via oc directly
    python3 scripts/error_list.py --queue triage_queue  # any list
    python3 scripts/error_list.py --json                # machine-readable
    oc exec deployment/valkey -- valkey-cli --no-raw LRANGE error_list 0 -1 \
        | python3 scripts/error_list.py --file -
    python3 scripts/error_list.py --file dump.txt       # parse a saved dump

Read-only: it never mutates the queue.
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter

RHEL_RE = re.compile(r"RHEL-\d+")
RC_LINE = re.compile(r'^\s*\d+\)\s+"(.*)"\s*$')  # valkey-cli interactive line
TOOL_ERR_RE = re.compile(r"Error executing tool ([\w.]+): (.+?)(?:\\n|\n|\"|$)")
AGENT_ERR_RE = re.compile(r"(AgentError: [^\"\\\n]+)")
REASON_FIELDS = ("details", "error", "message", "status", "traceback", "reason", "text")


def fetch_via_oc(queue: str, deployment: str) -> str:
    # --no-raw forces the `N) "<C-escaped>"` framing: each element on one
    # physical line with newlines escaped, which to_elements parses unambiguously
    # (raw output can't be split reliably when an element spans multiple lines).
    cmd = [
        "oc",
        "exec",
        f"deployment/{deployment}",
        "--",
        "valkey-cli",
        "--no-raw",
        "LRANGE",
        queue,
        "0",
        "-1",
    ]
    print(f"Running: {' '.join(cmd)}", file=sys.stderr)
    try:
        # Capture bytes and decode as UTF-8 explicitly: text=True would use the
        # locale encoding (US-ASCII under a C/POSIX locale, common in containers)
        # and crash on non-ASCII output. Matches how the --file paths decode.
        proc = subprocess.run(cmd, capture_output=True, timeout=60)  # noqa: S603
    except OSError as e:
        sys.exit(f"error: failed to run `oc` ({e}). Pipe a dump in via `--file -` instead.")
    except subprocess.TimeoutExpired:
        sys.exit("error: `oc exec` timed out. Check cluster connectivity / VPN.")
    stdout = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        sys.exit(
            f"error: oc exec failed (exit {proc.returncode}). "
            f"Are you logged in (`oc whoami`) and is the `{deployment}` deployment present?\n"
            f"{stderr.strip()[:300]}"
        )
    return stdout


def _unescape_rediscli(s: str) -> str:
    """Undo valkey-cli/redis-cli C-style escaping of a quoted bulk string.

    redis-cli emits each non-ASCII byte as its own ``\\xNN`` escape, so a
    multibyte UTF-8 character arrives as several escapes. Rebuild a byte buffer
    and decode it as UTF-8 at the end to recover such characters intact rather
    than turning each byte into a separate code point.
    """
    out, i, n = bytearray(), 0, len(s)
    # Mirror redis-cli's sdscatrepr escapes: \n \r \t \a \b \v \f plus " and \.
    simple = {
        "n": b"\n",
        "t": b"\t",
        "r": b"\r",
        "a": b"\a",
        "b": b"\b",
        "v": b"\v",
        "f": b"\f",
        '"': b'"',
        "\\": b"\\",
    }
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "x" and i + 3 < n:
                try:
                    out.append(int(s[i + 2 : i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
            out.extend(simple.get(nxt, nxt.encode("utf-8", "replace")))
            i += 2
        else:
            out.extend(c.encode("utf-8", "replace"))
            i += 1
    return out.decode("utf-8", errors="replace")


def to_elements(blob: str) -> list[str]:
    """Split the LRANGE dump into raw element strings.

    Prefers the `N) "..."` framing (from `--no-raw`, or an interactive TTY),
    which puts each element on one physical line and is unambiguous. Falls back
    to a best-effort scan for raw/piped output: concatenated JSON objects are
    decoded individually, and any other text is taken one line per element
    (valkey-cli `--raw` separates elements with newlines). A raw element that
    itself spans multiple lines cannot be reassembled here — prefer `--no-raw`.
    """
    lines = blob.splitlines()
    # --no-raw always prefixes the first element with `1) "..."`, so detect the
    # framing from the first non-empty line only. Probing every line with any()
    # would misfire on a raw traceback line that happens to look like `N) "..."`.
    first_line = next((ln for ln in lines if ln.strip()), None)
    if first_line and RC_LINE.match(first_line):
        return [_unescape_rediscli(m.group(1)) for line in lines if (m := RC_LINE.match(line))]
    elems, dec, i, n = [], json.JSONDecoder(), 0, len(blob)
    while i < n:
        while i < n and blob[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if blob[i] == "{":
            try:
                obj, end = dec.raw_decode(blob, i)
                elems.append(json.dumps(obj))
                i = end
                continue
            except json.JSONDecodeError:
                pass
        nl = blob.find("\n", i)
        end = n if nl == -1 else nl
        chunk = blob[i:end].strip()
        if chunk:
            elems.append(chunk)
        i = end
    return elems


def _try_json(s: str):
    v = s
    for _ in range(3):
        try:
            v = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(v, dict):
            return v
        if not isinstance(v, str):
            return None
    return None


def _issue_str(val) -> str:
    """Coerce an issue value to a string key (Jira sometimes nests it as {"key": ...})."""
    if isinstance(val, dict) and "key" in val:
        return str(val["key"])
    return str(val)


def issue_of(obj, raw: str) -> str:
    if isinstance(obj, dict):
        meta = obj.get("metadata")
        if isinstance(meta, dict) and meta.get("issue"):
            return _issue_str(meta["issue"])
        for key in ("jira_issue", "issue", "issue_key"):
            if obj.get(key):
                return _issue_str(obj[key])
    m = RHEL_RE.search(raw)
    return m.group(0) if m else "?"


def reason_of(obj, raw: str) -> str:
    m = TOOL_ERR_RE.search(raw)
    if m:
        return f"{m.group(1)}: {m.group(2).strip()[:90]}"
    a = AGENT_ERR_RE.search(raw)
    if a:
        return a.group(1)[:100]
    if isinstance(obj, dict):
        for f in REASON_FIELDS:
            v = obj.get(f)
            if v:
                lines = str(v).splitlines()
                first = lines[0] if lines else ""
                if first.strip():
                    return first[:100]
        if "attempts" in obj or "metadata" in obj:
            return f"(Task, attempts={obj.get('attempts', '?')}, no error field)"
    line = next((ln for ln in raw.splitlines() if ln.strip()), "")
    return line[:100] or "(empty)"


def signature(reason: str) -> str:
    m = re.match(r"([\w.]+):", reason)
    return m.group(1) if m else reason[:40]


def analyze(blob: str) -> list[dict]:
    out = []
    for elem in to_elements(blob):
        obj = _try_json(elem)
        out.append({"issue": issue_of(obj, elem), "reason": reason_of(obj, elem), "parsed": obj is not None})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue", default="error_list", help="Valkey list name (default: error_list)")
    ap.add_argument(
        "--deployment", default="valkey", help="OpenShift deployment running valkey (default: valkey)"
    )
    ap.add_argument("--file", help="Parse a saved `LRANGE ... 0 -1` dump; use '-' to read stdin")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = ap.parse_args()

    if args.file == "-":
        # Decode as UTF-8 regardless of locale: dumps carry non-ASCII tracebacks
        # and the default stdin encoding is the C locale (ASCII) in cron/CI pods.
        blob = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    elif args.file:
        try:
            with open(args.file, encoding="utf-8", errors="replace") as fh:
                blob = fh.read()
        except OSError as e:
            sys.exit(f"error: failed to read file '{args.file}': {e}")
    else:
        blob = fetch_via_oc(args.queue, args.deployment)

    entries = analyze(blob)
    total = len(entries)
    parsed = sum(1 for e in entries if e["parsed"])
    by_sig = Counter(signature(e["reason"]) for e in entries)

    if args.json:
        print(
            json.dumps(
                {
                    "queue": args.queue,
                    "total": total,
                    "parsed": parsed,
                    "by_issue": dict(Counter(e["issue"] for e in entries).most_common()),
                    "by_error": dict(by_sig.most_common()),
                    "entries": entries,
                },
                indent=2,
            )
        )
        return

    print(f"\n{args.queue}: {total} entr{'y' if total == 1 else 'ies'} ({parsed} parsed)\n")
    print("By issue:")
    grouped: dict[str, Counter] = {}
    for e in entries:
        grouped.setdefault(e["issue"], Counter())[e["reason"]] += 1
    width = max((len(k) for k in grouped), default=4)
    for issue, reasons in sorted(grouped.items(), key=lambda kv: -sum(kv[1].values())):
        n = sum(reasons.values())
        tag = f" (x{n})" if n > 1 else ""
        print(f"  {issue:<{width}}{tag}  {reasons.most_common(1)[0][0][:90]}")
    print("\nBy error type:")
    for sig, n in by_sig.most_common():
        print(f"  {n:>3}x  {sig}")


if __name__ == "__main__":
    main()
