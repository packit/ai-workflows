#!/usr/bin/env python3
"""Opt-in gate for the agentic merge request review (PACKIT-5545 / PACKIT-5609).

Consumed as the dynamic precondition of the agentic review jobs in
osci-pipelines (rog/): a small generator job runs this module in the
merge_check stage and only generates the review child pipeline when it
exits 0. Exit codes are the whole contract:

* 0 — the review is enabled for this merge request.
* 1 — it is not (one-line reason on stderr). Callers must treat this
  as "skip cleanly" so pipelines stay green for the packages that have
  not opted in.

Enablement order:

1. MR label ``feature::agentic-review::off`` — disabled (wins over everything).
2. MR label ``feature::agentic-review::on`` — enabled (forced).
3. Otherwise: per-package opt-in through the Ymir rules namespace
   (``redhat/centos-stream/rules/<package>`` on gitlab.com), which
   opts in via a top-level ``review:`` section containing
   ``enabled: true`` in its ``ymir.yaml``. The file is parsed with
   PyYAML only — we control this image — and anything malformed fails
   closed (never enabled).

The rules repo clone prefers an exported GitLab token (``GITLAB_TOKEN``,
the OSCI service account in the osci-pipelines wiring): authenticated
clones are not subject to anonymous rate limits and can reach
non-public rules repos; the anonymous fallback still works for public
ones. The URL is held in a variable and clone output discarded, so a
token can never reach a job log.
"""

import os
import subprocess
import sys
import tempfile

import yaml

LABEL_ON = "feature::agentic-review::on"
LABEL_OFF = "feature::agentic-review::off"
RULES_URL_TEMPLATE = "https://gitlab.com/redhat/centos-stream/rules/{project}.git"


def has_label(name: str) -> bool:
    """CI_MERGE_REQUEST_LABELS is comma-separated; pad for exact membership."""
    labels = f",{os.environ.get('CI_MERGE_REQUEST_LABELS', '')},"
    return f",{name}," in labels


def clone_url(project: str) -> str:
    url = RULES_URL_TEMPLATE.format(project=project)
    token = os.environ.get("GITLAB_TOKEN", "")
    if token:
        url = url.replace("https://", f"https://oauth2:{token}@")
    return url


def clone_rules_repo(project: str, dest: str) -> bool:
    """Shallow-clone the package's rules repo; False when it doesn't exist."""
    result = subprocess.run(
        ["git", "clone", "--depth", "1", clone_url(project), dest],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def review_enabled_in_config(path: str) -> bool:
    """Strict check: a top-level ``review:`` mapping with ``enabled: true``.

    Any parse error, a missing file, a non-mapping ``review`` section,
    or ``enabled`` not being the boolean True all mean "not opted in".
    """
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return False
    review = data.get("review") if isinstance(data, dict) else None
    return isinstance(review, dict) and review.get("enabled") is True


def main() -> int:
    if has_label(LABEL_OFF):
        print(f"agentic review disabled by label {LABEL_OFF}", file=sys.stderr)
        return 1

    if has_label(LABEL_ON):
        return 0

    project = os.environ.get("CI_PROJECT_NAME", "")
    if not project:
        print("agentic review not enabled: CI_PROJECT_NAME is not set", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="ymir-mr-review-rules-") as rules_dir:
        if not clone_rules_repo(project, rules_dir):
            print(
                f"agentic review not enabled: no rules repo rules/{project} (package not opted in)",
                file=sys.stderr,
            )
            return 1
        config = os.path.join(rules_dir, "ymir.yaml")
        if not os.path.isfile(config):
            print(f"agentic review not enabled: rules/{project} has no ymir.yaml", file=sys.stderr)
            return 1
        if not review_enabled_in_config(config):
            print(
                f"agentic review not enabled: no review: enabled: true in rules/{project}/ymir.yaml",
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
