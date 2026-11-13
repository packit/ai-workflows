"""Eval-only: Amalgamator plugged into the Ymir backport workflow in place of the
backport agent's cherry-pick / conflict-resolution step.

Everything around that step is Ymir's own code, so both arms start from the same
state and end in the same packaging:

* before it (workflow, unchanged): fork + clone dist-git, download_sources,
  run_package_prep, fetch the upstream patches;
* before it (agent steps 2, 4a-4e, run here deterministically with the agent's
  own tools): git_prepare_package_sources, extract_upstream_repository,
  get_package_info, clone_upstream_repository, find_base_commit and
  apply_downstream_patches, so the upstream clone carries the package's
  existing downstream patches. Where apply_downstream_patches fails, the agent
  falls back to the unpacked %prep sources (approach B); this arm commits that
  same %prep tree onto the release tag instead, because Amalgamator needs
  upstream history to port;
* the step itself: Amalgamator cherry-picks the fix onto that state;
* after it (agent steps 0, 4g, 5-8): Ymir's own backport agent, told that the
  cherry-pick is done, generates the patch file, edits the spec, runs prep,
  builds the SRPM and self-reviews; it may not change the backported code;
* after it (workflow, unchanged): update_release, stage_changes, log agent,
  commit and build.

The workflow calls ``backport_agent.run(prompt, expected_output=...)`` and reads
``response.last_message.text``, so this class only has to look like an agent.
There is no build-fix loop for this arm: a second call (the fix-build-error
prompt) returns a failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ymir.agents import tasks
from ymir.agents.amalgamator import adapter
from ymir.agents.amalgamator import inputs as arm_inputs
from ymir.agents.utils import run_tool
from ymir.common.models import BackportOutputSchema
from ymir.tools.unprivileged.specfile import GetPackageInfoTool, PackageInfo
from ymir.tools.unprivileged.upstream_tools import (
    ApplyDownstreamPatchesTool,
    CloneUpstreamRepositoryTool,
    ExtractUpstreamRepositoryTool,
    FindBaseCommitTool,
    UpstreamRepository,
)
from ymir.tools.unprivileged.wicked_git import GitPreparePackageSources

logger = logging.getLogger(__name__)

PACKAGING_PROMPT = """

IMPORTANT — this run differs from the instructions above:
Steps 1, 2, 3 and 4a-4f have ALREADY been completed by an external backporting tool.
- Step 2: package sources were prepared in {unpacked_sources}.
- Steps 4a-4e: the upstream repository was cloned to <UPSTREAM_REPO> = {upstream_repo},
  the base version was checked out and all existing downstream patches were applied.
  The base commit recorded for patch generation is {base_commit}.
- Step 4f: the upstream fix was cherry-picked and every conflict resolved. The resulting
  commit(s) are on top of {base_commit} in <UPSTREAM_REPO>.

Do NOT redo the backport and do NOT change the backported code. Continue with:
- step 0 (fetch the rules; you need them for patch naming and spec conventions),
- step 4g: generate the patch with `git_patch_create` on <UPSTREAM_REPO>. If it reports a
  missing base commit, use `run_shell_command` with
  `git -C {upstream_repo} format-patch --no-signature --stdout {base_commit}..HEAD > <patch>`,
- steps 5, 6 and 7, then the self-review.
You MAY drop changes to the kinds of files the general instructions list
(.github/ workflows, .gitignore, news/changelog files, internal documentation) from the
generated patch, as the general instructions require. You MUST NOT edit any other
content of the patch. If `run_package_prep` reports that the patch does not apply, end
with `success=False` and the prep error — do not adapt the code to make it apply.
"""


@dataclass
class _Message:
    text: str


@dataclass
class _Response:
    last_message: _Message


def _response(result: BackportOutputSchema) -> _Response:
    return _Response(last_message=_Message(text=result.model_dump_json()))


async def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


class AmalgamatorBackportAgent:
    """Stands in for the backport agent; see the module docstring."""

    def __init__(
        self,
        *,
        packaging_agent,
        gateway_tools: list,
        local_tool_options: dict[str, Any],
        case_input: dict,
        artifacts_dir: Path,
    ):
        self.packaging_agent = packaging_agent
        # The e2e test appends its MetricsMiddleware here; it then measures the
        # packaging agent (Ymir's LLM share of this arm).
        self.middlewares = packaging_agent.middlewares
        self.gateway_tools = gateway_tools
        self.opts = local_tool_options
        self.case = case_input
        self.out = artifacts_dir / case_input["jira_issue"]
        self.calls = 0
        self.record: dict[str, Any] = {}

    def _save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "plugged_amalgamator.json").write_text(
            json.dumps(self.record, indent=1, default=str) + "\n"
        )

    def _fail(self, error: str) -> _Response:
        self.record["error"] = error
        self._save()
        logger.error("Amalgamator arm for %s: %s", self.case["jira_issue"], error)
        return _response(BackportOutputSchema(success=False, status="", srpm_path=None, error=error))

    async def run(self, prompt: str, expected_output=None, **kwargs) -> _Response:
        self.calls += 1
        if self.calls > 1:
            return self._fail("Amalgamator arm has no build-fix loop (fix-build-error prompt received)")
        try:
            return await self._run(prompt, expected_output, **kwargs)
        except Exception as error:  # report, don't crash the workflow
            logger.exception("Amalgamator arm crashed")
            return self._fail(f"{type(error).__name__}: {error}")

    async def _run(self, prompt: str, expected_output, **kwargs) -> _Response:
        issue = self.case["jira_issue"]
        package = self.case["package"]
        upstream_url = self.case["upstream_patches"][0]
        local_clone = Path(self.opts["working_directory"])
        builddir = self.opts.get("builddir")
        unpacked = tasks.get_unpacked_sources(
            local_clone, package, builddir=Path(builddir) if builddir else None
        )
        github_tools = [
            t for t in self.gateway_tools if t.name in {"get_github_pull_request", "get_github_compare"}
        ]
        self.record.update(
            issue=issue, package=package, upstream_url=upstream_url, local_clone=str(local_clone)
        )

        # Agent step 2, with the agent's own tool and the shared options dict.
        await run_tool(GitPreparePackageSources(options=self.opts), unpacked_sources_path=str(unpacked))
        spec = local_clone / tasks.find_spec(local_clone, package)
        info = await run_tool(
            GetPackageInfoTool(options=self.opts), expected_output=PackageInfo, spec=str(spec)
        )
        # get_package_info leaves shell macros unexpanded (expat's Version is
        # `%(echo %{unversion} | sed ...)`); the agent resolves those itself, so
        # resolve them the same way the PR's arm does.
        version = info.version
        if "%" in version:
            version = arm_inputs.read_spec_version(spec) or version
        self.record.update(version=version, downstream_patches=len(info.patch_files))

        # The cherry-pick workflow's set-up, 4a-4d.
        upstream = local_clone.parent / f"{local_clone.name}-upstream"
        repo = await run_tool(
            ExtractUpstreamRepositoryTool(github_tools=github_tools, options=self.opts),
            expected_output=UpstreamRepository,
            upstream_fix_url=upstream_url,
        )
        await run_tool(
            CloneUpstreamRepositoryTool(options=self.opts),
            repo_url=repo.repo_url,
            clone_directory=str(local_clone),
        )
        await run_tool(FindBaseCommitTool(options=self.opts), repo_path=str(upstream), version=version)
        _, tag_commit = await _git(upstream, "rev-parse", "HEAD")
        tag_commit = tag_commit.strip()

        # 4e: reproduce the package's patched state on top of the release tag.
        try:
            await run_tool(
                ApplyDownstreamPatchesTool(options=self.opts),
                repo_path=str(upstream),
                patch_files=info.patch_files,
                patches_directory=str(local_clone),
                patch_strip_levels=info.patch_strip_levels,
            )
            base_commit = self.opts.get("base_head_commit")
            _, head = await _git(upstream, "rev-parse", "HEAD")
            if not base_commit or base_commit != head.strip():
                raise RuntimeError(
                    f"downstream patch application left base={base_commit} head={head.strip()}"
                )
            approach = "A"
        except Exception as error:
            # Where apply_downstream_patches cannot reproduce the state (nginx's
            # mail-formatted patches, expat 2.2.5's %setup subdirectory), the
            # agent's instructions fall back to approach B: work on the unpacked
            # sources, which carry every downstream patch exactly as %prep
            # applied them. Amalgamator cannot port without upstream history, so
            # give it that same tree as a commit on the release tag: identical
            # code to approach B, with the history a port needs.
            logger.info("Amalgamator arm for %s: 4e failed, using the %%prep tree: %s", issue, error)
            self.record["approach_a_error"] = f"{type(error).__name__}: {error}"[:2000]
            await _git(upstream, "am", "--abort")
            for args in (("reset", "--hard", tag_commit), ("clean", "-fdx")):
                code, out = await _git(upstream, *args)
                if code != 0:
                    return self._fail(f"could not reset {upstream}: {out[-500:]}")
            code, out = await _git(upstream, f"--work-tree={unpacked}", "add", "-A")
            if code == 0:
                code, out = await _git(
                    upstream,
                    f"--work-tree={unpacked}",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "Downstream state after %prep (all spec patches applied)",
                )
            if code != 0:
                return self._fail(f"could not commit the %prep tree onto {tag_commit}: {out[-1000:]}")
            await _git(upstream, "reset", "--hard", "HEAD")
            _, base_commit = await _git(upstream, "rev-parse", "HEAD")
            base_commit = base_commit.strip()
            self.opts["base_head_commit"] = base_commit
            approach = "A-prep-tree"

        fix_commit = await asyncio.to_thread(arm_inputs.resolve_fix_commit, upstream_url)
        if not fix_commit:
            return self._fail(f"could not resolve a fix commit from {upstream_url}")
        if not await asyncio.to_thread(arm_inputs.commit_exists, upstream, fix_commit):
            await asyncio.to_thread(arm_inputs.fetch_commit, upstream, fix_commit)
        target_repo = upstream
        fix_patch = None
        source_branch = arm_inputs.default_branch_ref(upstream)
        self.record.update(
            approach=approach,
            tag_commit=tag_commit,
            target_repo=str(target_repo),
            base_commit=base_commit,
            fix_commit=fix_commit,
        )

        commands: dict = {}
        if commands_path := os.getenv("AMALGAMATOR_BUILD_COMMANDS"):
            commands = json.loads(Path(commands_path).read_text()).get(issue) or {}

        workdir = self.out / "amalgamator"
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        result = await asyncio.to_thread(
            adapter.run_case,
            amalgamator_root=os.environ["AMALGAMATOR_ROOT"],
            issue_id=issue,
            description=arm_inputs.read_jira_summary(issue)
            or f"Backport the upstream fix for {package} tracked as {issue}",
            upstream_clone=target_repo,
            workdir=workdir,
            base_commit=base_commit,
            fix_commit=fix_commit,
            fix_patch=fix_patch,
            source_branch=source_branch,
            build_command=commands.get("build", ""),
            test_command=commands.get("test", ""),
            model=os.getenv("AMALGAMATOR_MODEL") or None,
            models_path=os.getenv("AMALGAMATOR_MODELS") or None,
            timeout=int(os.getenv("AMALGAMATOR_TIMEOUT", str(adapter.DEFAULT_TIMEOUT_SECONDS))),
            python=os.getenv("AMALGAMATOR_PYTHON") or None,
        )
        self.record["amalgamator"] = result.to_dict()
        self._save()
        if result.status != "success" or not result.candidate_patch:
            return self._fail(
                "Amalgamator did not produce a successful backport: "
                f"{result.status} / {result.outcome} / {result.error}"
            )

        # Put Amalgamator's commits on top of the recorded base, where the
        # agent's own cherry-picks would have been.
        shutil.copyfile(result.candidate_patch, self.out / "candidate.patch")
        code, out = await _git(target_repo, "am", "--3way", "--keep-cr", str(result.candidate_patch))
        if code != 0:
            await _git(target_repo, "am", "--abort")
            code, out = await _git(target_repo, "apply", "--index", str(result.candidate_patch))
            if code == 0:
                code, out = await _git(
                    target_repo, "commit", "-m", f"Backport of {upstream_url} (Amalgamator)"
                )
        if code != 0:
            return self._fail(f"Amalgamator's candidate does not apply to the prepared state: {out[-2000:]}")
        _, count = await _git(target_repo, "rev-list", "--count", f"{base_commit}..HEAD")
        self.record["backport_commits"] = int(count.strip() or 0)
        self._save()

        # Ymir's own agent finishes: patch generation, spec, prep, SRPM, review.
        packaging_prompt = prompt + PACKAGING_PROMPT.format(
            unpacked_sources=unpacked, upstream_repo=upstream, base_commit=base_commit
        )
        response = await self.packaging_agent.run(packaging_prompt, expected_output=expected_output, **kwargs)
        self.record["packaging_result"] = response.last_message.text
        self._save()
        return response
