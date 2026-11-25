"""Submit triage requests and recover package coordination."""

import argparse
import asyncio
import json
import os
import sys
from uuid import uuid4

from ymir.agents.temporal_package_coordination import recover_package_coordination
from ymir.common.temporal_triage import (
    CONTROL_QUEUE,
    TriageIntent,
    connect_triage_ingress,
    submit_triage_intent,
    triage_issue_workflow_id,
)


async def main(args: argparse.Namespace) -> None:
    client = await connect_triage_ingress()
    if args.command == "recover-coordination":
        queue = os.getenv("TEMPORAL_TRIAGE_CONTROL_QUEUE", CONTROL_QUEUE)
        print(json.dumps(await recover_package_coordination(client, queue), indent=2))
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
    actions.add_parser("recover-coordination", help="Resubmit waiting package-stream issues from Jira")
    asyncio.run(main(parser.parse_args()))
