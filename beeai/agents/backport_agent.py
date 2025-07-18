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
from triage_agent import BackportData, ErrorData
from utils import redis_client


class InputSchema(BaseModel):
    package: str = Field(description="Package to update")
    upstream_fix: str = Field(description="Link to an upstream fix for the issue")
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
    success: bool = Field(description="Whether the backport was successfully completed")
    status: str = Field(description="Backport status")
    mr_url: Optional[str] = Field(description="URL to the opened merge request")
    error: Optional[str] = Field(description="Specific details about an error")


class BackportAgent(BaseAgent):
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
          You are an agent for backporting a fix for a CentOS Stream package. You will prepare the content
          of the update and then create a commit with the changes. Create a temporary directory and always work
          inside it. Follow exactly these steps:

          1. Find the location of the {{ package }} package at {{ git_url }}. Always use the {{ dist_git_branch }} branch.

          2. Check if the package {{ package }} already has the fix {{ jira_issue }} applied.

          3. Create a local Git repository by following these steps:
            * Check if the fork already exists for {{ gitlab_user }} as {{ gitlab_user }}/{{ package }} and if not,
              create a fork of the {{ package }} package using the glab tool.
            * Clone the fork using git and HTTPS into the temp directory.
            * Run command `centpkg sources` in the cloned repository which downloads all sources defined in the RPM specfile.
            * Create a new Git branch named `automated-package-update-{{ jira_issue }}`.

          4. Update the {{ package }} with the fix:
            * Updating the 'Release' field in the .spec file as needed (or corresponding macros), following packaging
              documentation.
              * Make sure the format of the .spec file remains the same.
            * Fetch the upstream fix {{ upstream_fix }} locally and store it in the git repo as "{{ jira_issue }}.patch".
              * Add a new "Patch:" entry in the spec file for patch "{{ jira_issue }}.patch".
              * Verify that the patch is being applied in the "%prep" section.
            * Creating a changelog entry, referencing the Jira issue as "Resolves: <jira_issue>" for the issue {{ jira_issue }}.
              The changelog entry has to use the current date.
            * IMPORTANT: Only performing changes relevant to the backport update: Do not rename variables,
              comment out existing lines, or alter if-else branches in the .spec file.

          5. Verify and adjust the changes:
            * Use `rpmlint` to validate your .spec file changes and fix any new errors it identifies.
            * Generate the SRPM using `rpmbuild -bs` (ensure your .spec file and source files are correctly copied
              to the build environment as required by the command).
            * Verify the newly added patch applies cleanly using the command `centpkg prep`.

          6. Commit the changes:
            * The title of the Git commit should be in the format "[DO NOT MERGE: AI EXPERIMENTS] backport {{ jira_issue }}"
            * Include the reference to Jira as "Resolves: <jira_issue>" for the issue {{ jira_issue }}.
            * Commit the RPM spec file change and the newly added patch file.
            * Push the commit to the fork.

           7. Open a merge request:
             * Authenticate using `glab`
             * Open a merge request against the upstream repository of the {{ package }} in {{ git_url }}
               with previously created commit.
        """


async def main() -> None:
    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))
    agent = BackportAgent()

    if (
        (package := os.getenv("PACKAGE", None))
        and (upstream_fix := os.getenv("UPSTREAM_FIX", None))
        and (jira_issue := os.getenv("JIRA_ISSUE", None))
        and (branch := os.getenv("BRANCH", None))
    ):
        input = InputSchema(
            package=package,
            upstream_fix=upstream_fix,
            jira_issue=jira_issue,
            dist_git_branch=branch,
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
            element = await redis.brpop("backport_queue", timeout=30)
            if element is None:
                continue
            _, payload = element
            task = Task.model_validate_json(payload)
            backport_data = BackportData.model_validate(task.metadata)
            input = InputSchema(
                package=backport_data.package,
                upstream_fix=backport_data.patch_url,
                jira_issue=backport_data.jira_issue,
                dist_git_branch=backport_data.branch,
            )

            async def retry(task, error):
                task.attempts += 1
                if task.attempts < max_retries:
                    await redis.lpush("backport_queue", task.model_dump_json())
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
                    await redis.lpush("completed_backport_list", output.model_dump_json())
                else:
                    await retry(task, output.error)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
