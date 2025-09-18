from enum import Enum
from string import Template

BRANCH_PREFIX = "automated-package-update"

AGENT_WARNING = (
    "Warning: This is an AI-Generated contribution and may contain mistakes. "
    "Please carefully review the contributions made by AI agents.\n"
    "You can learn more about the Jotnar Pilot at https://docs.google.com/document/d/1mXTymiIe7MfjEDq6s4x0s3XnriC9db11DokdgZ5g9KU/edit"
)

JIRA_COMMENT_TEMPLATE = Template(f"""Output from $AGENT_TYPE Agent: \n\n$JIRA_COMMENT\n\n{AGENT_WARNING}""")

I_AM_JOTNAR = "by Jotnar, a Red Hat Enterprise Linux software maintenance AI agent."
CAREFULLY_REVIEW_CHANGES = "Carefully review the changes and make sure they are correct."
