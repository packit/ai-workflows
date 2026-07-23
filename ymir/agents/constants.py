import re
from string import Template

BRANCH_PREFIX = "automated-package-update"
ZSTREAM_TARGET_LABEL = "target::zstream"

JIRA_BROWSE_URL = "https://issues.redhat.com/browse/{issue}"

AGENT_WARNING = (
    "Warning: This is an AI-Generated contribution and may contain mistakes. "
    "Please carefully review the contributions made by AI agents.\n"
    "You can learn more about the Ymir project at https://ymir.pages.redhat.com/\n\n"
    "💬 *Have suggestions or complaints?* "
    "Please reach out to us on the [Slack forum #forum-ymir-package-automation|"
    "https://redhat.enterprise.slack.com/archives/C095699FLMR] "
    "where your feedback will be more visible than pinging us on individual issues."
)

JIRA_COMMENT_TEMPLATE = Template(
    f"""Output from Ymir $AGENT_TYPE Agent: \n\n$JIRA_COMMENT\n\n{AGENT_WARNING}"""
)

I_AM_YMIR = "by Ymir, a Red Hat Enterprise Linux software maintenance AI agent."


def format_jira_links_for_mr(issues: str | list[str] | None) -> str:
    """Format Jira issue key(s) as browse links for an MR description.

    Prefer this over ``Resolves:`` in MR bodies so ``check_tickets`` is not
    tripped. Commit messages should still use ``Resolves:``.
    """
    if not issues:
        return ""
    keys = [issues] if isinstance(issues, str) else [k for k in issues if k]
    if not keys:
        return ""
    if len(keys) == 1:
        url = JIRA_BROWSE_URL.format(issue=keys[0])
        return f"Jira: [{keys[0]}]({url})\n"
    lines = ["### Resolved Jira Issues", ""]
    lines.extend(f"- [{k}]({JIRA_BROWSE_URL.format(issue=k)})" for k in keys)
    return "\n".join(lines) + "\n"


def strip_resolves_from_mr_text(text: str) -> str:
    """Remove ticket footer lines so they cannot leak into an MR body.

    Strips ``Resolves:``, ``Related:``, and ``Jira:`` lines (including the
    browse-link form and optional markdown list markers such as ``- Resolves:``).
    """
    if not text:
        return text
    filtered = [
        line
        for line in text.splitlines()
        if not re.match(r"^\s*([-*+]\s*)?(Resolves|Related|Jira)\s*:", line, re.IGNORECASE)
    ]
    # Collapse runs of blank lines left by removals
    result: list[str] = []
    prev_blank = False
    for line in filtered:
        blank = not line.strip()
        if blank and prev_blank:
            continue
        result.append(line)
        prev_blank = blank
    return "\n".join(result).strip("\n")


def mr_description_footer(package: str) -> str:
    return (
        "---\n"  # noqa: S608
        "\n"
        "> **⚠️ AI-Generated MR**: Created by Ymir AI assistant. AI may make mistakes, "
        "select incorrect patches, or miss dependencies. **Carefully review the changes. "
        "Human RHEL maintainer needs to approve this contribution before merging.**\n"
        ">\n"
        "> <ins>By merging this MR, you agree to follow "
        "the [Guidelines on Use of AI Generated Content]"
        "(https://source.redhat.com/departments/legal/legal_compliance_ethics/"
        "compliance_folder/appendix_1_to_policy_on_the_use_of_ai_technologypdf) "
        "and [Guidelines for Responsible Use of AI Code Assistants]"
        "(https://source.redhat.com/projects_and_programs/ai/wiki/"
        "code_assistants_guidelines_for_responsible_use_of_ai_code_assistants).</ins>\n"
        "\n"
        "## ✏️ Want to make changes to this MR?\n"
        "\n"
        "You can check out the source branch from the fork and push your changes directly.\n"
        "\n"
        "## 🔄 Retrigger Ymir\n"
        "\n"
        "If you'd like Ymir to run again on this issue (e.g. after fixing the rules or resolving "
        "a blocker), add the `ymir_todo` label to the Jira issue. "
        "See the [triggering docs](https://ymir.pages.redhat.com/docs/triggering/) for details.\n"
        "\n"
        "## 🔧 Customize Ymir's behavior for your package\n"
        "\n"
        "If there is anything that could be adjusted regarding Ymir's behavior "
        "and is specific to your package, you can submit an MR to "
        f"[gitlab.com/redhat/centos-stream/rules/{package}]"
        f"(https://gitlab.com/redhat/centos-stream/rules/{package}). "
        "See the [customization docs](https://ymir.pages.redhat.com/docs/customizations/) "
        "for details.\n"
        "\n"
        "## 📞 Questions or Issues?\n"
        "\n"
        "**Contact:** redhat-ymir-agent@redhat.com | "
        "**Slack Forum:** [#forum-ymir-package-automation]"
        "(https://redhat.enterprise.slack.com/archives/C095699FLMR) | "
        "**Report AI Issues:** [Jira](https://issues.redhat.com/) "
        "(project: Packit, component: jotnar) "
        "or [GitHub](https://github.com/packit/ai-workflows/issues)\n"
        "\n"
        "### 💡 Feedback Welcome\n"
        "\n"
        "If you have suggestions or complaints about the quality of this MR, "
        "please reach out to us on the [Slack forum]"
        "(https://redhat.enterprise.slack.com/archives/C095699FLMR) "
        "where your feedback will be more visible than pinging us on individual issues. "
        "Your feedback helps us continuously improve Ymir's capabilities and "
        "deliver better results.\n"
    )
