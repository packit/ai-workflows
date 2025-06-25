from google.adk.agents import SequentialAgent
from .sub_agents.agent import (
    version_checker_agent,
    package_updater_agent
)

# Create the sequential workflow agent
root_agent = SequentialAgent(
    name="centos_package_workflow",
    description="A sequential workflow for CentOS package updating: version check → package update",
    sub_agents=[
        version_checker_agent,
        package_updater_agent
    ]
)
