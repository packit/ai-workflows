import asyncio
import os
import sys
import traceback
from typing import Optional

from pydantic import BaseModel, Field

from beeai_framework.agents.experimental.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool

from base_agent import BaseAgent, TInputSchema, TOutputSchema
from gemini import GeminiChatModel
from observability import setup_observability
from tools import ShellCommandTool
from triage_agent import RebaseData, ErrorData
from utils import redis_client


class InputSchema(BaseModel):
    package: str = Field(description="Package to update")
    version: str = Field(description="Version to update to")
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    dist_git_branch: str = Field(description="Git branch in dist-git to be updated")
    gitlab_user: str = Field(
        description="Name of the GitLab user",
        default=os.getenv("GITLAB_USER", "rhel-packaging-agent"),
    )
    git_url: str = Field(
        description="URL of the git repository",
        default="https://gitlab.com/redhat/centos-stream/rpms",
    )
    git_user: str = Field(description="Name of the git user", default="RHEL Packaging Agent")
    git_email: str = Field(
        description="E-mail address of the git user", default="rhel-packaging-agent@redhat.com"
    )


class OutputSchema(BaseModel):
    success: bool = Field(description="Whether the rebase was successfully completed")
    status: str = Field(description="Rebase status")
    mr_url: Optional[str] = Field(description="URL to the opened merge request")
    error: Optional[str] = Field(description="Specific details about an error")


class RebaseAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(
            llm=GeminiChatModel(),
            tools=[ThinkTool(), ShellCommandTool(), DuckDuckGoSearchTool()],
            memory=UnconstrainedMemory(),
            requirements=[
                ConditionalRequirement(ThinkTool, force_after=Tool, consecutive_allowed=False),
            ],
            middlewares=[GlobalTrajectoryMiddleware()],
        )

    @property
    def input_schema(self) -> type[TInputSchema]:
        return InputSchema

    @property
    def output_schema(self) -> type[TOutputSchema]:
        return OutputSchema

    @property
    def prompt(self) -> str:
        return """
          You are an AI Agent tasked to rebase a CentOS package to a newer version following the exact workflow.

          A couple of rules that you must follow and useful information for you:
          * All packages are in separate Git repositories under the Gitlab project {{ git_url }}
          * You can find the package at {{ git_url }}/{{ package }}
          * The Git user name is {{ git_user }}
          * The Git user's email address is {{ git_email }}
          * Use {{ gitlab_user }} as the GitLab user.
          * Work only in a temporary directory that you can create with the mktemp tool.
          * To create forks and open merge requests, always use GitLab's `glab` CLI tool.
          * You can find packaging guidelines at https://docs.fedoraproject.org/en-US/packaging-guidelines/
          * You can find the RPM packaging guide at https://rpm-packaging-guide.github.io/.
          * Do not run the `centpkg new-sources` command for now (testing purposes), just write down the commands you would run.

          IMPORTANT GUIDELINES:
          - **Tool Usage**: You have ShellCommand tool available - use it directly!
          - **Command Execution Rules**:
            - Use ShellCommand tool for ALL command execution
            - If a command shows "no output" or empty STDOUT, that is a VALID result - do not retry
            - Commands that succeed with no output are normal - report success
          - **Git Configuration**: Always configure git user name and email before any git operations

          Follow exactly these steps:

          1. Find the location of the {{ package }} package at {{ git_url }}.  Always use the {{ dist_git_branch }} branch.

          2. Check if the {{ package }} was not already updated to version {{ version }}.  That means comparing
             the current version and provided version.
              * The current version of the package can be found in the 'Version' field of the RPM .spec file.
              * If there is nothing to update, print a message and exit. Otherwise follow the instructions below.
              * Do not clone any repository for detecting the version in .spec file.

          3. Create a local Git repository by following these steps:
              * Check if the fork already exists for {{ gitlab_user }} as {{ gitlab_user }}/{{ package }} and if not,
                create a fork of the {{ package }} package using the glab tool.
              * Clone the fork using git and HTTPS into the temp directory.

          4. Update the {{ package }} to the newer version:
              * Create a new Git branch named `automated-package-update-{{ version }}`.
              * Update the local package by:
                * Updating the 'Version' and 'Release' fields in the .spec file as needed (or corresponding macros),
                  following packaging documentation.
                  * Make sure the format of the .spec file remains the same.
                * Updating macros related to update (e.g., 'commit') if present and necessary; examine the file's history
                  to see how updates are typically done.
                  * You might need to check some information in upstream repository, e.g. the commit SHA of the new version.
                * Creating a changelog entry, referencing the Jira issue as "Resolves: {{ jira_issue }}".
                * Downloading sources using `spectool -g -S {{ package }}.spec` (you might need to copy local sources,
                  e.g. if the .spec file loads some macros from them, to a directory where spectool expects them).
                * Uploading the new sources using `centpkg --release {{ dist_git_branch }} new-sources`.
                * IMPORTANT: Only performing changes relevant to the version update: Do not rename variables,
                  comment out existing lines, or alter if-else branches in the .spec file.

          5. Verify and adjust the changes:
              * Use `rpmlint` to validate your .spec file changes and fix any new errors it identifies.
              * Generate the SRPM using `rpmbuild -bs` (ensure your .spec file and source files are correctly
                copied to the build environment as required by the command).

          6. Commit the changes:
              * The title of the Git commit should be in the format "[DO NOT MERGE: AI EXPERIMENTS] Update to version {{ version }}"
              * Include the reference to Jira as "Resolves: <jira_issue>" for each issue in {{ jira_issues }}.
              * Commit just the specfile change.

          7. Open a merge request:
            * Authenticate using `glab`
            * Push the commit to the fork.
            * Open a merge request against the upstream repository of the {{ package }} in {{ git_url }}
              with previously created commit.

          Report the status of the rebase operation including:
          - Whether the package was already up to date
          - Any errors encountered during the process
          - The URL of the created merge request if successful
          - Any validation issues found with rpmlint
        """


async def main() -> None:
    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))
    agent = RebaseAgent()

    if (
        (package := os.getenv("PACKAGE", None))
        and (version := os.getenv("VERSION", None))
        and (jira_issue := os.getenv("JIRA_ISSUE", None))
        and (branch := os.getenv("BRANCH", None))
    ):
        input = InputSchema(
            package=package, version=version, jira_issue=jira_issue, dist_git_branch=branch
        )
        output = await agent.run_with_schema(input)
        print(output.model_dump_json(indent=4))
        return

    class Task(BaseModel):
        metadata: dict = Field(description="Task metadata")
        attempts: int = Field(default=0, description="Number of processing attempts")

    async with redis_client(os.getenv("REDIS_URL")) as redis:
        max_retries = int(os.getenv("MAX_RETRIES", 3))
        while True:
            element = await redis.brpop("rebase_queue", timeout=30)
            if element is None:
                continue
            _, payload = element
            task = Task.model_validate_json(payload)
            rebase_data = RebaseData.model_validate(task.metadata)
            input = InputSchema(
                package=rebase_data.package,
                version=rebase_data.version,
                jira_issue=rebase_data.jira_issue,
                dist_git_branch=rebase_data.branch,
            )

            async def retry(task, error):
                task.attempts += 1
                if task.attempts < max_retries:
                    await redis.lpush("rebase_queue", task.model_dump_json())
                else:
                    await redis.lpush("error_list", error)

            try:
                output = await agent.run_with_schema(input)
            except Exception as e:
                error = "".join(traceback.format_exception(e))
                print(error, file=sys.stderr)
                await retry(
                    task, ErrorData(details=error, jira_issue=input.jira_issue).model_dump_json()
                )
            else:
                if output.success:
                    await redis.lpush("completed_rebase_list", output.model_dump_json())
                else:
                    await retry(task, output.error)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
