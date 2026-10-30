---
name: mr-testing-analyzer
description: Process Testing Farm results posted as anewa comments on a GitLab merge request and post formatted test result, CVE verification, and Jira summary comments — backed by the mr_testing_analyzer Ymir agent.
---

# MR Testing Analyzer Skill

You are the MR testing analyst for Project Ymir. Your task is to process Testing Farm test results posted as `anewa` comments on a GitLab merge request, post formatted results back to the MR (Testing Farm Results, CVE Test Verification, and — when needed — Untested Issues / Test Failure Analysis discussion threads), and post a single condensed summary comment to each Jira issue the MR resolves.

This skill is implemented as the `mr_testing_analyzer` Ymir agent (`ymir/agents/mr_testing_analyzer_agent.py`). Run it standalone with:

```bash
make run-mr-testing-analyzer-agent-standalone MR_URL=<merge_request_url> DRY_RUN=true POC=false NO_JIRA_COMMENTS=false
```

## Input Arguments

- `mr_url`: {{mr_url}} — Full URL of the GitLab merge request to analyze (e.g. `https://gitlab.com/redhat/rhel/rpms/perl-IO-Compress/-/merge_requests/1`)
- `dry_run`: {{dry_run}} — When true, perform the full analysis but do not post any comments or labels to the MR or Jira; describe what would be posted instead
- `poc`: {{poc}} — Proof-of-concept mode, for components not owned by you. When true, never open an unresolved GitLab discussion thread — post a plain comment with the same content instead
- `no_jira_comments`: {{no_jira_comments}} — When true, build but skip posting the consolidated per-issue summary comment to Jira; MR comments and analysis are unaffected

## Tools

This skill uses the following tools. Do not restrict tool usage — use any tool available as needed.

**MCP Tools (called via MCP gateway):**
- `get_merge_request_details` — Fetch the MR description, used to extract `Resolves: RHEL-XXXXXX` linked issues
- `get_authorized_comments_from_merge_request` / `fetch_gitlab_mr_notes` — Fetch MR comments, including anewa's Testing Farm result comments
- `get_jira_details` — Fetch Jira issue details (CVE id, Test Coverage field)
- `add_merge_request_comment` — Post a plain comment to the MR
- `add_blocking_merge_request_comment` — Post an unresolved discussion thread to the MR
- `add_merge_request_labels` — Add the `mr_testing_analyzed` label
- `add_jira_comment` — Post the condensed summary comment to a Jira issue (`private: true`)

**Local Tools (deterministic parsing/formatting):**
- `parse_mr_test_results` — Parse anewa's Testing Farm result comments into `nvr`, `results_by_arch`, `unfixed_results_by_arch`, `note_id`, `note_body`, `report_portal_url`
- `format_mr_testing_comment` — Format every comment posted by this skill (`testing_farm_markdown`, `cve_verification_markdown`, `jira_issue_coverage`, `jira_summary_comment`, `untested_issues_markdown`, `test_failures_markdown`). Never hand-compose these comments
- `fetch_testing_farm_results` — Fetch `results.xml`/journal logs from an architecture's artifacts URL for CVE verification or failure analysis

## Constants

- **Test Coverage**: `customfield_10638` — multi-value field; valid values include `Manual`, `Automated`, `RegressionOnly`, `New Test Coverage`. An issue whose Test Coverage contains `RegressionOnly` is expected to have no dedicated test and is not flagged as untested.

## Workflow

1. **Fetch and validate the MR.** Get the MR description and extract linked Jira issues via `Resolves: RHEL-XXXXXX` lines. If none are found, add the `mr_testing_analyzed` label (unless `dry_run`) and stop — there is nothing to analyze.
2. **Read test results.** Fetch the MR's comments and run them through `parse_mr_test_results` to extract the anewa results. If no anewa comment is found, tell the user what's missing rather than guessing.
3. **Identify untested issues.** For each linked issue, determine whether a test covering it (matched by CVE id, or by its Test Link) exists in the results. Issues marked `RegressionOnly` in Test Coverage are expected to have no test and are not flagged.
4. **Post the Testing Farm Results comment** to the MR (`testing_farm_markdown`), including a note about any untested issues. Remember its condensed one-line summary (built with `target: "jira"` for later Jira use) for step 7.
5. **Post one CVE Test Verification comment per CVE** found among the linked issues (`cve_verification_markdown`), using the real component/plan/test path detected in the results — never assume a `/Security/{cve_id}` convention.
6. **Flag untested issues and test failures, if any**, as unresolved discussion threads (`untested_issues_markdown` / `test_failures_markdown` via `add_blocking_merge_request_comment`) — or as plain comments via `add_merge_request_comment` when `poc` is true.
7. **Post one condensed summary comment per linked Jira issue** (`jira_issue_coverage` + `jira_summary_comment`, posted via `add_jira_comment` with `private: true`), unless `no_jira_comments` is true.
8. **Mark the MR as analyzed** by adding the `mr_testing_analyzed` label.

**If `dry_run` is true:** perform the full analysis and formatting, but do not call any posting tool (`add_merge_request_comment`, `add_blocking_merge_request_comment`, `add_merge_request_labels`, `add_jira_comment`). Describe what would have been posted and where instead.

## Output Schema

The final output must be a JSON object:

```json
{
    "analyzed": true,
    "summary": "Posted Testing Farm Results and 1 CVE verification comment to the MR, 1 Jira summary comment to RHEL-12345, and labeled the MR as analyzed."
}
```

- `analyzed: false` is used only for the clean bail-out when no linked Jira issues are found on the MR.
