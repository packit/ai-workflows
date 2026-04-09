import json
import logging
from urllib.parse import quote as urlquote

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolRunOptions

from ..gitlab_utils import GITLAB_URL, gitlab_headers
from ..http_utils import aiohttp_session

logger = logging.getLogger(__name__)


class FetchGitlabMrNotesInput(BaseModel):
    project: str = Field(
        description="GitLab project path (e.g. 'redhat/centos-stream/rpms/podman')"
    )
    mr_iid: int = Field(description="Merge request IID within the project")


class FetchGitlabMrNotesTool(
    Tool[FetchGitlabMrNotesInput, ToolRunOptions, StringToolOutput]
):
    """
    Tool to fetch comments/notes from a GitLab merge request.
    This is useful for finding OSCI test results posted as comments
    on merge requests with titles like "Results for pipeline ...".
    """

    name = "fetch_gitlab_mr_notes"  # type: ignore
    description = (  # type: ignore
        "Fetch comments/notes from a GitLab merge request. "
        "Returns JSON with a list of notes including author, body, and creation date. "
        "Use this to find OSCI test results posted as comments on merge requests."
    )
    input_schema = FetchGitlabMrNotesInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "fetch_gitlab_mr_notes"],
            creator=self,
        )

    async def _run(
        self,
        input: FetchGitlabMrNotesInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        session = aiohttp_session()
        headers = gitlab_headers()
        encoded_project = urlquote(input.project, safe="")

        url = (
            f"{GITLAB_URL}/api/v4/projects/{encoded_project}"
            f"/merge_requests/{input.mr_iid}/notes"
        )
        logger.info("Fetching MR notes from %s", url)

        try:
            async with session.get(
                url, headers=headers, params={"per_page": "100"}
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    logger.error(
                        "Failed to fetch MR notes (HTTP %d): %s",
                        response.status,
                        text,
                    )
                    return StringToolOutput(
                        result=f"Failed to fetch notes for MR !{input.mr_iid} "
                        f"in {input.project} (HTTP {response.status}): {text}"
                    )

                notes = await response.json()

            result = [
                {
                    "author": note["author"]["name"],
                    "body": note["body"],
                    "created_at": note.get("created_at"),
                    "system": note.get("system", False),
                }
                for note in notes
            ]

            return StringToolOutput(result=json.dumps(result, indent=2))

        except Exception as e:
            logger.error("Error fetching GitLab MR notes: %s", e)
            return StringToolOutput(
                result=f"Error fetching GitLab MR notes: {e}"
            )
