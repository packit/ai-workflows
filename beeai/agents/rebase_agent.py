import asyncio
import os
import sys
import traceback
from typing import Any

from pydantic import BaseModel, Field

from beeai_framework.agents.experimental import RequirementAgent
from beeai_framework.agents.experimental.requirements.conditional import (
    ConditionalRequirement,
)
from beeai_framework.errors import FrameworkError
from beeai_framework.memory import UnconstrainedMemory
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware
from beeai_framework.template import PromptTemplate, PromptTemplateInput
from beeai_framework.tools import Tool
from beeai_framework.tools.search.duckduckgo import DuckDuckGoSearchTool
from beeai_framework.tools.think import ThinkTool

from gemini import GeminiChatModel
from models import RebaseTask
from observability import setup_observability
from tools import ShellCommandTool
from utils import redis_client


class PromptSchema(BaseModel):
    package: str = Field(description="Package to update")
    version: str = Field(description="Version to update to")
    jira_issue: str = Field(description="Jira issue to reference as resolved")
    dist_git_branch: str = Field(description="Git branch in dist-git to be updated")
    gitlab_user: str = Field(description="Name of the GitLab user")
    git_url: str = Field(description="URL of the git repository")
    git_user: str = Field(description="Name of the git user")
    git_email: str = Field(description="E-mail address of the git user")


prompt_template: PromptTemplate[PromptSchema] = PromptTemplate(
    PromptTemplateInput(
        schema=PromptSchema,
        template="""
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
        """,
    )
)


async def main() -> None:
    agent = RequirementAgent(
        llm=GeminiChatModel(),
        tools=[ThinkTool(), ShellCommandTool(), DuckDuckGoSearchTool()],
        memory=UnconstrainedMemory(),
        requirements=[
            ConditionalRequirement(ThinkTool, force_after=Tool, consecutive_allowed=False),
        ],
        middlewares=[GlobalTrajectoryMiddleware()],
    )
    max_retries = int(os.getenv("REBASE_MAX_RETRIES", "3"))
    async with redis_client(os.getenv("REDIS_URL")) as redis:
        while True:
            task = await redis.brpop("rebase_queue", timeout=30)
            if task is None:
                continue
            _, data = task
            rebase_task = RebaseTask.model_validate_json(data)
            try:
                response = await agent.run(
                    prompt=prompt_template.render(
                        package=rebase_task.package_name,
                        version=rebase_task.package_version,
                        jira_issue=rebase_task.jira_issue,
                        dist_git_branch=rebase_task.git_branch,
                        gitlab_user=os.getenv("GITLAB_USER", "rhel-packaging-agent"),
                        git_url="https://gitlab.com/redhat/centos-stream/rpms",
                        git_user="RHEL Packaging Agent",
                        git_email="rhel-packaging-agent@redhat.com",
                    ),
                )
                print(response.result.text)
                # TODO: check for other types of errors
            except Exception:
                traceback.print_exc()
                rebase_task.attempts += 1
                if rebase_task.attempts < max_retries:
                    await redis.lpush("rebase_queue", rebase_task.model_dump_json())
                else:
                    raise


if __name__ == "__main__":
    try:
        setup_observability(os.getenv("COLLECTOR_ENDPOINT"))
        asyncio.run(main())
    except FrameworkError as e:
        traceback.print_exc()
        sys.exit(e.explain())
