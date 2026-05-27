from pathlib import Path
from textwrap import dedent

from beeai_framework.agents.requirement.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool

from ymir.agents.reasoning_agent import ReasoningAgent
from ymir.agents.utils import get_chat_model, get_tool_call_checker_config, is_reasoning_enabled
from ymir.common.models import Resolution
from ymir.tools.unprivileged.commands import RunShellCommandTool
from ymir.tools.unprivileged.text import SearchTextTool, ViewTool


def create_applicability_agent(
    gateway_tools: list[Tool],
    local_tool_options: dict,
) -> ReasoningAgent:
    extra_gateway_tools = [t for t in gateway_tools if t.name in ["get_jira_details", "get_maintainer_rules"]]
    return ReasoningAgent(
        name="ApplicabilityAgent",
        llm=get_chat_model(),
        unconstrained=is_reasoning_enabled(),
        tool_call_checker=get_tool_call_checker_config(),
        tools=[
            ThinkTool(),
            ViewTool(options=local_tool_options),
            SearchTextTool(options=local_tool_options),
            RunShellCommandTool(options=local_tool_options),
            DuckDuckGoSearchTool(),
            *extra_gateway_tools,
        ],
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(
                ThinkTool,
                force_at_step=1,
                force_after=Tool,
                consecutive_allowed=False,
                only_success_invocations=False,
            ),
        ],
        middlewares=[GlobalTrajectoryMiddleware(pretty=True)],
        role="Red Hat security analyst",
    )


def build_applicability_prompt(
    *,
    jira_issue: str,
    package: str,
    target_branch: str,
    resolution: Resolution,
    cve_id: str | None,
    dep_component: str | None,
    dep_issue_key: str | None,
    patch_files: list[str],
    unpacked_sources: Path,
    local_clone: Path,
    prep_ok: bool = True,
) -> str:
    cve_label = cve_id or "the CVE"

    rebuild_context = ""
    if resolution in (Resolution.REBUILD, Resolution.POSTPONED) and dep_component:
        rebuild_context = f"\nThis is a dependency rebuild against updated '{dep_component}'."
        if dep_issue_key:
            rebuild_context += (
                f"\nDependency Jira issue: {dep_issue_key} "
                f"(use get_jira_details for context on what was fixed)."
            )
        rebuild_context += (
            f"\nCheck whether '{package}' actually uses the affected API/module "
            f"of '{dep_component}' (e.g. check Go imports, C includes, "
            f"Python imports, linked libraries)."
            f"\n\nREBUILD CAUTION: The bar for declaring a rebuild 'not affected' "
            f"is very high. A false negative means skipping a security rebuild "
            f"entirely. Only classify as not affected if you have strong, concrete "
            f"evidence — e.g. the package provably does not import/link/use the "
            f"affected module at all. If there is any ambiguity — transitive "
            f"dependencies, conditional imports, build-time usage, or you simply "
            f"cannot verify the full dependency chain — classify as 'Inconclusive'.\n"
        )

    sources_rel = unpacked_sources.relative_to(local_clone)
    if patch_files:
        patch_info = "Upstream fix patches are available at: " + ", ".join(patch_files)
    else:
        patch_info = "No upstream fix patch available."

    fallback_warning = ""
    if not prep_ok:
        fallback_warning = (
            "\nIMPORTANT: RPM prep failed — the source tree is unpatched upstream "
            "source (Source0 extraction only). Downstream patches are NOT applied. "
            "If you find vulnerable code, it may already be patched in the shipped "
            "version. Factor this into your confidence level.\n"
        )

    return dedent(f"""\
        Analyze whether {cve_label} affects package '{package}'
        at the version shipped in branch '{target_branch}'.

        Jira issue: {jira_issue}
        Triage resolution: {resolution.value}
        {rebuild_context}
        {patch_info}
        {fallback_warning}
        The unpacked package source is at: {sources_rel}

        CRITICAL: Your analysis MUST be based on the package source at
        {sources_rel} — this is the actual version shipped in RHEL.
        Do NOT clone or check the latest upstream repository — it may
        already contain the fix, which is irrelevant to whether the
        shipped RHEL version is affected. If the fix patch applies
        cleanly to the package source (e.g. via `git apply --check`
        or `patch --dry-run`), that is strong evidence the package
        IS affected (the vulnerable code is present and unfixed).

        Steps:
        0. Use get_maintainer_rules with package '{package}' to check for
           maintainer-specific guidelines. If rules are found, treat them
           as additional context — e.g. if they indicate rebuilds are always
           relevant, classify as Inconclusive rather than Not Affected.
        1. Use get_jira_details on {jira_issue} to understand the
           CVE context and what is affected. Also check the Jira
           comments — maintainers may have left notes about whether
           this CVE is relevant to the package. If the Jira issue
           does not provide sufficient context about the vulnerability,
           search for more information about the CVE online.
        2. If upstream fix patches are available, read them to identify
           the specific files and functions modified by the fix.
        3. Search for those files/functions in the package source at
           {sources_rel}. Do NOT look at any other copy of the source.
        4. If the vulnerable code is not present, determine why — older
           version that predates the vulnerability? Patched downstream?
        5. For dependency rebuilds: verify whether the package uses
           the specific affected API/module of the dependency. Check
           direct imports, linked libraries, and build dependencies.
           Remember: transitive dependencies and build-time usage
           also count — a package that vendors or bundles the
           dependency is affected even without a direct import.

        Classify using Red Hat justification categories:
        - "Component not Present" — the affected component/subcomponent
          is not included in this package build
        - "Vulnerable Code not Present" — the package includes the
          component but the specific vulnerable code was introduced in
          a later version or is patched/removed downstream
        - "Vulnerable Code not in Execute Path" — the vulnerable code
          exists but is not reachable in normal execution (unused import,
          dead code, dependency API not called by this package)
        - "Vulnerable Code cannot be Controlled by Adversary" — the
          vulnerable code is present and reachable, but the input that
          triggers the vulnerability cannot be supplied by an attacker
        - "Inline Mitigations already Exist" — additional hardening or
          security measures exist that prevent exploitation

        If affected or cannot determine with confidence, classify as
        "Inconclusive". Be conservative: default to "Inconclusive"
        when unsure.
    """)
