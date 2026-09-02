#!/usr/bin/env python3
"""
Requeue a specific `error_list` entry back onto its original trigger queue.

Only works for entries pushed by the `ErrorListEntry`-aware agents (i.e. those
carrying `error_id`/`queue`/`task`). Legacy entries pushed before that change,
or entries recorded for a payload that never parsed into a `Task` in the first
place, have no `queue`/`task` to requeue and are reported as non-requeueable.

The task's `attempts` counter is reset to 0 and `user_triggered` is forced to
True before requeuing, on the assumption that whoever runs this has already
fixed the underlying issue and wants a fresh retry budget. Forcing
`user_triggered` also matters functionally: triage and reproducer skip
processing outright when a terminal `ymir_*_errored`/similar label is still
on the issue and the task isn't user-triggered — exactly the state a
just-requeued task is in until it gets a chance to run and clear that label
itself. It also routes the task onto the priority (`_todo`) twin of its
queue, same as any other maintainer-triggered run.

The entry is located and its replacement task computed here, client-side (a
plain LRANGE + local JSON parsing) rather than inside Redis: scanning and
JSON-decoding the whole (cumulative, potentially large) error_list from
within a Lua script would block the single-threaded server for everyone else
using it. The actual remove-from-error_list and push-to-target-queue then
happen as a single atomic Lua script (`EVAL`) so a failure or a racing
concurrent invocation can't leave the task duplicated (both still in
error_list and requeued) or double-enqueued. That script treats the entry
and task as opaque strings it never decodes — Redis's Lua `cjson` can't tell
an empty JSON array from an empty object, so decoding and re-encoding the
task would risk silently corrupting typed list fields (e.g.
`RebaseData.consolidated_issues`). The (potentially large) entry/task strings
are staged into temporary Redis keys via `valkey-cli -x` (stdin) rather than
passed as `oc exec`/subprocess command-line arguments, which have far lower
size limits than a Redis value.

Usage:
    make requeue-error ERROR_ID=42                       # from openshift/ (preferred)
    python3 scripts/requeue_error.py 42                  # same, run directly
    python3 scripts/requeue_error.py 42 --dry-run         # show the plan, don't mutate anything
"""

import argparse
import contextlib
import json
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from error_list import _try_json, fetch_via_oc, to_elements


def run_valkey_cli(deployment: str, args: list[str], stdin: bytes | None = None) -> str:
    # No command-line logging here: `args`/`stdin` can carry a full task/error
    # record (traceback, metadata), and the caller already prints a safe,
    # human-level summary of what's about to happen before invoking this.
    exec_flags = ["-i"] if stdin is not None else []
    cmd = ["oc", "exec", *exec_flags, f"deployment/{deployment}", "--", "valkey-cli", *args]
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, timeout=60)  # noqa: S603
    except OSError as e:
        sys.exit(f"error: failed to run `oc` ({e}).")
    except subprocess.TimeoutExpired:
        sys.exit("error: `oc exec` timed out. Check cluster connectivity / VPN.")
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        sys.exit(f"error: valkey-cli command failed (exit {proc.returncode}): {stderr.strip()[:300]}")
    return proc.stdout.decode("utf-8", errors="replace")


def find_entry(blob: str, error_id: int) -> tuple[str, dict] | None:
    for raw in to_elements(blob):
        obj = _try_json(raw)
        if isinstance(obj, dict) and obj.get("error_id") == error_id:
            return raw, obj
    return None


# Removes the pre-staged raw entry from the source list and, only if that
# removal actually found and deleted something, pushes the pre-staged task
# onto the target list. Both values are read via GET (opaque strings — never
# JSON-decoded, see module docstring) from temporary keys that are always
# cleaned up before returning, win or lose.
#
# LREM has to run before LPUSH so only the invocation that actually removes
# the entry proceeds to push it (that's what makes concurrent requeues of the
# same error_id race-safe). But Redis doesn't roll back a script's earlier
# writes just because a later command errors — so if LPUSH then failed (e.g.
# the target key exists with the wrong type), the entry would be gone from
# `source` forever without ever reaching `target`. Guard against that with
# redis.pcall (catches the error instead of aborting the script) and, on
# failure, push the entry back onto `source` rather than lose it.
_ATOMIC_REQUEUE_LUA = """
local raw = redis.call('GET', KEYS[3])
local task_json = redis.call('GET', KEYS[4])
local result = 0
if raw and task_json then
    local removed = redis.call('LREM', KEYS[1], 1, raw)
    if removed == 1 then
        local reply = redis.pcall('LPUSH', KEYS[2], task_json)
        if type(reply) == 'table' and reply.err then
            redis.call('LPUSH', KEYS[1], raw)
            result = -1
        else
            result = 1
        end
    end
end
redis.call('DEL', KEYS[3], KEYS[4])
return result
"""


# Safety net for staging keys: the Lua script always DELs them on the happy
# path, but if a SET or the EVAL itself fails, best-effort cleanup (below)
# might not run either (e.g. the process gets killed) — a bounded TTL is the
# backstop that guarantees they can't accumulate in Valkey forever.
_STAGING_KEY_TTL_SECONDS = 300


def _stage_value(deployment: str, key: str, value: str) -> None:
    """Write `value` to `key` via stdin (avoids argv size limits), with a TTL."""
    run_valkey_cli(deployment, ["-x", "SET", key], stdin=value.encode())
    run_valkey_cli(deployment, ["EXPIRE", key, str(_STAGING_KEY_TTL_SECONDS)])


def _cleanup_staging_keys(deployment: str, *keys: str) -> None:
    """Best-effort delete — the TTL above is the real safety net, so a failure
    here (e.g. the same outage that broke the operation we're cleaning up
    after) is swallowed rather than masking the original error."""
    with contextlib.suppress(SystemExit):
        run_valkey_cli(deployment, ["DEL", *keys])


def atomic_requeue(
    deployment: str, source_queue: str, target_queue: str, raw_entry: str, task_json: str, error_id: int
) -> int:
    """Atomically move `raw_entry` off `source_queue` and `task_json` onto `target_queue`.

    `raw_entry`/`task_json` are staged into temporary keys via stdin
    (`valkey-cli -x`) rather than passed as command-line arguments, since
    either can be arbitrarily large; the Lua script only ever receives small
    key names. A random suffix keeps concurrent requeues of the same
    error_id from sharing staging keys, and a bounded TTL plus best-effort
    cleanup on failure keep a broken SET/EVAL from leaking them permanently.

    Returns:
       1 — moved successfully
       0 — `raw_entry` was no longer in `source_queue` (e.g. another
           invocation already requeued or removed it first); nothing pushed
      -1 — the push to `target_queue` itself failed (e.g. wrong type on that
           key); the entry was put back onto `source_queue`, nothing lost
    """
    token = secrets.token_hex(8)
    entry_key = f"tmp:requeue_error:{error_id}:{token}:entry"
    task_key = f"tmp:requeue_error:{error_id}:{token}:task"
    try:
        _stage_value(deployment, entry_key, raw_entry)
        _stage_value(deployment, task_key, task_json)
        out = run_valkey_cli(
            deployment,
            ["EVAL", _ATOMIC_REQUEUE_LUA, "4", source_queue, target_queue, entry_key, task_key],
        )
    except SystemExit:
        _cleanup_staging_keys(deployment, entry_key, task_key)
        raise
    return int(out.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "error_id", type=int, help="error_id of the entry to requeue (see `make show-error-list`)"
    )
    ap.add_argument("--queue", default="error_list", help="Source Valkey list name (default: error_list)")
    ap.add_argument(
        "--deployment", default="valkey", help="OpenShift deployment running valkey (default: valkey)"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Print the plan without pushing or removing anything"
    )
    args = ap.parse_args()

    blob = fetch_via_oc(args.queue, args.deployment)
    found = find_entry(blob, args.error_id)
    if found is None:
        sys.exit(f"error: no entry with error_id={args.error_id} found in {args.queue}")
    raw, entry = found

    target_queue = entry.get("queue")
    task = entry.get("task")
    jira_issue = (entry.get("error") or {}).get("jira_issue", "unknown")
    if not target_queue or not task:
        sys.exit(
            f"error: entry {args.error_id} ({jira_issue}) has no requeueable task "
            "(legacy entry, or a payload that never parsed into a Task) — nothing to requeue."
        )

    old_attempts = task.get("attempts", 0)
    task["attempts"] = 0
    task["user_triggered"] = True
    if not target_queue.endswith("_todo"):
        target_queue = f"{target_queue}_todo"
    task_json = json.dumps(task)

    print(
        f"Requeuing error_id={args.error_id} ({jira_issue}) onto '{target_queue}' "
        f"(attempts {old_attempts} -> 0, user_triggered -> true)"
    )
    if args.dry_run:
        print(f"[dry-run] Would atomically remove from {args.queue} and LPUSH {target_queue}: {task_json}")
        return

    result = atomic_requeue(args.deployment, args.queue, target_queue, raw, task_json, args.error_id)
    if result == 0:
        sys.exit(
            f"error: entry {args.error_id} was no longer in {args.queue} "
            "(already requeued or removed by another invocation) — nothing pushed."
        )
    if result == -1:
        sys.exit(
            f"error: entry {args.error_id} could not be pushed onto {target_queue} "
            f"(e.g. wrong type at that key) — it was left in {args.queue}, nothing lost."
        )
    print(f"✓ Requeued onto {target_queue} and removed from {args.queue}.")


if __name__ == "__main__":
    main()
