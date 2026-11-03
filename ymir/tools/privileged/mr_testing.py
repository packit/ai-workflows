"""Parse Testing Farm results from anewa/NEWA/OSCI-gating MR comments and
format the resulting GitLab/Jira markdown comments for the ``mr_testing``
workflow.

Ported from the ``mr_testing`` Claude Code skill's ``parse_results.py``
(``parse_anewa_comments``, ``parse_newa_comment``,
``parse_osci_results_comment``, and ``extract_tf_request_from_job_trace`` --
this agent only ever works from MR comments/CI traces, never a local
statedir) and ``format_comments.py`` (all seven actions, including gating
mode's ``gating_results_markdown``). The exact parsing/formatting logic and
markdown templates are preserved so output stays consistent with the skill
this replaces.

Report Portal integration (reconstructing a full per-architecture breakdown
from a NEWA comment's Report Portal link) is NOT yet ported -- no Report
Portal API tool exists in this agent's toolset. ``parse_newa_mr_comment``
extracts what the NEWA comment itself contains (completed/all_passed/links);
see that function's docstring for what remains unported.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, StringToolOutput, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.tools.base import CloneableTool as Tool

logger = logging.getLogger(__name__)

# Marks where this workflow's own formatted analysis is appended into anewa's
# completed comment (Step 4's in-place fold). Table parsing must stop at this
# marker -- without it, a rerun would re-parse the previously-appended
# analysis (its own "Results by Architecture"/"Test Coverage" tables) as if
# they were more architecture rows in anewa's own table, corrupting results.
# The marker is also how a rerun finds and replaces its own prior fold
# instead of appending a duplicate one every time.
FOLDED_RESULTS_MARKER = "<!-- mr_testing:results -->"


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
    """Return a comment's numeric GitLab note ID, if the caller's data
    includes one -- this is what update_merge_request_comment's note_id
    parameter requires. Deliberately does NOT fall back to discussion_id:
    that's a different, much longer identifier (GitLab discussion IDs are
    hex strings, not small integers) that update_merge_request_comment
    cannot accept -- treating it as a note ID would make every in-place
    fold attempt fail. Callers that get None back from here should fall
    back to posting a new comment instead of trying to update in place,
    exactly as they already do when no anewa comment is found at all.
    """
    return comment.get("note_id") or comment.get("id")


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


_TF_REQUEST_ID_RE = re.compile(r"testing-farm/([^/\s)]+)")


def _parse_markdown_table(body: str) -> dict[str, dict[str, Any]]:
    """Parse the first markdown table found in `body` into the
    architecture-keyed dict shape format_mr_testing_comment's
    testing_farm_markdown/cve_verification_markdown actions require directly
    (``{arch: {"overall_status": ..., "plans": {plan: status}, "artifacts_url": ...,
    "request_id": ...}}``) -- not a flat list of per-row dicts with
    `plan_<name>`-prefixed keys, which doesn't match what those actions
    accept and previously had to be reshaped by hand before every call.
    """
    results_by_arch: dict[str, dict[str, Any]] = {}
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
        # The formatter itself only ever renders one artifacts link per row
        # (the "Artifacts" column), so a row's own artifacts_url -- or,
        # failing that, the first plan's own URL -- is used for that; any
        # other per-plan URLs are otherwise not surfaced in the rendered
        # comment and are intentionally dropped here, same as upstream.
        if len(cells) >= 1:
            arch = cells[0]
            arch_data: dict[str, Any] = {"plans": {}}
            first_plan_url: str | None = None

            for i, header in enumerate(headers[1:], start=1):
                if i >= len(cells):
                    continue
                cell = cells[i]
                header_lower = header.lower()

                if header_lower == "artifacts":
                    match = re.search(r"\[artifacts\]\(([^)]+)\)", cell)
                    if match:
                        arch_data["artifacts_url"] = match.group(1)
                    continue

                if header_lower == "result":
                    # No plan names were known yet (e.g. "all-plans" before
                    # testing-farm reported results).
                    arch_data["overall_status"] = cell
                    continue

                # A plan column, named `` `/plans/...` `` -- strip the
                # backticks. The cell may be a markdown link wrapping the
                # status if this plan's own artifacts URL was known.
                plan_name = header.strip("`")
                link_match = re.match(r"\[(.*)\]\(([^)]+)\)$", cell)
                if link_match:
                    arch_data["plans"][plan_name] = link_match.group(1)
                    if first_plan_url is None:
                        first_plan_url = link_match.group(2)
                else:
                    arch_data["plans"][plan_name] = cell

            if "artifacts_url" not in arch_data and first_plan_url:
                arch_data["artifacts_url"] = first_plan_url

            if arch_data.get("artifacts_url"):
                id_match = _TF_REQUEST_ID_RE.search(arch_data["artifacts_url"])
                if id_match:
                    arch_data["request_id"] = id_match.group(1)

            results_by_arch[arch] = arch_data

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
) -> tuple[str | None, dict[str, dict[str, Any]], dict[str, dict[str, Any]], Any, str | None, str | None]:
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
        return None, {}, {}, None, None, None

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
        return nvr, {}, {}, None, None, report_portal_url

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

    # On a rerun, `body` may already contain this workflow's own previously
    # folded-in analysis (see FOLDED_RESULTS_MARKER above) -- exclude it from
    # table parsing entirely, so its own tables aren't mistaken for more
    # anewa architecture rows. `note_body` (returned below) stays the full,
    # untruncated body, since the caller needs the marker intact to find and
    # replace the prior fold in place.
    anewa_only_body = body.split(FOLDED_RESULTS_MARKER, 1)[0]

    # anewa posts one combined comment with "### Fixed build" / "### Unfixed
    # build" headings when --issue triggered a companion run; fall back to
    # parsing the whole body as a single table when there are no headings
    # (plain runs, and older comments from before this feature existed).
    fixed_match = re.search(
        r"^### Fixed build\s*$(.*?)(?=^### Unfixed build\s*$|\Z)", anewa_only_body, re.MULTILINE | re.DOTALL
    )
    unfixed_match = re.search(r"^### Unfixed build\s*$(.*)", anewa_only_body, re.MULTILINE | re.DOTALL)

    if fixed_match or unfixed_match:
        results_by_arch = _parse_markdown_table(fixed_match.group(1)) if fixed_match else {}
        unfixed_results_by_arch = _parse_markdown_table(unfixed_match.group(1)) if unfixed_match else {}
    else:
        results_by_arch = _parse_markdown_table(anewa_only_body)
        unfixed_results_by_arch = {}

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
    results_by_arch: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Fixed-build results, keyed by architecture -- directly usable as "
        "format_mr_testing_comment's results_by_arch argument, no reshaping needed",
    )
    unfixed_results_by_arch: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Unfixed-build companion run results, keyed by architecture (empty if none)",
    )
    note_id: Any = Field(default=None, description="Identifier of anewa's completed comment, if found")
    note_body: str | None = Field(default=None, description="Raw body of anewa's completed comment, if found")
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
# fold_mr_comment_body -- deterministic merge of this workflow's own
# analysis into anewa's completed comment, idempotent across reruns
# ---------------------------------------------------------------------------


def fold_comment_body(note_body: str, formatted_markdown: str) -> str:
    """Merge `formatted_markdown` into anewa's own `note_body`, in place,
    using FOLDED_RESULTS_MARKER -- the same marker parse_mr_test_results
    stops table-parsing at (see its module-level docstring). Idempotent: a
    rerun replaces everything from the marker onward instead of appending a
    duplicate fold every time, since `note_body` is always the ORIGINAL,
    untruncated comment body (marker included, if present from a prior run).
    """
    base = note_body.split(FOLDED_RESULTS_MARKER, 1)[0].rstrip()
    # The "---" separator is part of the fold, not anewa's own content -- strip
    # a trailing one too, or a rerun would accumulate an extra separator line
    # above the marker every time (the previous fold's own base already ends
    # in "...\n\n---", since that's what this function appended last time).
    base = re.sub(r"\n*-{3,}\s*\Z", "", base).rstrip()
    return f"{base}\n\n---\n\n{FOLDED_RESULTS_MARKER}\n{formatted_markdown}"


class FoldMrCommentBodyInput(BaseModel):
    note_body: str = Field(
        description="anewa's own completed comment body, from parse_mr_test_results' note_body"
    )
    formatted_markdown: str = Field(
        description="This workflow's own formatted analysis (e.g. testing_farm_markdown output) to fold in"
    )


class FoldMrCommentBodyTool(Tool[FoldMrCommentBodyInput, ToolRunOptions, StringToolOutput]):
    """Deterministically build the merged comment body for folding this
    workflow's analysis into anewa's own completed comment, instead of
    posting a near-duplicate new comment. Always use this rather than
    hand-splicing note_body and the formatted markdown together -- it
    handles the marker-based idempotency (a rerun replaces the previous
    fold instead of appending a duplicate) correctly and consistently.
    """

    name = "fold_mr_comment_body"  # type: ignore
    description = (  # type: ignore
        "Build the new body for folding a formatted analysis comment into anewa's own "
        "completed comment in place (via update_merge_request_comment), idempotent across "
        "reruns. Always use this instead of hand-composing the merged body."
    )
    input_schema = FoldMrCommentBodyInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "mr_testing", self.name],
            creator=self,
        )

    async def _run(
        self,
        input: FoldMrCommentBodyInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        return StringToolOutput(result=fold_comment_body(input.note_body, input.formatted_markdown))


# ---------------------------------------------------------------------------
# parse_newa_mr_comment (ported from parse_results.py's parse_newa_comment)
# ---------------------------------------------------------------------------


def parse_newa_mr_comment(comments: list[dict[str, Any]]) -> dict[str, Any]:
    """Find and parse the most recent NEWA comment on an MR.

    Unlike anewa, NEWA posts a single short comment with no per-plan/
    per-architecture table -- just a status line, a Report Portal link, and
    a Jenkins build link. There is nothing here to reconstruct a
    results_by_arch structure from directly; a caller that needs the full
    per-architecture breakdown would need to fetch the Report Portal launch
    itself (no tool for that exists in this agent yet -- see
    ``get_report_portal_launch_artifacts`` in the ``mr_testing`` skill this
    was ported from) and cross-reference each Testing Farm request ID found
    there via ``get_testing_farm_request``. This function only extracts what
    the comment text itself contains.

    Returns a dict with:
        - found: whether a NEWA comment was found at all
        - comment_id: the comment's own identifier (kept only for reference --
          NEWA's comment is never folded into, unlike anewa's; callers must
          always post a new MR comment for results)
        - completed: whether the comment reports testing as finished (False
          for a "NEWA testing started"-style comment, meaning the caller
          should wait rather than process partial results)
        - all_passed: True/False inferred from the comment's leading status
          emoji (checkmark vs warning/cross), or None if no recognizable
          emoji was found -- **never treat this as authoritative**; always
          cross-check against the actual Testing Farm request status once
          that's available, mirroring the skill's own warning that NEWA's
          and Report Portal's own status fields can both say "passed" even
          when every underlying request actually errored before running
          anything.
        - report_portal_url / jenkins_url: links found in the comment, or
          None if absent
    """
    newa_comments = [c for c in comments if re.search(r"\bNEWA\b", _comment_text(c))]

    if not newa_comments:
        return {
            "found": False,
            "comment_id": None,
            "completed": False,
            "all_passed": None,
            "report_portal_url": None,
            "jenkins_url": None,
        }

    newa_comments.sort(key=lambda c: c.get("created_at") or "")
    comment = newa_comments[-1]
    body = _comment_text(comment)

    completed = bool(re.search(r"\bNEWA\b[^\n]*\b(completed|finished|failed)\b", body, re.IGNORECASE))

    all_passed: bool | None = None
    if re.search(r"✅", body[:200]):
        all_passed = True
    elif re.search(r"[❌⚠️]", body[:200]):
        all_passed = False

    rp_match = re.search(r"report\s*portal\s*:\*?\*?\s*(https?://\S+)", body, re.IGNORECASE)
    report_portal_url = rp_match.group(1) if rp_match else _extract_report_portal_url(body)
    if not report_portal_url:
        # Some NEWA variants post the bare launch URL with no "Report
        # Portal:" label at all.
        bare_match = re.search(r"https?://reportportal[^\s)]*", body, re.IGNORECASE)
        report_portal_url = bare_match.group(0) if bare_match else None

    jenkins_match = re.search(r"jenkins\s*build\s*:\*?\*?\s*(https?://\S+)", body, re.IGNORECASE)
    jenkins_url = jenkins_match.group(1) if jenkins_match else None

    return {
        "found": True,
        "comment_id": _comment_id(comment),
        "completed": completed,
        "all_passed": all_passed,
        "report_portal_url": report_portal_url,
        "jenkins_url": jenkins_url,
    }


class ParseNewaMrCommentInput(BaseModel):
    comments: list[dict[str, Any]] = Field(
        description=(
            "MR comments as returned by fetch_gitlab_mr_notes or "
            "get_authorized_comments_from_merge_request -- each a dict with a text "
            "field (body or message), created_at, and optionally an id/discussion_id."
        )
    )


class ParsedNewaMrComment(BaseModel):
    found: bool = Field(description="Whether a NEWA comment was found at all")
    comment_id: Any = Field(default=None, description="The NEWA comment's own identifier, for reference only")
    completed: bool = Field(description="Whether the comment reports testing as finished, not just started")
    all_passed: bool | None = Field(
        default=None,
        description="Inferred from the comment's leading emoji -- NOT authoritative, always cross-check "
        "against the actual Testing Farm request status",
    )
    report_portal_url: str | None = None
    jenkins_url: str | None = None


class ParseNewaMrCommentTool(
    Tool[ParseNewaMrCommentInput, ToolRunOptions, JSONToolOutput[ParsedNewaMrComment]]
):
    """Parse NEWA's Testing Farm result comment on a GitLab MR.

    NEWA posts a much sparser comment than anewa -- just a status line, a
    Report Portal link, and a Jenkins build link, with no per-architecture
    table. This extracts those fields; reconstructing a full per-architecture
    breakdown additionally requires fetching the Report Portal launch itself,
    which no tool in this agent currently supports (see the ``mr_testing``
    skill's Step 1c for the full procedure once such a tool exists).
    """

    name = "parse_newa_mr_comment"  # type: ignore
    description = (  # type: ignore
        "Parse NEWA's Testing Farm result comment posted on a GitLab merge request. "
        "Extracts whether testing completed, the (non-authoritative) overall pass/fail "
        "reading from the comment's leading emoji, and any Report Portal / Jenkins links. "
        "Returns found=false if no NEWA comment is present."
    )
    input_schema = ParseNewaMrCommentInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "mr_testing", self.name],
            creator=self,
        )

    async def _run(
        self,
        input: ParseNewaMrCommentInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[ParsedNewaMrComment]:
        return JSONToolOutput(result=ParsedNewaMrComment(**parse_newa_mr_comment(input.comments)))


# ---------------------------------------------------------------------------
# parse_osci_results_comment / extract_tf_request_from_job_trace
# (ported from parse_results.py, for gating mode)
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_OSCI_JOB_ID_RE = re.compile(r"/-/jobs/(\d+)")

# Matches one job entry in an OSCI bot "**Check(s)**"/"**Build(s)**"/
# "**Test(s)**" <li> list, in either observed link style:
#   "[name](job_url)" (centos-stream/rpms bot, when RESULTS_DATA is missing)
#   "name ([logs](job_url))" (rhel/rpms bot, which never emits RESULTS_DATA)
_OSCI_JOB_LI_RE = re.compile(
    r"(✅|❌|⚠️)\s*"
    r"(?:<b>\(Optional\)</b>\s*)?"
    r"(?:\[([^\]]+)\]\((https?://\S+?)\)"
    r"|([^\n(]+?)\s*\(\[logs\]\((https?://\S+?)\)\))",
)


def _job_status_is_pass(status: str | None) -> bool:
    return status == "✅" or (status or "").strip().lower() == "success"


def _parse_osci_job_sections(body: str) -> dict[str, Any]:
    """Fall back to parsing OSCI bot's human-facing Check(s)/Build(s)/Test(s)
    <li> lists when the comment has no <!--RESULTS_DATA--> JSON blob (observed:
    the rhel/rpms namespace's bot never emits one; centos-stream/rpms's bot
    does). Stage is derived from which section a job's <li> appears under,
    since nothing else in this format carries it."""
    sections = [
        ("check", r"\*\*Check\(s\)\*\*"),
        ("build", r"\*\*Build\(s\)\*\*"),
        ("test", r"\*\*Test\(s\)\*\*"),
    ]
    markers = [(stage, m.start()) for stage, pattern in sections for m in re.finditer(pattern, body)]
    markers.sort(key=lambda t: t[1])

    jobs: dict[str, Any] = {}
    for i, (stage, start) in enumerate(markers):
        end = markers[i + 1][1] if i + 1 < len(markers) else len(body)
        section_text = body[start:end]
        for match in _OSCI_JOB_LI_RE.finditer(section_text):
            status, link_name, link_url, plain_name, plain_url = match.groups()
            name = (link_name or plain_name or "").strip()
            url = link_url or plain_url
            if not name or not url:
                continue
            id_match = _OSCI_JOB_ID_RE.search(url)
            jobs[name] = {
                "id": int(id_match.group(1)) if id_match else None,
                "status": status,
                "url": url,
                "stage": stage,
                "allow_failure": None,
            }

    return jobs


def parse_osci_results_comment(comments: list[dict[str, Any]]) -> dict[str, Any]:
    """Find and parse the OSCI bot's latest pipeline results comment on an MR
    (the ``<!--RESULTS_COMMENT-->``-marked note), for gating mode.

    The comment *sometimes* ends with an HTML-commented ``<!--RESULTS_DATA ...-->``
    JSON blob mapping job name -> {id, status, url, stage, allow_failure,
    pipeline_id} for every job in the pipeline -- when present, that's the
    source of truth for job status/IDs/stage. Not every OSCI bot variant
    includes it (observed: centos-stream/rpms's bot does, rhel/rpms's bot
    doesn't) -- when absent, this falls back to parsing the human-facing
    Check(s)/Build(s)/Test(s) <li> lists directly.

    **Never trust the comment's own leading banner** ("All checks and builds
    passed! 🥳" vs "A check or build failed! 😱") for all_passed -- OSCI bot's
    comment is edited in place as the pipeline progresses, and a real case
    was observed where the banner still said "All checks and builds passed!"
    while the Test(s) list below it showed a failed job and the comment's own
    closing line said the MR was "not ready to merge". all_passed here is
    instead computed from every individual check/build/test job's own
    ✅/❌/⚠️ status (excluding pipeline-scaffolding stages that only exist in
    RESULTS_DATA, like "summary"/"pre_build", whose status says nothing about
    whether the actual checks/builds/tests passed). The raw banner reading is
    returned separately as banner_all_passed for logging/cross-check only.

    Returns a dict with:
        - found, comment_id, pipeline_id, pipeline_url
        - all_passed: computed from job statuses (see above) -- authoritative
        - banner_all_passed: the comment's own banner reading -- not authoritative
        - jobs: dict of job name -> {id, status, url, stage, allow_failure}
        - test_jobs: the subset of jobs that are actual gating test checks
          (stage == "test" and a dot-namespaced name, e.g. osci.tier0 --
          excludes scaffolding like generate_test_jobs/discover_tests, which
          share stage "test" in RESULTS_DATA but have no Testing Farm request
          of their own)
    """
    osci_comments = [c for c in comments if "<!--RESULTS_COMMENT-->" in _comment_text(c)]

    if not osci_comments:
        return {
            "found": False,
            "comment_id": None,
            "pipeline_id": None,
            "pipeline_url": None,
            "all_passed": None,
            "banner_all_passed": None,
            "jobs": {},
            "test_jobs": {},
        }

    osci_comments.sort(key=lambda c: c.get("created_at") or "")
    comment = osci_comments[-1]
    body = _comment_text(comment)

    pipeline_match = re.search(r"Results for pipeline \[(\d+)\]\((https?://\S+)\)", body)
    pipeline_id = pipeline_match.group(1) if pipeline_match else None
    pipeline_url = pipeline_match.group(2) if pipeline_match else None

    banner_all_passed = bool(re.search(r"All checks and builds passed", body))

    jobs: dict[str, Any] = {}
    data_match = re.search(r"<!--RESULTS_DATA\s*(\{.*?\})\s*-->", body, re.DOTALL)
    if data_match:
        try:
            jobs = json.loads(data_match.group(1)).get("jobs", {})
        except json.JSONDecodeError:
            jobs = {}

    if not jobs:
        jobs = _parse_osci_job_sections(body)

    test_jobs = {name: info for name, info in jobs.items() if info.get("stage") == "test" and "." in name}

    relevant_jobs = {
        name: info for name, info in jobs.items() if info.get("stage") in ("check", "build", "test")
    }
    all_passed = (
        all(_job_status_is_pass(info.get("status")) for info in relevant_jobs.values())
        if relevant_jobs
        else None
    )

    return {
        "found": True,
        "comment_id": _comment_id(comment),
        "pipeline_id": pipeline_id,
        "pipeline_url": pipeline_url,
        "all_passed": all_passed,
        "banner_all_passed": banner_all_passed,
        "jobs": jobs,
        "test_jobs": test_jobs,
    }


class ParseOsciResultsCommentInput(BaseModel):
    comments: list[dict[str, Any]] = Field(
        description=(
            "MR comments as returned by fetch_gitlab_mr_notes or "
            "get_authorized_comments_from_merge_request -- each a dict with a text "
            "field (body or message), created_at, and optionally an id/discussion_id."
        )
    )


class ParsedOsciResultsComment(BaseModel):
    found: bool
    comment_id: Any = Field(default=None)
    pipeline_id: str | None = None
    pipeline_url: str | None = None
    all_passed: bool | None = Field(
        default=None, description="Computed from individual job statuses -- authoritative"
    )
    banner_all_passed: bool | None = Field(
        default=None,
        description="The comment's own banner reading -- NOT authoritative, see tool description",
    )
    jobs: dict[str, Any] = Field(default_factory=dict)
    test_jobs: dict[str, Any] = Field(default_factory=dict)


class ParseOsciResultsCommentTool(
    Tool[ParseOsciResultsCommentInput, ToolRunOptions, JSONToolOutput[ParsedOsciResultsComment]]
):
    """Parse OSCI bot's pipeline results comment on a GitLab MR, for gating mode."""

    name = "parse_osci_results_comment"  # type: ignore
    description = (  # type: ignore
        "Parse OSCI bot's latest pipeline results comment (<!--RESULTS_COMMENT-->) on a GitLab "
        "merge request, for gating mode. Extracts pipeline id/url, every job in the pipeline "
        "(jobs), the subset that are actual gating test checks (test_jobs), and an authoritative "
        "all_passed computed from each job's own status -- never trust the comment's own banner "
        "text (returned separately as banner_all_passed), which can go stale. "
        "Returns found=false if no OSCI results comment is present."
    )
    input_schema = ParseOsciResultsCommentInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "mr_testing", self.name],
            creator=self,
        )

    async def _run(
        self,
        input: ParseOsciResultsCommentInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[ParsedOsciResultsComment]:
        return JSONToolOutput(result=ParsedOsciResultsComment(**parse_osci_results_comment(input.comments)))


def extract_tf_request_from_job_trace(trace: str | None) -> dict[str, str | None]:
    """Extract a Testing Farm request ID/URL from an OSCI test job's raw CI
    trace log, for gating mode. This is the only place that URL is exposed
    for a gating pipeline job -- it isn't in the job's GitLab metadata or the
    OSCI bot's comment.

    The exact wording/scheme varies by pipeline variant (observed so far):
        Testing Farm Result: https://artifacts.osci.redhat.com/testing-farm/<id>   (centos-stream/rpms bot)
        [...] url: http://artifacts.osci.redhat.com/testing-farm/<id>              (rhel/rpms bot)
        TEST_RESULT_URL=http://artifacts.osci.redhat.com/testing-farm/<id>         (rhel/rpms bot)

    so this matches the bare artifacts.osci.redhat.com/testing-farm/<id> URL
    itself (http or https) rather than any one surrounding label, and always
    returns it normalized to https. Returns both fields None if no trace was
    given or no such URL was found anywhere in it (e.g. the job never got to
    running tests, or isn't Testing-Farm-backed at all -- some gating checks,
    like rpminspect, run directly in the CI job with no Testing Farm request).
    """
    if not trace:
        return {"request_id": None, "artifacts_url": None}

    clean = _ANSI_RE.sub("", trace)
    match = re.search(r"https?://(artifacts\.osci\.redhat\.com/testing-farm/([0-9a-fA-F-]+))", clean)
    if not match:
        return {"request_id": None, "artifacts_url": None}

    return {"request_id": match.group(2), "artifacts_url": f"https://{match.group(1)}"}


class ExtractTfRequestFromJobTraceInput(BaseModel):
    trace: str | None = Field(default=None, description="Raw CI trace text, from get_pipeline_job_trace")


class ExtractedTfRequest(BaseModel):
    request_id: str | None = None
    artifacts_url: str | None = None


class ExtractTfRequestFromJobTraceTool(
    Tool[ExtractTfRequestFromJobTraceInput, ToolRunOptions, JSONToolOutput[ExtractedTfRequest]]
):
    """Extract a Testing Farm request ID/URL from a gating test job's raw CI trace."""

    name = "extract_tf_request_from_job_trace"  # type: ignore
    description = (  # type: ignore
        "Extract the Testing Farm request_id/artifacts_url printed in a gating test job's raw "
        "CI trace (from get_pipeline_job_trace) -- the only place that URL is exposed for a "
        "gating pipeline job. Both fields are null if the job's trace had no such URL (e.g. a "
        "job that doesn't run through Testing Farm at all, or one that never got that far)."
    )
    input_schema = ExtractTfRequestFromJobTraceInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "mr_testing", self.name],
            creator=self,
        )

    async def _run(
        self,
        input: ExtractTfRequestFromJobTraceInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> JSONToolOutput[ExtractedTfRequest]:
        return JSONToolOutput(result=ExtractedTfRequest(**extract_tf_request_from_job_trace(input.trace)))


# ---------------------------------------------------------------------------
# format_mr_testing_comment (ported from format_comments.py, all 6 actions)
# ---------------------------------------------------------------------------

# Templates ported verbatim from the mr_testing skill's templates.json, kept
# in sync with that file's formatting (GitLab shortcode emoji vs. plain
# unicode for Jira, table layouts, etc), and externalized the same way --
# see mr_testing_templates.json next to this module.
_TEMPLATES_PATH = Path(__file__).parent / "mr_testing_templates.json"
try:
    with open(_TEMPLATES_PATH) as _f:
        _TEMPLATES: dict[str, str] = json.load(_f)
except FileNotFoundError as e:
    raise RuntimeError(f"Templates file not found: {_TEMPLATES_PATH}") from e


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


def _format_gating_results_markdown(
    pipeline_id: str | None,
    pipeline_url: str | None,
    all_passed: bool,
    test_jobs: dict[str, dict[str, Any]],
    target: str = "gitlab",
) -> str:
    """Format a brief gating-mode results comment, for an OSCI bot pipeline's
    test jobs. Deliberately shorter than _format_testing_farm_results_markdown:
    gating mode has no per-plan, per-architecture anewa/NEWA table to report
    against, just one Testing Farm request per gating test job, so a flat
    one-row-per-job table covers it without the collapsible
    sections/plan-summary/request-details boilerplate of the anewa-derived
    comment.

    Args:
        pipeline_id/pipeline_url: the OSCI pipeline this comment reports on
        all_passed: whether every gating test job passed
        test_jobs: dict of job name -> {status, url (job web_url), artifacts_url
            (Testing Farm artifacts URL, or None/absent if the job's trace had
            none), result_summary (e.g. "12 pass" or "1 fail, 11 pass", or
            None/absent if results.xml wasn't checked)}
        target: "gitlab" (default) or "jira" -- see _render_emoji
    """
    check, warn = _render_emoji("✅", target), _render_emoji("⚠️", target)

    summary = (
        f"{check} **All gating tests passed**"
        if all_passed
        else f"{warn} **One or more gating tests failed or errored**"
    )

    headers = ["Test", "Result", "Testing Farm"]
    rows = []
    for name in sorted(test_jobs.keys()):
        info = test_jobs[name]
        status = _convert_emoji_markup(info.get("status") or "❓ unknown", target)
        if info.get("result_summary"):
            status = f"{status} ({info['result_summary']})"
        artifacts_url = info.get("artifacts_url")
        tf_cell = f"[artifacts]({artifacts_url})" if artifacts_url else "—"
        rows.append([f"`{name}`", status, tf_cell])

    table = _format_markdown_table(headers, rows)
    pipeline_line = f"**Pipeline:** [{pipeline_id}]({pipeline_url})" if pipeline_id else ""

    return _TEMPLATES["gating_results_markdown"].format(
        pipeline_line=pipeline_line, summary=summary, table=table
    )


def _cve_status_is_pass(status: str) -> bool:
    """A CVE status counts as passed only if it carries no failure/error
    indicator -- a naive substring check on "pass" false-matches status text
    like "failed (2 fail, 10 pass)", which also contains the word "pass"
    (same bug class as _plan_status_is_pass guards against)."""
    s = (status or "").strip().lower()
    if "❌" in s or ":x:" in s or "⚠️" in s or ":warning:" in s:
        return False
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
    "gating_results_markdown",
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

    # gating_results_markdown
    pipeline_id: str | None = None
    pipeline_url: str | None = None
    test_jobs: dict[str, dict[str, Any]] | None = None

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


class FormatMrTestingCommentTool(Tool[FormatMrTestingCommentInput, ToolRunOptions, StringToolOutput]):
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

        elif input.action == "gating_results_markdown":
            self._require(input, "all_passed", "test_jobs")
            result = _format_gating_results_markdown(
                pipeline_id=input.pipeline_id,
                pipeline_url=input.pipeline_url,
                all_passed=input.all_passed,  # type: ignore[arg-type]
                test_jobs=input.test_jobs,  # type: ignore[arg-type]
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
