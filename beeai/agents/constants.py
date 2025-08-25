from string import Template

COMMIT_PREFIX = "[DO NOT MERGE: AI EXPERIMENTS]"

BRANCH_PREFIX = "automated-package-update"

AGENT_WARNING = "Warning: This is an AI-Generated contribution and may contain mistakes. Please carefully review the contributions made by AI agents."

JIRA_COMMENT_TEMPLATE = Template(f"""Output from $AGENT_TYPE Agent: \n\n$JIRA_COMMENT\n\n{AGENT_WARNING}""")

I_AM_JOTNAR = "by Jotnar, a Red Hat Enterprise Linux packaging AI agent."
CAREFULLY_REVIEW_CHANGES = "Carefully review the changes and make sure they are correct."
