"""Parse Testing Farm results from anewa MR comments and format the resulting
GitLab/Jira markdown comments for the ``mr_testing`` workflow.

Ported from the ``mr_testing`` Claude Code skill's ``parse_results.py``
(``parse_anewa_comments`` action only -- this agent only ever works from MR
comments, never a local statedir) and ``format_comments.py`` (all six
actions). The exact parsing/formatting logic and markdown templates are
preserved so output stays consistent with the skill this replaces.
"""

import logging
import re
from typing import Any

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.tools.base import CloneableTool as Tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# parse_mr_test_results (ported from parse_results.py's parse_anewa_comments)
# ---------------------------------------------------------------------------


def _comment_text(comment: dict[str, Any]) -> str:
    """Return a comment's text body, whatever the gateway tool called it.

    ``get_authorized_comments_from_merge_request`` returns ``message``,
    ``fetch_gitlab_mr_notes`` returns ``body`` -- accept either.
    """
    return comment.get("body") or comment.get("message") or ""


def _comment_id(comment: dict[str, Any]) -> Any:
    """Return a comment's identifier, if the caller's data includes one."""
    return comment.get("id", comment.get("discussion_id"))


def _split_table_row(line: str) -> list[str]:
    """Split a markdown table row into cells, preserving empty (blank) cells.
    Only the leading/trailing empty strings produced by the row's outer
    `|` delimiters are dropped -- interior blank cells must be kept so
    column positions stay aligned with the header."""
    parts = line.split("|")
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [cell.strip() for cell in parts]


def _parse_markdown_table(body: str) -> list[dict[str, Any]]:
    """Parse the first markdown table found in `body` into row dicts."""
    results_by_arch = []
    lines = body.split("\n")
    in_table = False
    headers: list[str] = []

    for line in lines:
        if "|" not in line:
            continue

        cells = _split_table_row(line)

        if not cells:
            continue

        # First line with cells is the header
        if not in_table:
            headers = cells
            in_table = True
            continue

        # Skip separator line
        if all(c.startswith("---") or c == "" for c in cells):
            continue

        # This is a data row. anewa's table has no dedicated "Overall"
        # column -- each plan is a separate testing-farm request with its
        # own artifacts URL, linked directly in that plan's own cell (e.g.
        # `[pass](url)`), rather than one link shared per architecture
        # row. Older anewa comments (posted before per-plan links existed)
        # instead have a single "Artifacts" column right after
        # "Architecture", which is handled here for backward compatibility.
        if len(cells) >= 1:
            arch = cells[0]
            row_data: dict[str, Any] = {"architecture": arch}

            for i, header in enumerate(headers[1:], start=1):
                if i >= len(cells):
                    continue
                cell = cells[i]
                header_lower = header.lower()

                if header_lower == "artifacts":
                    match = re.search(r"\[artifacts\]\(([^)]+)\)", cell)
                    if match:
                        row_data["artifacts_url"] = match.group(1)
                    continue

                if header_lower == "result":
                    # No plan names were known yet (e.g. "all-plans" before
                    # testing-farm reported results).
                    row_data["overall_status"] = cell
                    continue

                # A plan column, named `` `/plans/...` `` -- strip the
                # backticks. The cell may be a markdown link wrapping the
                # status if this plan's own artifacts URL was known.
                plan_name = header.strip("`")
                link_match = re.match(r"\[(.*)\]\(([^)]+)\)$", cell)
                if link_match:
                    row_data[f"plan_{plan_name}"] = link_match.group(1)
                    row_data[f"plan_{plan_name}_url"] = link_match.group(2)
                else:
                    row_data[f"plan_{plan_name}"] = cell

            results_by_arch.append(row_data)

    return results_by_arch


def _extract_report_portal_url(body: str) -> str | None:
    """Find a Report Portal link in an anewa comment body, if anewa posted
    one. Matches either a markdown link whose text mentions "Report Portal"
    (e.g. `[Report Portal](https://...)`) or a labeled line
    (`**Report Portal:** https://...`), since anewa's exact phrasing isn't
    guaranteed to stay constant."""
    link_match = re.search(r"\[[^\]]*report\s*portal[^\]]*\]\((https?://[^\s)]+)\)", body, re.IGNORECASE)
    if link_match:
        return link_match.group(1)

    label_match = re.search(r"report\s*portal\s*:\*?\*?\s*(https?://\S+)", body, re.IGNORECASE)
    if label_match:
        return label_match.group(1)

    return None


def _extract_nvr(body: str) -> str | None:
    match = re.search(r"\*\*Build:\*\*\s+`([^`]+)`", body)
    return match.group(1) if match else None


def parse_anewa_mr_comments(
    comments: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]], Any, str | None, str | None]:
    """Parse anewa comments from an MR.

    Args:
        comments: List of comment dicts, each with at least a text field
            (``body`` or ``message``), ``created_at``, and optionally an
            identifier (``id`` or ``discussion_id``).

    Returns:
        (nvr, results_by_arch, unfixed_results_by_arch, note_id, note_body,
        report_portal_url) -- see module docstring / the ``mr_testing`` skill
        this was ported from for the full semantics of each field.
    """
    anewa_comments = [c for c in comments if "**ANEWA:**" in _comment_text(c)]

    if not anewa_comments:
        return None, [], [], None, None, None

    # Sort by creation time (newest last)
    anewa_comments.sort(key=lambda c: c.get("created_at") or "")

    # Get the most recent completed comment
    completed_index = None
    for i in range(len(anewa_comments) - 1, -1, -1):
        if "completed" in _comment_text(anewa_comments[i]):
            completed_index = i
            break

    if completed_index is None:
        # No completed comment -- fall back to the NVR and Report Portal link
        # of the most recent comment that has one, since there's nothing to
        # match it against.
        nvr = None
        for comment in reversed(anewa_comments):
            nvr = _extract_nvr(_comment_text(comment))
            if nvr:
                break
        report_portal_url = None
        for comment in reversed(anewa_comments):
            report_portal_url = _extract_report_portal_url(_comment_text(comment))
            if report_portal_url:
                break
        return nvr, [], [], None, None, report_portal_url

    completed_comment = anewa_comments[completed_index]

    # Find the NVR belonging to this same completed comment, not just the
    # first Build: line across all anewa comments -- if a respin happened
    # mid-testing, an older comment's NVR would otherwise be mismatched
    # against this (newer) completed comment's results. New-style single
    # comments carry their own Build: line; older two-comment-style runs
    # only have it on the preceding "started" comment, so walk backwards
    # from the completed comment to the nearest one that has it.
    nvr = None
    for i in range(completed_index, -1, -1):
        nvr = _extract_nvr(_comment_text(anewa_comments[i]))
        if nvr:
            break

    # Same reasoning as the NVR walk-back above: use the Report Portal link
    # belonging to this run, not whichever anewa comment happened to post
    # one first -- a respin's stale link would otherwise outrank the current
    # run's.
    report_portal_url = None
    for i in range(completed_index, -1, -1):
        report_portal_url = _extract_report_portal_url(_comment_text(anewa_comments[i]))
        if report_portal_url:
            break

    body = _comment_text(completed_comment)
    note_id = _comment_id(completed_comment)

    # anewa posts one combined comment with "### Fixed build" / "### Unfixed
    # build" headings when --issue triggered a companion run; fall back to
    # parsing the whole body as a single table when there are no headings
    # (plain runs, and older comments from before this feature existed).
    fixed_match = re.search(
        r"^### Fixed build\s*$(.*?)(?=^### Unfixed build\s*$|\Z)", body, re.MULTILINE | re.DOTALL
    )
    unfixed_match = re.search(r"^### Unfixed build\s*$(.*)", body, re.MULTILINE | re.DOTALL)

    if fixed_match or unfixed_match:
        results_by_arch = _parse_markdown_table(fixed_match.group(1)) if fixed_match else []
        unfixed_results_by_arch = _parse_markdown_table(unfixed_match.group(1)) if unfixed_match else []
    else:
        results_by_arch = _parse_markdown_table(body)
        unfixed_results_by_arch = []

    return nvr, results_by_arch, unfixed_results_by_arch, note_id, body, report_portal_url


class ParseMrTestResultsInput(BaseModel):
    comments: list[dict[str, Any]] = Field(
        description=(
            "MR comments as returned by the fetch_gitlab_mr_notes or "
            "get_authorized_comments_from_merge_request gateway tools -- each a dict with "
            "a text field (body or message), created_at, and optionally an id/discussion_id."
        )
    )


class ParsedMrTestResults(BaseModel):
    nvr: str | None = Field(default=None, description="Package NVR being tested, from anewa's Build: line")
    results_by_arch: list[dict[str, Any]] = Field(
        default_factory=list, description="Per-architecture results for the fixed build"
    )
    unfixed_results_by_arch: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-architecture results for the unfixed-build companion run (empty if none)",
    )
    note_id: Any = Field(default=None, description="Identifier of anewa's completed comment, if found")
    note_body: str | None = Field(
        default=None, description="Raw body of anewa's completed comment, if found"
    )
    report_portal_url: str | None = Field(
        default=None, description="Report Portal link found in an anewa comment"
    )


class ParseMrTestResultsTool(
    Tool[ParseMrTestResultsInput, ToolRunOptions, JSONToolOutput[ParsedMrTestResults]]
):
    """Parse anewa's Testing Farm result comments on a GitLab MR.

    Extracts the package NVR, per-architecture/per-plan pass/fail status and
    artifact URLs, the completed comment's own identifier/body (so it can be
    edited in place), and any Report Portal link. Handles both the modern
    per-plan-artifact-link comment format and the older shared-artifact-link
    format.
    """

    name = "parse_mr_test_results"  # type: ignore
    description = (  # type: ignore
        "Parse anewa's Testing Farm result comments posted on a GitLab merge request. "
        "Extracts the NVR under test, per-architecture/per-plan pass/fail status and "
        "artifact URLs (for both the fixed build and, if present, the unfixed-build "
        "companion run), the completed comment's own id/body, and any Report Portal link. "
        "Returns empty/null fields if no anewa comment is found."
    )
    input_schema = ParseMrTestResultsInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "mr_testing", self.name],
            creator=self,
        )

    async def _run(
        self,
        input: ParseMrTestResultsInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[ParsedMrTestResults]:
        nvr, results_by_arch, unfixed_results_by_arch, note_id, note_body, report_portal_url = (
            parse_anewa_mr_comments(input.comments)
        )
        return JSONToolOutput(
            result=ParsedMrTestResults(
                nvr=nvr,
                results_by_arch=results_by_arch,
                unfixed_results_by_arch=unfixed_results_by_arch,
                note_id=note_id,
                note_body=note_body,
                report_portal_url=report_portal_url,
            )
        )


# ---------------------------------------------------------------------------
# format_mr_testing_comment (ported from format_comments.py, all 6 actions)
# ---------------------------------------------------------------------------

# Templates ported verbatim from the mr_testing skill's templates.json, kept
# in sync with that file's formatting (GitLab shortcode emoji vs. plain
# unicode for Jira, table layouts, etc).
_TEMPLATES: dict[str, str] = {
    "testing_farm_results_markdown": (
        "## Testing Farm Results\n\n### Summary\n{summary}\n\n<details>\n"
        "<summary><strong>Results by Architecture</strong></summary>\n\n{results_table}\n\n</details>\n"
        "{unfixed_section}\n### Test Plan Results\n\n**Passed Plans:**\n{plan_section}\n\n"
        "{test_coverage_section}\n\n<details>\n<summary><strong>Request Details</strong></summary>\n\n"
        "{request_section}\n\n</details>\n\n---\n\n"
        "**These are full testing results that can be used for final verification of linked Jira issue(s).**"
    ),
    "testing_farm_results_markdown_folded": (
        "## Testing Farm Results\n\n### Summary\n{summary}\n\n### Test Plan Results\n\n"
        "**Passed Plans:**\n{plan_section}\n\n{test_coverage_section}\n\n<details>\n"
        "<summary><strong>Request Details</strong></summary>\n\n{request_section}\n\n</details>\n\n---\n\n"
        "**These are full testing results that can be used for final verification of linked Jira issue(s).**"
    ),
    "cve_verification_markdown": (
        "## {cve_id} Test Verification\n\nThe security test for **{cve_id}** linked to issue "
        "[{jira_key}](https://issues.redhat.com/browse/{jira_key}) was executed and **{test_status}**:\n\n"
        "{results_table}\n{fix_verification_section}\n**Test Details:**\n"
        "- Test source: [gitlab.com/redhat/rhel/tests/{component}/.../{test_path}]"
        "(https://gitlab.com/redhat/rhel/tests/{component}/-/tree/master/{test_path})\n"
        "- Package tested: `{nvr}`\n- Test executed as part of `{plan_name}`\n{phases_line}\n\n"
        "**Example test execution logs:**\n{journal_links}\n\n{closing_note}"
    ),
    "untested_issues_markdown": (
        "## Untested Issues\n\n:warning: **This thread requires human review before merging.**\n\n"
        "One or more issues resolved by this MR have no verifiable test evidence in this test run, "
        "and their `Test Coverage` field doesn't indicate this is expected (`RegressionOnly`).\n\n"
        "{issues_table}\n\nPlease confirm whether this is expected (and update `Test Coverage` accordingly) "
        "or whether test coverage is missing and needs to be added, then resolve this thread."
    ),
    "test_failures_markdown": (
        "## Test Failure Analysis\n\n:warning: **This thread requires human review before merging, "
        "even where a waiver is suggested below.** Please resolve this thread only after reviewing and "
        "agreeing with the assessment.\n\n{failure_sections}\n\n---\n\n"
        "*This is AI analysis of the test failure and it might be incorrect.*"
    ),
    "test_coverage_section_markdown": "\n### Test Coverage for Linked Issues\n\n{coverage_table}",
    "failure_section_markdown": (
        "<details>\n<summary><strong>{test_name}</strong> — {architectures} | {result}</summary>\n\n"
        "**Phase:** {phase}\n\n**What happened:**\n{what_happened}\n\n"
        "**Key error output:**\n```\n{key_error}\n```\n\n"
        "{journal_section}**Likely cause:**\n{likely_cause}\n\n**Assessment:** {assessment}\n\n</details>"
    ),
    "jira_issue_coverage_found": "The test covering this issue was executed:\n\n{results_table}",
    "jira_issue_coverage_found_unfixed_fixed": (
        "The test covering this issue was executed against both the unfixed and fixed build:\n\n"
        "{results_table}\n\n{verdict_lines}"
    ),
    "jira_issue_coverage_not_found": "⚠️ **No test was found covering this issue.** {reason}",
    "jira_report_portal_section": "\n**Report Portal:** [{report_portal_url}]({report_portal_url})\n",
    "jira_summary_comment": (
        "## Testing Results\n\n{test_section}\n\n{issue_coverage}\n{report_portal_section}\n"
        "For full regression testing results and detailed test failure analysis, see the "
        "[merge request]({mr_url})."
    ),
}


def _format_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Format a markdown table."""
    header_line = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join(["---"] * len(headers)) + " |"
    row_lines = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([header_line, separator] + row_lines)


def _format_jira_table(headers: list[str], rows: list[list[str]]) -> str:
    """Format a table for a Jira comment. jira-cli's to_adf() parses standard
    markdown into real ADF nodes (headings, tables, links, ...), so this is
    just _format_markdown_table under a Jira-facing name."""
    return _format_markdown_table(headers, rows)


# GitLab renders `:shortcode:` emoji markup in comments. Jira's ADF (as
# produced by jira-cli's to_adf()) does not expand that syntax at all -- it
# shows the literal text ":white_check_mark:" instead of a checkmark -- so
# anything posted straight to Jira must keep the plain unicode emoji.
_EMOJI_SHORTCODES = {"✅": ":white_check_mark:", "❌": ":x:", "⚠️": ":warning:"}


def _render_emoji(unicode_emoji: str, target: str) -> str:
    return _EMOJI_SHORTCODES[unicode_emoji] if target == "gitlab" else unicode_emoji


def _convert_emoji_markup(text: str, target: str) -> str:
    """Convert literal unicode emoji in `text` to GitLab `:shortcode:` markup
    when target == "gitlab"; left as unicode (Jira-safe) otherwise."""
    if target != "gitlab":
        return text
    for unicode_emoji, shortcode in _EMOJI_SHORTCODES.items():
        text = text.replace(unicode_emoji, shortcode)
    return text


def _plan_summary_phrase(passed_count: int, total_count: int, arch_list: str) -> str:
    """Phrase for a plan's pass summary line. Only claims "all" when the
    plan actually passed on every tested architecture."""
    if passed_count == total_count:
        return f"passed on all {total_count} architectures ({arch_list})"
    return f"passed on {passed_count} of {total_count} architectures ({arch_list})"


def _plan_status_is_pass(status: str) -> bool:
    """A plan cell counts as fully passed only if it carries no failure/error
    indicator -- a naive substring check on "pass" false-matches status text
    like "failed (2 fail, 10 pass)", which also contains the word "pass"."""
    s = status or ""
    if "❌" in s or ":x:" in s or "⚠️" in s or ":warning:" in s:
        return False
    return "✅" in s or "pass" in s.lower()


def _build_results_table_markdown(
    architectures: list[str],
    results_by_arch: dict[str, dict[str, Any]],
    plans: list[str],
    target: str = "gitlab",
) -> str:
    headers = ["Architecture", "Overall"] + plans + ["Artifacts"]
    rows = []

    for arch in architectures:
        arch_data = results_by_arch.get(arch, {})
        overall = arch_data.get("overall_status", "❓ unknown")
        overall = _convert_emoji_markup(overall, target)

        row = [arch, overall]

        for plan in plans:
            plan_status = arch_data.get("plans", {}).get(plan, "")
            plan_status = _convert_emoji_markup(plan_status, target)
            row.append(plan_status)

        artifacts_url = arch_data.get("artifacts_url", "")
        if artifacts_url:
            row.append(f"[artifacts]({artifacts_url})")
        else:
            row.append("")

        rows.append(row)

    return _format_markdown_table(headers, rows)


def _format_testing_farm_results_markdown(
    all_passed: bool,
    architectures: list[str],
    results_by_arch: dict[str, dict[str, Any]],
    plans: list[str],
    untested_issues: list[dict[str, Any]] | None = None,
    unfixed_architectures: list[str] | None = None,
    unfixed_results_by_arch: dict[str, dict[str, Any]] | None = None,
    unfixed_plans: list[str] | None = None,
    include_results_table: bool = True,
    target: str = "gitlab",
) -> str:
    """Format the Testing Farm Results comment in markdown, for a GitLab MR
    by default (pass target="jira" for Jira-safe plain unicode emoji)."""
    check, warn = _render_emoji("✅", target), _render_emoji("⚠️", target)
    if all_passed:
        summary = f"{check} **All tests passed** across all architectures"
    else:
        summary = f"{warn} **Some tests failed or errored** - see details below"

    results_table = ""
    unfixed_section = ""
    if include_results_table:
        results_table = _build_results_table_markdown(architectures, results_by_arch, plans, target)

        if unfixed_architectures and unfixed_results_by_arch is not None:
            unfixed_table = _build_results_table_markdown(
                unfixed_architectures, unfixed_results_by_arch, unfixed_plans or [], target
            )
            unfixed_section = (
                "\n<details>\n<summary><strong>Unfixed Build Results</strong></summary>\n\n"
                f"{unfixed_table}\n\n</details>\n"
            )

    plan_lines = []
    for plan in plans:
        passed_arches = [
            arch
            for arch in architectures
            if _plan_status_is_pass(results_by_arch.get(arch, {}).get("plans", {}).get(plan, ""))
        ]
        if passed_arches:
            arch_list = ", ".join(passed_arches)
            phrase = _plan_summary_phrase(len(passed_arches), len(architectures), arch_list)
            plan_lines.append(f"- {check} `{plan}` - {phrase}")

    plan_section = "\n".join(plan_lines) if plan_lines else "No plans passed"

    request_lines = []
    for arch in architectures:
        arch_data = results_by_arch.get(arch, {})
        request_id = arch_data.get("request_id", "")
        if request_id:
            request_lines.append(
                f"- {arch}: [{request_id}](https://api.dev.testing-farm.io/v0.1/requests/{request_id})"
            )

    request_section = "\n".join(request_lines) if request_lines else ""

    test_coverage_section = ""
    if untested_issues:
        coverage_headers = ["Jira Issue", "CVE", "Test Status", "Coverage"]
        coverage_rows = []
        for issue in untested_issues:
            jira_key = issue.get("jira_key", "")
            cve_id = issue.get("cve_id") or "—"
            test_status = issue.get("test_status", "⚠️ NO TEST FOUND")
            coverage = issue.get("coverage", "")
            coverage_rows.append(
                [
                    f"[{jira_key}](https://issues.redhat.com/browse/{jira_key})",
                    cve_id,
                    test_status,
                    coverage,
                ]
            )
        coverage_table = _format_markdown_table(coverage_headers, coverage_rows)
        test_coverage_section = _TEMPLATES["test_coverage_section_markdown"].format(
            coverage_table=coverage_table
        )

    template_key = (
        "testing_farm_results_markdown" if include_results_table else "testing_farm_results_markdown_folded"
    )
    template = _TEMPLATES[template_key]
    format_args = dict(
        summary=summary,
        plan_section=plan_section,
        test_coverage_section=test_coverage_section,
        request_section=request_section,
    )
    if include_results_table:
        format_args["results_table"] = results_table
        format_args["unfixed_section"] = unfixed_section
    return template.format(**format_args)


def _cve_status_is_pass(status: str) -> bool:
    s = (status or "").strip().lower()
    return s == "✅" or "pass" in s


_CVE_VERDICT_EMOJI_UNICODE = {"verified": "✅", "inconclusive": "⚠️", "not fixed": "❌"}
_CVE_VERDICT_DESCRIPTION = {
    "verified": (
        "fails on the unfixed build and passes on the fixed build -- the fix resolves the vulnerability"
    ),
    "inconclusive": (
        "passes on both the unfixed and fixed builds -- cannot confirm the vulnerability reproduces "
        "without the fix"
    ),
    "not fixed": "still fails on the fixed build",
}


def _cve_verdict(unfixed_status: str, fixed_status: str) -> str:
    """Verdict for one architecture's before/after CVE test result. A
    fixed-build failure always means "not fixed", regardless of the
    unfixed build's own result."""
    fixed_pass = _cve_status_is_pass(fixed_status)
    unfixed_pass = _cve_status_is_pass(unfixed_status)
    if not fixed_pass:
        return "not fixed"
    return "inconclusive" if unfixed_pass else "verified"


def _group_archs_by_verdict(verdicts: dict[str, str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for arch, verdict in verdicts.items():
        grouped.setdefault(verdict, []).append(arch)
    return grouped


def _format_cve_verification_markdown(
    cve_id: str,
    jira_key: str,
    architectures: list[str],
    results_by_arch: dict[str, dict[str, Any]],
    component: str,
    plan_name: str,
    test_path: str,
    nvr: str | None = None,
    journal_urls: dict[str, str] | None = None,
    unfixed_results_by_arch: dict[str, dict[str, Any]] | None = None,
    target: str = "gitlab",
) -> str:
    """Format the CVE Test Verification comment in markdown, for a GitLab MR
    by default (pass target="jira" for Jira-safe plain unicode emoji)."""
    all_passed = True
    check, warn = _render_emoji("✅", target), _render_emoji("⚠️", target)
    verdict_emoji = {k: _render_emoji(v, target) for k, v in _CVE_VERDICT_EMOJI_UNICODE.items()}

    if unfixed_results_by_arch is not None:
        headers = ["Architecture", "Unfixed Build", "Fixed Build", "Resolution"]
        rows = []
        verdicts: dict[str, str] = {}

        for arch in architectures:
            fixed_status = results_by_arch.get(arch, {}).get(f"cve_{cve_id}_status", "UNKNOWN")
            unfixed_status = unfixed_results_by_arch.get(arch, {}).get(f"cve_{cve_id}_status", "UNKNOWN")
            if not _cve_status_is_pass(fixed_status):
                all_passed = False

            verdict = _cve_verdict(unfixed_status, fixed_status)
            verdicts[arch] = verdict

            display_unfixed = _convert_emoji_markup(unfixed_status, target)
            display_fixed = _convert_emoji_markup(fixed_status, target)
            rows.append([arch, display_unfixed, display_fixed, f"{verdict_emoji[verdict]} {verdict}"])

        results_table = _format_markdown_table(headers, rows)

        fix_verification_lines = [
            f"- {verdict_emoji[verdict]} **{', '.join(archs)}**: {_CVE_VERDICT_DESCRIPTION[verdict]}"
            for verdict, archs in _group_archs_by_verdict(verdicts).items()
        ]
        fix_verification_section = "\n**Fix Verification:**\n" + "\n".join(fix_verification_lines) + "\n"

        if all(v == "verified" for v in verdicts.values()):
            closing_note = (
                f"This confirms **{cve_id}** is fixed: the vulnerability reproduces on the unfixed build "
                "and is resolved on the fixed build."
            )
        else:
            closing_note = (
                f"{warn} **The fix for {cve_id} could not be fully verified on all architectures** "
                "-- see Fix Verification above."
            )
    else:
        headers = ["Architecture", "Test Result", "Test Path"]
        rows = []
        fix_verification_section = ""

        for arch in architectures:
            arch_data = results_by_arch.get(arch, {})
            status = arch_data.get(f"cve_{cve_id}_status", "UNKNOWN")

            if not _cve_status_is_pass(status):
                all_passed = False

            status = _convert_emoji_markup(status, target)

            rows.append([arch, status, f"`{test_path}`"])

        results_table = _format_markdown_table(headers, rows)
        if all_passed:
            closing_note = (
                "The CVE fix has been verified by automated testing across all supported architectures."
            )
        else:
            failing = [
                a
                for a in architectures
                if not _cve_status_is_pass(results_by_arch.get(a, {}).get(f"cve_{cve_id}_status", "UNKNOWN"))
            ]
            closing_note = (
                f"{warn} **The fix for {cve_id} could not be verified on {', '.join(failing)}** -- see the "
                "table above and the failing architectures' logs before treating this issue as verified."
            )

    journal_lines = []
    if journal_urls:
        for arch, url in journal_urls.items():
            journal_lines.append(f"- [{arch} test journal]({url})")

    journal_section = "\n".join(journal_lines) if journal_lines else ""

    test_status = f"{check} passed" if all_passed else f"{_render_emoji('❌', target)} failed"
    phases_line = (
        "- All test phases (Setup, Test, Cleanup) passed successfully"
        if all_passed
        else "- Not all test phases passed on every architecture -- see the table above"
    )

    template = _TEMPLATES["cve_verification_markdown"]
    return template.format(
        cve_id=cve_id,
        jira_key=jira_key,
        component=component,
        nvr=nvr or "",
        results_table=results_table,
        fix_verification_section=fix_verification_section,
        journal_links=journal_section,
        test_status=test_status,
        plan_name=plan_name,
        test_path=test_path.lstrip("/"),
        closing_note=closing_note,
        phases_line=phases_line,
    )


def _format_untested_issues_markdown(untested_issues: list[dict[str, Any]]) -> str:
    """Format the Untested Issues comment for a GitLab MR (shows all issues)."""
    if not untested_issues:
        return ""

    headers = ["Jira Issue", "CVE", "Test Coverage", "What's missing"]
    rows = []

    for issue in untested_issues:
        jira_key = issue.get("jira_key", "")
        cve_id = issue.get("cve_id") or "—"
        test_coverage = issue.get("test_coverage") or "(not set)"
        reason = issue.get("reason", "")

        rows.append(
            [
                f"[{jira_key}](https://issues.redhat.com/browse/{jira_key})",
                cve_id,
                test_coverage,
                reason,
            ]
        )

    table = _format_markdown_table(headers, rows)
    return _TEMPLATES["untested_issues_markdown"].format(issues_table=table)


def _format_journal_section(journal_urls: dict[str, str] | None) -> str:
    """Build the '**Test logs:**' block for a failure section. Returns ""
    (no section at all) when no URLs were found."""
    if not journal_urls:
        return ""

    lines = [f"- [{arch} test log]({url})" for arch, url in journal_urls.items()]
    return "**Test logs:**\n" + "\n".join(lines) + "\n\n"


def _format_test_failures_markdown(failures: list[dict[str, Any]]) -> str:
    """Format the Test Failure Analysis comment for a GitLab MR (shows all failures)."""
    if not failures:
        return ""

    sections = []
    section_template = _TEMPLATES["failure_section_markdown"]

    for failure in failures:
        test_name = failure.get("test_name", "Unknown Test")
        architectures = failure.get("architectures", [])
        arch_str = ", ".join(architectures) if architectures else "unknown"
        result = failure.get("result", "UNKNOWN")
        phase = failure.get("phase", "Unknown")
        what_happened = failure.get("what_happened", "")
        key_error = failure.get("key_error", "")
        likely_cause = failure.get("likely_cause", "")
        assessment = failure.get("assessment", "")
        journal_section = _format_journal_section(failure.get("journal_urls"))

        sections.append(
            section_template.format(
                test_name=test_name,
                architectures=arch_str,
                result=result,
                phase=phase,
                what_happened=what_happened,
                key_error=key_error,
                journal_section=journal_section,
                likely_cause=likely_cause,
                assessment=assessment,
            )
        )

    sections_text = "\n\n".join(sections)
    return _TEMPLATES["test_failures_markdown"].format(failure_sections=sections_text)


def _format_jira_issue_coverage(
    test_found: bool,
    architectures: list[str] | None = None,
    results_by_arch: dict[str, dict[str, Any]] | None = None,
    cve_id: str | None = None,
    unfixed_results_by_arch: dict[str, dict[str, Any]] | None = None,
    reason: str = "",
) -> str:
    """Build the per-issue test coverage block used inside the single Jira
    summary comment."""
    if not test_found:
        return _TEMPLATES["jira_issue_coverage_not_found"].format(reason=reason)

    status_key = f"cve_{cve_id}_status" if cve_id else "overall_status"
    architectures = architectures or []
    results_by_arch = results_by_arch or {}

    if unfixed_results_by_arch is not None:
        headers = ["Architecture", "Unfixed Build", "Fixed Build", "Resolution"]
        rows = []
        verdicts: dict[str, str] = {}
        for arch in architectures:
            fixed_status = results_by_arch.get(arch, {}).get(status_key, "UNKNOWN")
            unfixed_status = unfixed_results_by_arch.get(arch, {}).get(status_key, "UNKNOWN")
            verdict = _cve_verdict(unfixed_status, fixed_status)
            verdicts[arch] = verdict
            rows.append([arch, unfixed_status, fixed_status, verdict])

        results_table = _format_jira_table(headers, rows)
        verdict_lines = "\n".join(
            f"- **{', '.join(archs)}**: {_CVE_VERDICT_DESCRIPTION[verdict]}"
            for verdict, archs in _group_archs_by_verdict(verdicts).items()
        )
        return _TEMPLATES["jira_issue_coverage_found_unfixed_fixed"].format(
            results_table=results_table, verdict_lines=verdict_lines
        )

    headers = ["Architecture", "Test Result"]
    rows = [[arch, results_by_arch.get(arch, {}).get(status_key, "UNKNOWN")] for arch in architectures]
    results_table = _format_jira_table(headers, rows)
    return _TEMPLATES["jira_issue_coverage_found"].format(results_table=results_table)


def _format_jira_summary_comment(
    jira_key: str,
    mr_url: str,
    test_section: str = "",
    issue_coverage: str = "",
    report_portal_url: str | None = None,
) -> str:
    """Format the single condensed comment posted to a Jira issue."""
    report_portal_section = ""
    if report_portal_url:
        report_portal_section = _TEMPLATES["jira_report_portal_section"].format(
            report_portal_url=report_portal_url
        )
    return _TEMPLATES["jira_summary_comment"].format(
        test_section=test_section,
        issue_coverage=issue_coverage,
        report_portal_section=report_portal_section,
        mr_url=mr_url,
    )


_FORMAT_ACTIONS = (
    "testing_farm_markdown",
    "cve_verification_markdown",
    "jira_issue_coverage",
    "jira_summary_comment",
    "untested_issues_markdown",
    "test_failures_markdown",
)


class FormatMrTestingCommentInput(BaseModel):
    action: str = Field(description=f"Which comment to format. One of: {', '.join(_FORMAT_ACTIONS)}")

    # testing_farm_markdown
    all_passed: bool | None = None
    architectures: list[str] | None = None
    results_by_arch: dict[str, dict[str, Any]] | None = None
    plans: list[str] | None = None
    untested_issues: list[dict[str, Any]] | None = None
    unfixed_architectures: list[str] | None = None
    unfixed_results_by_arch: dict[str, dict[str, Any]] | None = None
    unfixed_plans: list[str] | None = None
    include_results_table: bool = True
    target: str = "gitlab"

    # cve_verification_markdown
    cve_id: str | None = None
    jira_key: str | None = None
    component: str | None = None
    plan_name: str | None = None
    test_path: str | None = None
    nvr: str | None = None
    journal_urls: dict[str, str] | None = None

    # jira_issue_coverage
    test_found: bool | None = None
    reason: str = ""

    # jira_summary_comment
    mr_url: str | None = None
    test_section: str = ""
    issue_coverage: str = ""
    report_portal_url: str | None = None

    # untested_issues_markdown / test_failures_markdown
    failures: list[dict[str, Any]] | None = None


class FormatMrTestingCommentTool(
    Tool[FormatMrTestingCommentInput, ToolRunOptions, StringToolOutput]
):
    """Format Testing Farm/CVE test results into markdown for GitLab MR
    comments, or Jira-facing markdown for Jira comments, for the
    ``mr_testing`` workflow.

    Dispatches on ``action``, mirroring the ``mr_testing`` skill's
    ``format_comments.py`` CLI. Supported actions: ``testing_farm_markdown``,
    ``cve_verification_markdown``, ``jira_issue_coverage``,
    ``jira_summary_comment``, ``untested_issues_markdown``,
    ``test_failures_markdown``.
    """

    name = "format_mr_testing_comment"  # type: ignore
    description = (  # type: ignore
        "Format Testing Farm / CVE test results into the exact markdown used by the mr_testing "
        "workflow for GitLab MR comments and Jira comments. Never hand-compose these comments -- "
        "always use this tool so GitLab emoji shortcodes, Jira-safe unicode emoji, and table "
        "layouts stay correct and consistent. action must be one of: "
        f"{', '.join(_FORMAT_ACTIONS)}."
    )
    input_schema = FormatMrTestingCommentInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "mr_testing", self.name],
            creator=self,
        )

    @staticmethod
    def _require(input: FormatMrTestingCommentInput, *fields: str) -> None:
        missing = [f for f in fields if getattr(input, f) is None]
        if missing:
            raise ToolError(f"action {input.action!r} requires: {', '.join(missing)}")

    async def _run(
        self,
        input: FormatMrTestingCommentInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        if input.action == "testing_farm_markdown":
            self._require(input, "all_passed", "architectures", "results_by_arch", "plans")
            result = _format_testing_farm_results_markdown(
                all_passed=input.all_passed,  # type: ignore[arg-type]
                architectures=input.architectures,  # type: ignore[arg-type]
                results_by_arch=input.results_by_arch,  # type: ignore[arg-type]
                plans=input.plans,  # type: ignore[arg-type]
                untested_issues=input.untested_issues,
                unfixed_architectures=input.unfixed_architectures,
                unfixed_results_by_arch=input.unfixed_results_by_arch,
                unfixed_plans=input.unfixed_plans,
                include_results_table=input.include_results_table,
                target=input.target,
            )

        elif input.action == "cve_verification_markdown":
            self._require(
                input,
                "cve_id",
                "jira_key",
                "architectures",
                "results_by_arch",
                "component",
                "plan_name",
                "test_path",
            )
            result = _format_cve_verification_markdown(
                cve_id=input.cve_id,  # type: ignore[arg-type]
                jira_key=input.jira_key,  # type: ignore[arg-type]
                architectures=input.architectures,  # type: ignore[arg-type]
                results_by_arch=input.results_by_arch,  # type: ignore[arg-type]
                component=input.component,  # type: ignore[arg-type]
                plan_name=input.plan_name,  # type: ignore[arg-type]
                test_path=input.test_path,  # type: ignore[arg-type]
                nvr=input.nvr,
                journal_urls=input.journal_urls,
                unfixed_results_by_arch=input.unfixed_results_by_arch,
                target=input.target,
            )

        elif input.action == "untested_issues_markdown":
            self._require(input, "untested_issues")
            result = _format_untested_issues_markdown(
                untested_issues=input.untested_issues  # type: ignore[arg-type]
            )

        elif input.action == "test_failures_markdown":
            self._require(input, "failures")
            result = _format_test_failures_markdown(failures=input.failures)  # type: ignore[arg-type]

        elif input.action == "jira_issue_coverage":
            self._require(input, "test_found")
            result = _format_jira_issue_coverage(
                test_found=input.test_found,  # type: ignore[arg-type]
                architectures=input.architectures,
                results_by_arch=input.results_by_arch,
                cve_id=input.cve_id,
                unfixed_results_by_arch=input.unfixed_results_by_arch,
                reason=input.reason,
            )

        elif input.action == "jira_summary_comment":
            self._require(input, "jira_key", "mr_url")
            result = _format_jira_summary_comment(
                jira_key=input.jira_key,  # type: ignore[arg-type]
                mr_url=input.mr_url,  # type: ignore[arg-type]
                test_section=input.test_section,
                issue_coverage=input.issue_coverage,
                report_portal_url=input.report_portal_url,
            )

        else:
            raise ToolError(f"Unknown action: {input.action!r}. Must be one of: {', '.join(_FORMAT_ACTIONS)}")

        return StringToolOutput(result=result)
