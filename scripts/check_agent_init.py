#!/usr/bin/env python3
"""Static checks for the catatonit init entrypoint in agent images.

Verifies that:
- Both Containerfiles install catatonit and set the ENTRYPOINT.
- No OpenShift agent deployment overrides the ENTRYPOINT with 'command:'.

For runtime checks (orphan reaping, signal forwarding, exit codes), build the
images and run scripts/test_agent_init.py locally.
"""

import glob
import json
import re
import sys

import yaml

CONTAINERFILES = ["Containerfile.c9s", "Containerfile.c10s"]
DEPLOYMENT_GLOB = "openshift/deployment-*-agent*.yml"

errors: list[str] = []

for cf in CONTAINERFILES:
    with open(cf) as f:
        content = f.read()
    # Dockerfile comments are ignored, including between continued lines.
    content = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith("#"))
    content = re.sub(r"\\\s*\n", " ", content)
    run_commands = re.findall(r"^\s*RUN\s+(.+)$", content, re.MULTILINE | re.IGNORECASE)
    if not any(
        re.search(r"\bdnf\s+[^;&|]*\binstall\s+[^;&|]*(?<!\S)catatonit(?=\s|$)", command)
        for command in run_commands
    ):
        errors.append(f"{cf}: catatonit package not installed")
    entrypoints = re.findall(r"^\s*ENTRYPOINT\s+(.+)$", content, re.MULTILINE | re.IGNORECASE)
    try:
        entrypoint = json.loads(entrypoints[-1]) if entrypoints else None
    except json.JSONDecodeError:
        entrypoint = None
    if entrypoint != ["/usr/bin/catatonit", "--"]:
        errors.append(f'{cf}: effective ENTRYPOINT must be ["/usr/bin/catatonit", "--"]')

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

print(f"OK: {len(CONTAINERFILES)} Containerfiles and {checked_deployments} deployment(s) checked")
