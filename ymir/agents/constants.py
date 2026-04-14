from enum import Enum
from string import Template

BRANCH_PREFIX = "automated-package-update"

AGENT_WARNING = (
    "Warning: This is an AI-Generated contribution and may contain mistakes. "
    "Please carefully review the contributions made by AI agents.\n"
    "You can learn more about the Ymir project at https://docs.google.com/document/d/1zKeJQtIlGkgQ7QoEVFxz4dLVEjqB74_E3tW0_wCo6YM/edit?usp=sharing"
)

JIRA_COMMENT_TEMPLATE = Template(f"""Output from Ymir $AGENT_TYPE Agent: \n\n$JIRA_COMMENT\n\n{AGENT_WARNING}""")

I_AM_YMIR = "by Ymir, a Red Hat Enterprise Linux software maintenance AI agent."
CAREFULLY_REVIEW_CHANGES = "Carefully review the changes and make sure they are correct."
