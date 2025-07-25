#!/usr/bin/env python3
"""
Custom ADK agent for CentOS package workflow orchestration.
Implements the full workflow: issue analysis → decision → backport/rebase actions.
"""
import logging
import re
import os
from typing import AsyncGenerator, Optional
from jira import JIRA

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from issue_analyzer import create_issue_analyzer_agent, mcp_connection
from package_updater import create_package_updater_agent
from backport_agent import create_backport_agent
from constants import (
    JIRA_COMMENT_REBASE_TEMPLATE,
    JIRA_COMMENT_BACKPORT_TEMPLATE,
    JIRA_COMMENT_OTHER_TEMPLATE,
    JIRA_COMMENT_FAILURE_TEMPLATE,
    DEFAULT_VALUES
)

logger = logging.getLogger(__name__)


class CentOSPackageWorkflowAgent(BaseAgent):
    """Custom agent that orchestrates the full CentOS package workflow."""

    def __init__(self, jira_issue: str):
        super().__init__(
            name="centos_package_workflow",
            description="Orchestrates CentOS package workflow: issue analysis → decision → action"
        )
        self._jira_issue = jira_issue

    @property
    def jira_issue(self) -> str:
        """Get the JIRA issue key."""
        return self._jira_issue

    def _create_event(self, message: str) -> Event:
        """Helper to create standardized events."""
        return Event(
            content=types.Content(
                role="assistant",
                parts=[types.Part(text=message)]
            ),
            author=self.name
        )

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        """Main workflow orchestration logic."""
        logger.info(f"[{self.name}] Starting CentOS package workflow for {self.jira_issue}")

        # Store the JIRA issue in session state
        ctx.session.state["jira_issue"] = self.jira_issue
        os.environ['JIRA_ISSUE'] = self.jira_issue

        try:
            # Stage 1: Issue Analysis
            async for event in self._run_issue_analysis_stage(ctx):
                yield event

            # Stage 2: Decision Processing
            decision_info = self._parse_analysis_decision(ctx)
            logger.info(f"[{self.name}] Decision parsed: {decision_info}")

            # Stage 3: Execute action based on decision
            if decision_info.get('decision') in ['rebase', 'backport']:
                async for event in self._run_action_stage(ctx, decision_info):
                    yield event
            else:
                async for event in self._handle_no_action_decision(ctx, decision_info):
                    yield event

            # Stage 4: Report results to JIRA (commented out for now)
            # success = await self._report_to_jira(decision_info)
            # if success:
            #     logger.info(f"[{self.name}] Successfully reported workflow results to JIRA")
            # else:
            #     logger.warning(f"[{self.name}] Failed to report workflow results to JIRA")

        except Exception as e:
            logger.error(f"[{self.name}] Workflow failed: {e}")
            # TODO how do we get to know this? Sentry?
            raise

    async def _run_issue_analysis_stage(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        """Stage 1: Run issue analysis with MCP tools."""
        logger.info(f"[{self.name}] Running issue analysis stage...")

        # Use MCP connection for issue analyzer
        async with mcp_connection() as mcp_tools:
            logger.info(f"[{self.name}] MCP connection established for issue analysis")

            # Create issue analyzer with MCP tools
            issue_analyzer = create_issue_analyzer_agent(mcp_tools=mcp_tools)

            # Run issue analyzer and yield all events
            async for event in issue_analyzer.run_async(ctx):
                # Log events for debugging
                if event.is_final_response():
                    logger.info(f"[{self.name}] Issue analysis completed")
                    # Store analysis result in session state
                    if event.content and event.content.parts:
                        ctx.session.state["analysis_result"] = event.content.parts[0].text
                yield event

        logger.info(f"[{self.name}] Issue analysis stage completed")

    def _parse_analysis_decision(self, ctx: InvocationContext) -> dict:
        """Parse decision info from analysis results stored in session state."""
        analysis_text = ctx.session.state.get("analysis_result", "")
        logger.info(f"[{self.name}] Parsing decision from analysis text")

        decision_info = {'decision': 'unknown'}

        # Combined pattern for all fields including decision
        patterns = {
            'decision': r'DECISION:\s*(rebase|backport|clarification-needed|no-action|error)',
            'package': r'PACKAGE:\s*([^\n\r]+)',
            'version': r'VERSION:\s*([^\n\r]+)',
            'branch': r'BRANCH:\s*([^\n\r]+)',
            'patch_url': r'PATCH_URL:\s*([^\n\r]+)',
            'justification': r'JUSTIFICATION:\s*([^\n\r]+)',
            'findings': r'FINDINGS:\s*([^\n\r]+)',
            'additional_info_needed': r'ADDITIONAL_INFO_NEEDED:\s*([^\n\r]+)',
            'reasoning': r'REASONING:\s*([^\n\r]+)'
        }

        for field, pattern in patterns.items():
            match = re.search(pattern, analysis_text, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                decision_info[field] = value.lower() if field == 'decision' else value

        # Store decision info in session state for later stages
        ctx.session.state["decision_info"] = decision_info
        return decision_info

    async def _run_action_stage(self, ctx: InvocationContext, decision_info: dict) -> AsyncGenerator[Event, None]:
        """Stage 3: Execute package update actions based on decision."""
        decision = decision_info.get('decision')
        package = decision_info.get('package')

        logger.info(f"[{self.name}] Running action stage for {decision} on package {package}")

        if decision == 'rebase':
            async for event in self._handle_rebase_action(ctx, decision_info):
                yield event
        elif decision == 'backport':
            async for event in self._handle_backport_action(ctx, decision_info):
                yield event

    async def _handle_rebase_action(self, ctx: InvocationContext, decision_info: dict) -> AsyncGenerator[Event, None]:
        """Handle rebase action using package updater."""
        logger.info(f"[{self.name}] Handling rebase action")

        package = decision_info.get('package')
        version = decision_info.get('version')
        branch = decision_info.get('branch')

        if not package:
            logger.error(f"[{self.name}] Package name not found in decision info")
            return

        # Set environment variables for package updater
        env_vars = {'PACKAGE_NAME': package}
        if version:
            env_vars['TARGET_VERSION'] = version
        if branch:
            env_vars['TARGET_BRANCH'] = branch
        os.environ.update(env_vars)

        # Create and run package updater
        package_updater = create_package_updater_agent()
        logger.info(f"[{self.name}] Running package updater for {package}")

        async for event in package_updater.run_async(ctx):
            if event.is_final_response():
                logger.info(f"[{self.name}] Package update completed")
                # Store result in session state
                if event.content and event.content.parts:
                    ctx.session.state["package_update_result"] = event.content.parts[0].text
            yield event

    async def _handle_backport_action(self, ctx: InvocationContext, decision_info: dict) -> AsyncGenerator[Event, None]:
        """Handle backport action using backport agent."""
        logger.info(f"[{self.name}] Handling backport action")

        package = decision_info.get('package')
        patch_url = decision_info.get('patch_url')
        branch = decision_info.get('branch')
        justification = decision_info.get('justification')

        if not package or not patch_url:
            logger.error(f"[{self.name}] Package name or patch URL not found in decision info")
            return

        # Set environment variables for backport agent
        env_vars = {
            'PACKAGE_NAME': package,
            'PATCH_URL': patch_url,
            'UPSTREAM_FIX': patch_url
        }
        if branch:
            env_vars['DIST_GIT_BRANCH'] = branch

        os.environ.update(env_vars)

        # Create and run backport agent
        backport_agent = create_backport_agent()
        logger.info(f"[{self.name}] Running backport agent for {package}")

        async for event in backport_agent.run_async(ctx):
            if event.is_final_response():
                logger.info(f"[{self.name}] Backport completed")
                # Store result in session state
                if event.content and event.content.parts:
                    ctx.session.state["backport_result"] = event.content.parts[0].text
            yield event

    async def _handle_no_action_decision(self, ctx: InvocationContext, decision_info: dict) -> AsyncGenerator[Event, None]:
        """Handle no-action or clarification-needed decisions."""
        decision = decision_info.get('decision')
        logger.info(f"[{self.name}] Handling {decision} decision")

        if decision == 'clarification-needed':
            additional_info = decision_info.get('additional_info_needed', 'No specific info mentioned')
            message = f"Clarification needed for JIRA issue {self.jira_issue}.\nAdditional info needed: {additional_info}"
        elif decision == 'no-action':
            reasoning = decision_info.get('reasoning', 'No reasoning provided')
            message = f"No action required for JIRA issue {self.jira_issue}.\nReasoning: {reasoning}"
        else:
            message = f"Unknown decision '{decision}' for JIRA issue {self.jira_issue}"

        logger.info(f"[{self.name}] {message}")

        # Store decision info in session state
        ctx.session.state["final_decision"] = message

        # Create and yield response event
        yield self._create_event(message)

    def _get_jira_client(self):
        """Get JIRA client instance."""
        server = os.getenv('JIRA_BASE_URL', 'https://issues.redhat.com')
        api_token = os.getenv('JIRA_API_TOKEN')
        auth_user = os.getenv('JIRA_EMAIL') or os.getenv('JIRA_USERNAME')

        if not api_token or not auth_user:
            raise ValueError("Missing JIRA credentials. Need JIRA_API_TOKEN and JIRA_EMAIL/JIRA_USERNAME")

        return JIRA(server=server, basic_auth=(auth_user, api_token))

    async def _add_jira_comment(self, comment_text: str) -> bool:
        """Add a comment to JIRA issue using the jira library."""
        # TODO handle avoiding comment duplication
        try:
            jira_client = self._get_jira_client()

            logger.info(f"[{self.name}] Adding comment to JIRA issue {self.jira_issue}")

            # Add comment to the issue
            jira_client.add_comment(self.jira_issue, comment_text)

            logger.info(f"[{self.name}] Successfully added comment to JIRA issue {self.jira_issue}")
            return True

        except Exception as e:
            logger.error(f"[{self.name}] Failed to add JIRA comment: {e}")
            return False

    async def _report_to_jira(self, decision_info: dict) -> bool:
        """Report workflow results to JIRA issue (simplified)."""
        decision = decision_info.get('decision')

        # Prepare template values with defaults
        template_values = DEFAULT_VALUES.copy()
        template_values.update(decision_info)

        # Create comment based on decision type
        if decision == 'rebase':
            comment = JIRA_COMMENT_REBASE_TEMPLATE.format(**template_values)
        elif decision == 'backport':
            comment = JIRA_COMMENT_BACKPORT_TEMPLATE.format(**template_values)
        else:
            template_values['decision'] = decision.title() if decision else 'Unknown'
            comment = JIRA_COMMENT_OTHER_TEMPLATE.format(**template_values)

        # Add comment to JIRA
        return await self._add_jira_comment(comment)


def create_centos_workflow_agent(jira_issue: str) -> CentOSPackageWorkflowAgent:
    """Factory function to create CentOS workflow agent."""
    return CentOSPackageWorkflowAgent(jira_issue=jira_issue)
