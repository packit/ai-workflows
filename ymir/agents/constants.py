from string import Template

BRANCH_PREFIX = "automated-package-update"

AGENT_WARNING = (
    "Warning: This is an AI-Generated contribution and may contain mistakes. "
    "Please carefully review the contributions made by AI agents.\n"
    "You can learn more about the Ymir project "
    "at https://docs.google.com/document/d/1zKeJQtIlGkgQ7QoEVFxz4dLVEjqB74_E3tW0_wCo6YM/edit?usp=sharing"
)

JIRA_COMMENT_TEMPLATE = Template(
    f"""Output from Ymir $AGENT_TYPE Agent: \n\n$JIRA_COMMENT\n\n{AGENT_WARNING}"""
)

I_AM_YMIR = "by Ymir, a Red Hat Enterprise Linux software maintenance AI agent."

MR_DESCRIPTION_FOOTER = (
    "---\n"
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
    "## 📞 Questions or Issues?\n"
    "\n"
    "**Contact:** jotnar@redhat.com | **Slack:** #forum-jötnar-package-automation | "
    "**Report AI Issues:** [Jira](https://issues.redhat.com/) "
    "(project: Packit, component: jotnar) "
    "or [GitHub](https://github.com/packit/ai-workflows/issues)\n"
    "\n"
    "### 💡 Feedback Welcome\n"
    "\n"
    "If the quality of this MR does not meet your expectations or you have suggestions "
    "for improvement, please reach out to us. Your feedback helps us continuously "
    "improve Ymir's capabilities and deliver better results.\n"
)
