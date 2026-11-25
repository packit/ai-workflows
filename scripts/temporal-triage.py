#!/usr/bin/env python3
"""Run the opt-in Temporal triage against a configured MCP gateway."""

import argparse
import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from ymir.agents.pydantic_agent import configured_model, pydantic_model_settings
from ymir.agents.temporal_triage_analysis import (
    TriageAnalysisRequest,
    TriageAnalysisWorkflow,
    create_analysis_worker,
    triage_issue_workflow_id,
)
from ymir.agents.temporal_triage_provisioning import OpenShiftTriageProvisioner, create_triage_control_worker
from ymir.agents.temporal_triage_publication import (
    TriagePublicationRequest,
    _writes_disabled,
    run_triage_request,
)
from ymir.agents.utils import get_agent_execution_config, resolve_chat_model_override


async def main(args: argparse.Namespace) -> None:
    resolve_chat_model_override("triage")
    client = await Client.connect(
        args.address, namespace=args.temporal_namespace, plugins=[PydanticAIPlugin()]
    )
    request = TriageAnalysisRequest(
        jira_issue=args.issue.strip().upper(),
        model_settings=dict(pydantic_model_settings(os.environ["CHAT_MODEL"])),
        request_limit=get_agent_execution_config()["max_iterations"],
        force_cve_triage=args.force_cve_triage,
        user_triggered=args.user_triggered,
        triage_only_requested=args.triage_only,
        follow_up_requested=args.follow_up_requested,
    )
    publication = args.request_id is not None
    workflow_id = triage_issue_workflow_id(request.jira_issue)
    if publication:
        command = TriagePublicationRequest(
            request,
            args.request_id,
            not args.publish or _writes_disabled(),
            args.enqueue_reproducer,
            provision_pod=args.openshift_namespace is not None,
        )
        queue = command.receipt_workflow_id()
    else:
        queue = f"triage-analysis-{uuid4().hex[:12]}"

    if args.openshift_namespace:
        provisioner = OpenShiftTriageProvisioner(
            args.openshift_namespace,
            args.pod_template,
            args.pod_temporal_address or args.address,
            client.namespace,
        )
        async with create_triage_control_worker(client, queue, provisioner):
            print(f"Workflow ID: {workflow_id}", flush=True)
            result = await run_triage_request(client, command, queue)
        _report(result, publication=True)
        return

    async with (
        configured_model() as model,
        await create_analysis_worker(client, queue, model, publication=publication),
    ):
        print(f"Workflow ID: {workflow_id}", flush=True)
        if publication:
            result = await run_triage_request(client, command, queue)
        else:
            try:
                result = await client.execute_workflow(
                    TriageAnalysisWorkflow.run,
                    request,
                    id=workflow_id,
                    task_queue=queue,
                    id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                    execution_timeout=timedelta(minutes=45),
                )
            except WorkflowAlreadyStartedError as error:
                raise SystemExit(f"Triage is already running for {request.jira_issue}") from error

    _report(result, publication)


def _report(result, publication: bool) -> None:
    report = {}
    if publication:
        report.update(
            dry_run=result.dry_run,
            skipped_reason=result.skipped_reason,
            destinations=result.destinations,
        )
        result = result.analysis
    if result is not None:
        report.update(
            state=result.state.model_dump(mode="json"), follow_up_requested=result.follow_up_requested
        )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue")
    parser.add_argument("--address", default="localhost:7233")
    parser.add_argument("--temporal-namespace", default="default")
    parser.add_argument(
        "--openshift-namespace", help="Provision a worker and gateway pod using the current oc context"
    )
    parser.add_argument("--pod-temporal-address", help="Temporal address reachable from the execution pod")
    parser.add_argument(
        "--pod-template",
        type=Path,
        default=Path("openshift/templates/temporal-triage-pod.yml"),
    )
    parser.add_argument("--force-cve-triage", action="store_true")
    parser.add_argument("--user-triggered", action="store_true")
    parser.add_argument("--triage-only", action="store_true")
    parser.add_argument("--follow-up-requested", action="store_true")
    parser.add_argument("--request-id", help="Stable request identifier for the publication workflow")
    parser.add_argument("--publish", action="store_true", help="Enable Jira and Redis writes for the request")
    parser.add_argument(
        "--enqueue-reproducer", action="store_true", help="Include eligible reproducer handoffs"
    )
    args = parser.parse_args()
    if args.request_id is None and (args.publish or args.enqueue_reproducer or args.openshift_namespace):
        parser.error("--publish, --enqueue-reproducer, and --openshift-namespace require --request-id")
    asyncio.run(main(args))
