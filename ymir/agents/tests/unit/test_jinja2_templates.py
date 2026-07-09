"""Tests for Jinja2 template rendering of agent prompts.

These tests exercise the Jinja2 templates in ymir/agents/prompts/ without
importing the full agent stack (which requires beeai-framework and its heavy
transitive dependencies).  We reconstruct a minimal ``render_template``
directly from Jinja2 + Pydantic so the test suite can run with just
``pip install jinja2 pydantic``.
"""

from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

_env = Environment(
    loader=FileSystemLoader(_PROMPTS_DIR),
    autoescape=False,  # noqa: S701 — LLM prompts, not HTML
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_template(template_name: str, input: BaseModel | None = None) -> str:
    template = _env.get_template(template_name)
    return template.render(input.model_dump(mode="json") if input else {})


try:
    from ymir.common.models import (
        BackportInputSchema,
        BuildInputSchema,
        BuildInstructionsInput,
        LogInputSchema,
        MergeRequestInputSchema,
        RebaseInputSchema,
        TriageInputSchema,
    )
except ImportError:
    # Fallback stand-in schemas when ymir.common is not importable

    class BuildInstructionsInput(BaseModel):  # type: ignore[no-redef]
        has_extract_log_snippets: bool = False

    class BuildInputSchema(BaseModel):  # type: ignore[no-redef]
        srpm_path: Path
        dist_git_branch: str
        jira_issue: str

    class LogInputSchema(BaseModel):  # type: ignore[no-redef]
        jira_issue: str
        changes_summary: str
        source_changelog: str | None = None

    class BackportInputSchema(BaseModel):  # type: ignore[no-redef]
        local_clone: Path
        unpacked_sources: Path
        package: str
        dist_git_branch: str
        jira_issue: str
        cve_id: str | None = None
        upstream_patches: list[str] = Field(default_factory=list)
        build_error: str | None = None
        triage_summary: str | None = None
        has_extract_log_snippets: bool = False

    class RebaseInputSchema(BaseModel):  # type: ignore[no-redef]
        local_clone: Path
        fedora_clone: Path | None = None
        package: str
        dist_git_branch: str
        version: str
        jira_issue: str
        build_error: str | None = None
        triage_summary: str | None = None

    class MergeRequestInputSchema(BaseModel):  # type: ignore[no-redef]
        local_clone: Path
        package: str
        dist_git_branch: str
        jira_issue: str
        merge_request_url: str
        merge_request_title: str
        merge_request_description: str
        comments: str
        fedora_clone: Path | None = None
        build_error: str | None = None

    class TriageInputSchema(BaseModel):  # type: ignore[no-redef]
        issue: str
        is_older_zstream: bool = False
        needs_internal_fix: bool = False
        internal_target_branch: str | None = None


# ---------------------------------------------------------------------------
# Instruction templates (static, no variables)
# ---------------------------------------------------------------------------


class TestBuildInstructions:
    def test_loads_with_extract_log_snippets(self):
        result = render_template(
            "build/instructions.j2",
            BuildInstructionsInput(has_extract_log_snippets=True),
        )
        assert "expert on building packages" in result
        assert "build_package" in result
        assert "builder-live.log" in result
        assert "extract_log_snippets" in result
        assert "start with" not in result

    def test_loads_without_extract_log_snippets(self):
        result = render_template(
            "build/instructions.j2",
            BuildInstructionsInput(has_extract_log_snippets=False),
        )
        assert "expert on building packages" in result
        assert "build_package" in result
        assert "builder-live.log" in result
        assert "extract_log_snippets" not in result
        assert "start with" in result


class TestLogInstructions:
    def test_loads(self):
        result = render_template("log/instructions.j2")
        assert "summarizing packaging changes" in result
        assert "add_changelog_entry" in result
        assert "git diff --cached" in result


class TestRebaseInstructions:
    def test_loads(self):
        result = render_template("rebase/instructions.j2")
        assert "rebasing packages" in result
        assert "rpmdev-vercmp" in result
        assert "spectool" in result
        assert "get_maintainer_rules" in result


class TestMergeRequestInstructions:
    def test_loads(self):
        result = render_template("merge_request/instructions.j2")
        assert "maintaining packages" in result
        assert "accomodate user feedback" in result
        assert "run_package_prep" in result


class TestBackportInstructions:
    def test_normal_loads(self):
        result = render_template("backport/instructions.j2")
        assert "backporting upstream patches" in result
        assert "CHERRY-PICK WORKFLOW" in result
        assert "GIT AM WORKFLOW" in result
        assert "get_maintainer_rules" in result

    def test_zstream_loads(self):
        result = render_template("backport/instructions_zstream.j2")
        assert "backporting upstream patches" in result
        assert "DIST-GIT WORKFLOW" in result
        assert "UPSTREAM CHERRY-PICK WORKFLOW" in result
        assert "detect_distgit_source" in result

    def test_normal_has_spec_only_check(self):
        result = render_template("backport/instructions.j2")
        assert "spec file application" in result.lower() or "spec-only" in result.lower()

    def test_zstream_has_distgit_workflow(self):
        result = render_template("backport/instructions_zstream.j2")
        assert "clone_repository" in result
        assert "DISTGIT_SOURCE" in result


# ---------------------------------------------------------------------------
# User prompt templates (with Jinja2 variables)
# ---------------------------------------------------------------------------


class TestBuildTemplate:
    def test_renders_variables(self):
        result = render_template(
            "build/prompt.j2",
            BuildInputSchema(
                srpm_path=Path("/tmp/pkg-1.0-1.el9.src.rpm"),
                dist_git_branch="c9s",
                jira_issue="RHEL-12345",
            ),
        )
        assert "/tmp/pkg-1.0-1.el9.src.rpm" in result
        assert "c9s" in result
        assert "RHEL-12345" in result


class TestLogTemplate:
    def test_renders_without_source_changelog(self):
        result = render_template(
            "log/prompt.j2",
            LogInputSchema(
                jira_issue="RHEL-12345",
                changes_summary="Rebased to version 2.0",
            ),
        )
        assert "RHEL-12345" in result
        assert "Rebased to version 2.0" in result
        assert "changelog message was used" not in result

    def test_renders_with_source_changelog(self):
        result = render_template(
            "log/prompt.j2",
            LogInputSchema(
                jira_issue="RHEL-12345",
                changes_summary="Backported fix",
                source_changelog="- Fix CVE-2024-1234",
            ),
        )
        assert "Fix CVE-2024-1234" in result
        assert "changelog message was used" in result


class TestBackportTemplate:
    def test_renders_without_build_error(self):
        result = render_template(
            "backport/prompt.j2",
            BackportInputSchema(
                local_clone=Path("/tmp/clone"),
                unpacked_sources=Path("/tmp/sources"),
                package="libfoo",
                dist_git_branch="c9s",
                jira_issue="RHEL-12345",
                upstream_patches=[
                    "https://example.com/patch1.patch",
                    "https://example.com/patch2.patch",
                ],
                build_error=None,
            ),
        )
        assert "/tmp/clone" in result
        assert "libfoo" in result
        assert "c9s" in result
        assert "RHEL-12345" in result
        assert "patch1.patch" in result
        assert "patch2.patch" in result
        assert "repeated backport" not in result

    def test_renders_with_build_error(self):
        result = render_template(
            "backport/prompt.j2",
            BackportInputSchema(
                local_clone=Path("/tmp/clone"),
                unpacked_sources=Path("/tmp/sources"),
                package="libfoo",
                dist_git_branch="c9s",
                jira_issue="RHEL-12345",
                upstream_patches=["https://example.com/patch1.patch"],
                build_error="error: implicit declaration of function 'foo'",
            ),
        )
        assert "repeated backport" in result
        assert "implicit declaration" in result
        assert "Backport upstream patches" not in result

    def test_renders_with_cve_id(self):
        result = render_template(
            "backport/prompt.j2",
            BackportInputSchema(
                local_clone=Path("/tmp/clone"),
                unpacked_sources=Path("/tmp/sources"),
                package="libfoo",
                dist_git_branch="c9s",
                jira_issue="RHEL-12345",
                cve_id="CVE-2024-1234",
                upstream_patches=["https://example.com/patch1.patch"],
                build_error=None,
            ),
        )
        assert "CVE-2024-1234" in result

    def test_renders_without_cve_id(self):
        result = render_template(
            "backport/prompt.j2",
            BackportInputSchema(
                local_clone=Path("/tmp/clone"),
                unpacked_sources=Path("/tmp/sources"),
                package="libfoo",
                dist_git_branch="c9s",
                jira_issue="RHEL-12345",
                upstream_patches=["https://example.com/patch1.patch"],
                build_error=None,
            ),
        )
        assert "a.k.a." not in result


class TestBackportFixBuildErrorTemplate:
    def test_renders_with_extract_log_snippets(self):
        result = render_template(
            "backport/prompt_fix_build_error.j2",
            BackportInputSchema(
                local_clone=Path("/tmp/clone"),
                unpacked_sources=Path("/tmp/sources"),
                package="libfoo",
                dist_git_branch="c9s",
                jira_issue="RHEL-12345",
                upstream_patches=["https://example.com/p1.patch"],
                build_error="undefined reference to 'bar'",
                has_extract_log_snippets=True,
            ),
        )
        assert "cherry-pick workflow succeeded but the build failed" in result
        assert "undefined reference" in result
        assert "fix-attempts.md" in result
        assert "extract_log_snippets" in result
        assert "start with" not in result

    def test_renders_without_extract_log_snippets(self):
        result = render_template(
            "backport/prompt_fix_build_error.j2",
            BackportInputSchema(
                local_clone=Path("/tmp/clone"),
                unpacked_sources=Path("/tmp/sources"),
                package="libfoo",
                dist_git_branch="c9s",
                jira_issue="RHEL-12345",
                upstream_patches=["https://example.com/p1.patch"],
                build_error="undefined reference to 'bar'",
                has_extract_log_snippets=False,
            ),
        )
        assert "cherry-pick workflow succeeded but the build failed" in result
        assert "undefined reference" in result
        assert "fix-attempts.md" in result
        assert "extract_log_snippets" not in result
        assert "get logs and identify the new error" in result


class TestRebaseTemplate:
    def test_renders_basic_rebase(self):
        result = render_template(
            "rebase/prompt.j2",
            RebaseInputSchema(
                local_clone=Path("/tmp/clone"),
                fedora_clone=None,
                package="libfoo",
                dist_git_branch="c9s",
                version="2.0.1",
                jira_issue="RHEL-12345",
                build_error=None,
            ),
        )
        assert "/tmp/clone" in result
        assert "libfoo" in result
        assert "2.0.1" in result
        assert "Fedora repository" not in result
        assert "repeated rebase" not in result

    def test_renders_with_fedora_clone(self):
        result = render_template(
            "rebase/prompt.j2",
            RebaseInputSchema(
                local_clone=Path("/tmp/clone"),
                fedora_clone=Path("/tmp/fedora"),
                package="libfoo",
                dist_git_branch="c9s",
                version="2.0.1",
                jira_issue="RHEL-12345",
                build_error=None,
            ),
        )
        assert "/tmp/fedora" in result
        assert "Fedora repository" in result

    def test_renders_with_build_error(self):
        result = render_template(
            "rebase/prompt.j2",
            RebaseInputSchema(
                local_clone=Path("/tmp/clone"),
                fedora_clone=None,
                package="libfoo",
                dist_git_branch="c9s",
                version="2.0.1",
                jira_issue="RHEL-12345",
                build_error="configure: error: missing dependency",
            ),
        )
        assert "repeated rebase" in result
        assert "missing dependency" in result
        assert "Rebase the package to version" not in result


class TestMergeRequestTemplate:
    def test_renders_without_build_error(self):
        result = render_template(
            "merge_request/prompt.j2",
            MergeRequestInputSchema(
                local_clone=Path("/tmp/clone"),
                package="libfoo",
                dist_git_branch="c9s",
                jira_issue="RHEL-12345",
                merge_request_url="https://gitlab.com/foo/bar/-/merge_requests/1",
                merge_request_title="Fix CVE",
                merge_request_description="This fixes a CVE",
                comments='[{"body": "Please fix the patch"}]',
                fedora_clone=None,
                build_error=None,
            ),
        )
        assert "merge_requests/1" in result
        assert "Fix CVE" in result
        assert "accomodate user feedback" in result

    def test_renders_with_build_error(self):
        result = render_template(
            "merge_request/prompt.j2",
            MergeRequestInputSchema(
                local_clone=Path("/tmp/clone"),
                package="libfoo",
                dist_git_branch="c9s",
                jira_issue="RHEL-12345",
                merge_request_url="https://gitlab.com/foo/bar/-/merge_requests/1",
                merge_request_title="Fix CVE",
                merge_request_description="This fixes a CVE",
                comments="[]",
                fedora_clone=None,
                build_error="build failed",
            ),
        )
        assert "retry" in result.lower()
        assert "build failed" in result


class TestTriageTemplate:
    def test_renders_basic(self):
        result = render_template(
            "triage/prompt.j2",
            TriageInputSchema(issue="RHEL-12345", is_older_zstream=False),
        )
        assert "RHEL-12345" in result
        assert "Fedora" in result
        assert "zstream_search" not in result
        assert "centos-stream" in result
        assert "redhat/rhel/rpms" not in result

    def test_renders_older_zstream(self):
        result = render_template(
            "triage/prompt.j2",
            TriageInputSchema(issue="RHEL-12345", is_older_zstream=True),
        )
        assert "RHEL-12345" in result
        assert "zstream_search" in result
        assert "Do not use upstream patches for older z-streams" in result

    def test_renders_needs_internal_fix(self):
        result = render_template(
            "triage/prompt.j2",
            TriageInputSchema(
                issue="RHEL-189361",
                needs_internal_fix=True,
                internal_target_branch="rhel-10.2",
            ),
        )
        assert "RHEL-189361" in result
        assert "clone_repository" in result
        assert "redhat/rhel/rpms" in result
        assert "rhel-10.2" in result
        assert "centos-stream" in result
        assert "clone_path" in result
        assert "/git-repos/RHEL-189361/" in result

    def test_renders_needs_internal_fix_without_branch_falls_back(self):
        result = render_template(
            "triage/prompt.j2",
            TriageInputSchema(
                issue="RHEL-12345",
                needs_internal_fix=True,
                internal_target_branch=None,
            ),
        )
        assert "centos-stream" in result
        assert "redhat/rhel/rpms" not in result


# Lightweight stand-in models to avoid importing modules with heavy deps (nitrate)


class _PreliminaryTestingInput(BaseModel):
    issue_key: str
    issue_data: str
    build_nvr: str | None
    jira_pull_requests: str
    current_time: datetime


class TestPreliminaryTestingTemplate:
    def test_renders(self):
        result = render_template(
            "preliminary_testing/prompt.j2",
            _PreliminaryTestingInput(
                issue_key="RHEL-12345",
                issue_data="test issue data",
                build_nvr="pkg-1.0-1.el9",
                jira_pull_requests="[]",
                current_time=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        )
        assert "RHEL-12345" in result
        assert "pkg-1.0-1.el9" in result
        assert "GreenWave" in result

    def test_mustache_bug_fixes(self):
        """Verify that former Mustache variable lookups are now literal text."""
        result = render_template(
            "preliminary_testing/prompt.j2",
            _PreliminaryTestingInput(
                issue_key="RHEL-1",
                issue_data="x",
                build_nvr=None,
                jira_pull_requests="[]",
                current_time=datetime(2025, 1, 1, tzinfo=UTC),
            ),
        )
        assert "{<classname>/<testname>}" in result
        assert "{panel}" in result


class _IssueVerificationInput(BaseModel):
    issue: object
    erratum: object
    maintainer_rules: str
    current_time: datetime


class TestIssueVerificationTemplates:
    def _make_input(self):
        return _IssueVerificationInput(
            issue={"key": "RHEL-12345"},
            erratum={"advisory_name": "RHBA-2025:0001"},
            maintainer_rules="no special rules",
            current_time=datetime(2025, 1, 1, tzinfo=UTC),
        )

    def test_normal_template(self):
        result = render_template("issue_verification/normal.j2", self._make_input())
        assert "testing analyst agent" in result
        assert "NEWA" in result
        assert "tests-passed" in result

    def test_after_baseline_template(self):
        result = render_template("issue_verification/after_baseline.j2", self._make_input())
        assert "testing analyst agent" in result
        assert "baseline" in result
        assert "regressions" in result

    def test_common_included(self):
        result = render_template("issue_verification/normal.j2", self._make_input())
        assert "JIRA_ISSUE_DATA:" in result
        assert "ERRATUM_DATA:" in result
        assert "MAINTAINER_RULES:" in result
        assert "CURRENT_TIME:" in result


# ---------------------------------------------------------------------------
# render_template infrastructure
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_no_input(self):
        """Static templates work without an input model."""
        result = render_template("build/instructions.j2")
        assert len(result) > 100

    def test_with_input(self):
        """Templates with variables are rendered correctly."""
        result = render_template(
            "build/prompt.j2",
            BuildInputSchema(
                srpm_path=Path("/tmp/test.src.rpm"),
                dist_git_branch="c10s",
                jira_issue="RHEL-99999",
            ),
        )
        assert "RHEL-99999" in result
        assert "/tmp/test.src.rpm" in result

    def test_path_serialization(self):
        """Path objects are serialized as strings, not PosixPath repr."""
        result = render_template(
            "build/prompt.j2",
            BuildInputSchema(
                srpm_path=Path("/tmp/test.src.rpm"),
                dist_git_branch="c10s",
                jira_issue="RHEL-1",
            ),
        )
        assert "PosixPath" not in result
