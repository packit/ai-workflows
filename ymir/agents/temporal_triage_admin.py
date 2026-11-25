"""Submit triage requests and transfer retired Redis queues at cutover."""

import argparse
import asyncio
import json
import os
import sys
from hashlib import sha256
from uuid import uuid4

from ymir.common.base_utils import fix_await, redis_client
from ymir.common.constants import RedisQueues
from ymir.common.models import Task, TriageInputSchema
from ymir.common.temporal_triage import (
    PARKED_QUEUE_SUFFIX,
    TriageIntent,
    connect_triage_ingress,
    submit_triage_intent,
    triage_issue_workflow_id,
)

LEGACY_QUEUES = (RedisQueues.TRIAGE_QUEUE_TODO.value, RedisQueues.TRIAGE_QUEUE.value)


async def transfer_queue(redis, client, queue: str) -> int:
    """Transfer a quiescent queue, removing each original only after acceptance."""
    count = 0
    while (payload := await fix_await(redis.lindex(queue, -1))) is not None:
        raw = json.loads(payload)
        task = Task.model_validate(raw)
        metadata = TriageInputSchema.model_validate(task.metadata)
        request_id = (
            raw.get("execution_id")
            or sha256(payload.encode() if isinstance(payload, str) else payload).hexdigest()
        )
        await submit_triage_intent(
            client,
            TriageIntent(
                metadata.issue,
                f"redis:{request_id}",
                "redis",
                dry_run=False,
                user_triggered=task.user_triggered,
                triage_only=task.triage_only,
                force_cve_triage=metadata.force_cve_triage,
            ),
        )
        if not await fix_await(redis.lrem(queue, -1, payload)):
            raise RuntimeError(f"{queue} changed during transfer")
        count += 1
    return count


async def main(args: argparse.Namespace) -> None:
    client = await connect_triage_ingress()
    if args.command == "drain":
        async with redis_client(os.environ["REDIS_URL"]) as redis:
            for queue in (*LEGACY_QUEUES, *(name + PARKED_QUEUE_SUFFIX for name in LEGACY_QUEUES)):
                count = await transfer_queue(redis, client, queue)
                print(f"{queue}: {count} requests transferred", flush=True)
        return

    intent = TriageIntent(
        args.issue,
        args.request_id or f"manual:{uuid4()}",
        "cli",
        dry_run=args.dry_run,
        user_triggered=args.user_triggered,
        force_cve_triage=args.force_cve_triage,
        triage_only=args.triage_only,
    )
    result = await submit_triage_intent(client, intent, wait=args.wait)
    if isinstance(result, str):
        print(f"{result} {intent.request_id} in {triage_issue_workflow_id(intent.issue)}")
    else:
        if result["output"] is not None:
            print(json.dumps(result["output"], indent=2))
        else:
            print(result["detail"] or result["status"], file=sys.stderr)
        if result["status"] in {"failed", "unstarted"}:
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="command", required=True)
    submit = actions.add_parser("submit")
    submit.add_argument("issue")
    submit.add_argument("--request-id")
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--dry-run", action="store_true")
    submit.add_argument("--force-cve-triage", action="store_true")
    submit.add_argument("--user-triggered", action="store_true")
    submit.add_argument("--triage-only", action="store_true")
    actions.add_parser("drain")
    asyncio.run(main(parser.parse_args()))
