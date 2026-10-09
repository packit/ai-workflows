#!/usr/bin/env python3
"""Check that OpenShift agent containers preserve the image entrypoint.

Run scripts/test_agent_init.py against built images to check init behavior.
"""

import glob
import sys

import yaml

DEPLOYMENT_GLOB = "openshift/deployment-*-agent*.yml"

errors: list[str] = []

checked_deployments = 0
for dep in sorted(glob.glob(DEPLOYMENT_GLOB)):
    with open(dep) as f:
        content = f.read()
    for deployment in yaml.safe_load_all(content):
        if not deployment or deployment.get("kind") != "Deployment":
            continue
        pod_spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
        has_agent = False
        for container in pod_spec.get("containers", []):
            image_name = container.get("image", "").rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]
            if image_name != "beeai-agent":
                continue
            has_agent = True
            if container.get("command"):
                errors.append(
                    f"{dep}: agent container {container.get('name', '<unnamed>')} uses 'command:' "
                    "which overrides the image ENTRYPOINT"
                )
        if has_agent:
            checked_deployments += 1

if not checked_deployments:
    errors.append(f"No beeai-agent deployments found matching {DEPLOYMENT_GLOB}")

if errors:
    for e in errors:
        print(f"FAIL: {e}", file=sys.stderr)
    raise SystemExit(1)

print(f"OK: {checked_deployments} agent deployment(s) checked")
