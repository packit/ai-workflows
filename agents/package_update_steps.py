import re
import logging
import tasks
from pydantic import BaseModel, Field
from pathlib import Path

from common.constants import JiraLabels
from common.config import load_rhel_config
from common.models import LogOutputSchema

logger = logging.getLogger(__name__)

class PackageUpdateState(BaseModel):
        jira_issue: str
        package: str
        dist_git_branch: str
        local_clone: Path | None = Field(default=None)
        update_branch: str | None = Field(default=None)
        fork_url: str | None = Field(default=None)
        build_error: str | None = Field(default=None)
        log_result: LogOutputSchema | None = Field(default=None)
        merge_request_url: str | None = Field(default=None)

class PackageUpdateStep():
  """
  Steps for package update operations (backport and rebase steps).

  A place where to share common steps between backport and rebase workflows.
  """

  @staticmethod
  async def add_fusa_label(state, next_step, dry_run, gateway_tools):
      """Add FuSa label to Jira and GitLab merge request for FuSa packages on FuSa branches.

      Args:
          state: The state of the workflow.
          next_step: The next step to run.
          dry_run: Whether to run the workflow in dry-run mode.
          gateway_tools: The gateway tools to use.

      Returns:
          The next step to run.
      """
      target_branch = state.dist_git_branch

      config = await load_rhel_config()
      fusa_packages = config.get("fusa_packages", [])

      is_fusa_package = state.package in fusa_packages
      if not is_fusa_package:
          logger.info(f"Skipping FuSa label for non-FuSa package: {state.package}")
          return next_step

      # Only add FuSa labels for c9s or rhel9-[1-10] branches
      is_fusa_branch = (target_branch == "c9s" or
                      re.match(r"^rhel-9\.([0-9]|10)\.0$", target_branch))
      if not is_fusa_branch:
          logger.info(f"Skipping FuSa label for non-FuSa branch: {target_branch}")
          return next_step

      if dry_run:
          logger.info(f"Skipping FuSa label for FuSa package: {state.package}, FuSa branch: {target_branch}, because running in dry-run mode")
          return next_step

      # Add FuSa label to Jira
      await tasks.set_jira_labels(
          jira_issue=state.jira_issue,
          labels_to_add=[JiraLabels.FUSA.value],
          dry_run=dry_run
      )

      # Add FuSa label to GitLab merge request if it exists
      if state.merge_request_url:
          try:
              await tasks.run_tool(
                  "add_merge_request_labels",
                  merge_request_url=state.merge_request_url,
                  labels=[JiraLabels.FUSA.value],
                  available_tools=gateway_tools,
              )
          except Exception as e:
              logger.warning(f"Failed to add FuSa label to GitLab MR: {e}")

      return next_step

  @staticmethod
  async def add_blocking_comment(state, next_step, dry_run, gateway_tools):
      """Add a blocking comment to the MR to prevent merge until Jotnar members approve.

      Args:
          state: The state of the workflow.
          next_step: The next step to run.
          dry_run: Whether to run the workflow in dry-run mode.
          gateway_tools: The gateway tools to use.

      Returns:
          The next step to run.
      """
      if dry_run:
          return next_step

      if state.merge_request_url:
        blocking_comment = (
            "**⚠️ Blocking Merge Request ⚠️**\n\n"
            "This MR should only be merged by Jotnar team members. Feel free to approve and review the changes.\n\n"
            "**Reason**: There are automated processes that run after merge, and this MR "
            "may need to wait before being merged to avoid conflicts with ongoing automation.\n\n"
            "Please wait for approval from a Jotnar member before merging."
        )

        try:
            await tasks.run_tool(
                "add_blocking_merge_request_comment",
                merge_request_url=state.merge_request_url,
                comment=blocking_comment,
                available_tools=gateway_tools,
            )
            logger.info(f"Added blocking comment to MR {state.merge_request_url}")
        except Exception as e:
            logger.warning(f"Failed to add blocking comment to MR: {e}")
            # Don't fail the workflow if comment addition fails

      return next_step
