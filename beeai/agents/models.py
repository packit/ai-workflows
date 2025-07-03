from pydantic import BaseModel, Field


class RebaseTask(BaseModel):
    package_name: str = Field(description="Package to update")
    package_version: str = Field(description="Version to update to")
    git_branch: str = Field(description="Git branch in dist-git to be updated")
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    attempts: int = Field(default=0, description="Number of processing attempts")
