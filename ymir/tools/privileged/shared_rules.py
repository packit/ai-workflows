import json
import logging
import os
import time
from urllib.parse import quote

import aiohttp
import yaml
from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolError, ToolRunOptions
from pydantic import BaseModel, Field

from ymir.tools.constants import AIOHTTP_TIMEOUT, GITLAB_API_URL, RULES_NAMESPACE, YMIR_USER_AGENT
from ymir.tools.http import aiohttp_get_with_retries

logger = logging.getLogger(__name__)
SHARED_RULES_REPO = "shared-rules"
REGISTRY_FILE = "registry.yaml"
REGISTRY_TTL_SECONDS = 3600  # 1 hour


class SharedRulesInput(BaseModel):
    package: str = Field(
        description="Name of the CentOS Stream package to find applicable shared rule sets for"
    )


class SharedRulesTool(Tool[SharedRulesInput, ToolRunOptions, StringToolOutput]):
    name = "get_shared_rules"
    description = (
        "Look up which shared rule sets apply to a package. "
        'Returns a JSON list of shared rule set names (e.g. ["python", "autotools"]) '
        "from the central registry at gitlab.com/redhat/centos-stream/rules/shared-rules/registry.yaml. "
        'For each name returned, use get_maintainer_rules with package="shared-rules" and '
        'file_path="{name}/AGENTS.md" to fetch the actual shared rules. '
        "Returns an empty list if no shared rules apply or the registry does not exist."
    )
    input_schema = SharedRulesInput

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._registry_cache: dict[str, list[str]] | None = None
        self._registry_fetched_at: float = 0

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "rules", self.name],
            creator=self,
        )

    def _cache_and_return(self, registry: dict[str, list[str]] | None) -> dict[str, list[str]]:
        self._registry_cache = registry
        self._registry_fetched_at = time.monotonic()
        return registry or {}

    async def _fetch_registry(self) -> dict[str, list[str]]:
        if (
            self._registry_fetched_at
            and (time.monotonic() - self._registry_fetched_at) < REGISTRY_TTL_SECONDS
        ):
            return self._registry_cache or {}

        project_path = quote(f"{RULES_NAMESPACE}/{SHARED_RULES_REPO}", safe="")
        file_path = quote(REGISTRY_FILE, safe="")
        url = f"{GITLAB_API_URL}/projects/{project_path}/repository/files/{file_path}/raw?ref=main"

        headers: dict[str, str] = {"User-Agent": YMIR_USER_AGENT}
        if token := os.getenv("GITLAB_TOKEN"):
            headers["PRIVATE-TOKEN"] = token

        async with (
            aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session,
            aiohttp_get_with_retries(session, url, headers=headers) as response,
        ):
            if response.status == 404:
                logger.info("Shared rules registry not found (404)")
                return self._cache_and_return(None)

            if response.status != 200:
                text = await response.text()
                logger.warning(
                    "Failed to fetch shared rules registry (HTTP %d): %s",
                    response.status,
                    text,
                )
                return self._cache_and_return(None)

            raw = await response.text()

        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError:
            logger.warning("Malformed YAML in shared rules registry")
            return self._cache_and_return(None)

        if not isinstance(parsed, dict):
            logger.warning("Shared rules registry is not a YAML mapping")
            return self._cache_and_return(None)

        return self._cache_and_return(parsed)

    async def _run(
        self,
        tool_input: SharedRulesInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        try:
            registry = await self._fetch_registry()
        except TimeoutError as e:
            raise ToolError("Timeout while fetching shared rules registry") from e
        except Exception as e:
            raise ToolError(f"Error fetching shared rules registry: {e}") from e

        matching = [
            name
            for name, packages in registry.items()
            if isinstance(packages, list) and tool_input.package in packages
        ]

        return StringToolOutput(result=json.dumps(matching))
