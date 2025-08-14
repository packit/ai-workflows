from string import Template

COMMIT_PREFIX = "[DO NOT MERGE: AI EXPERIMENTS]"

BRANCH_PREFIX = "automated-package-update"

AGENT_WARNING = "Warning: This is an AI-Generated contribution and may contain mistakes. Please carefully review the contributions made by AI agents."

JIRA_COMMENT_TEMPLATE = Template(f"""Output from $AGENT_TYPE Agent: $JIRA_COMMENT\n\n{AGENT_WARNING}""")
