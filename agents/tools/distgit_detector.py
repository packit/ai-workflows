from urllib.parse import urlparse

from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import JSONToolOutput, Tool, ToolRunOptions


class DistgitDetectorInput(BaseModel):
    url: str = Field(description="URL to check if it's from a dist-git source")


class DistgitDetectorResult(BaseModel):
    is_distgit: bool = Field(description="True if URL is from a dist-git source (Fedora, RHEL, CentOS Stream), False otherwise")


class DistgitDetectorOutput(JSONToolOutput[DistgitDetectorResult]):
    pass


class DistgitDetectorTool(Tool[DistgitDetectorInput, ToolRunOptions, DistgitDetectorOutput]):
    name = "detect_distgit_source"
    description = """
    Detects if a URL is from a dist-git source (Fedora, RHEL, or CentOS Stream).

    Dist-git sources are packaging repositories that contain RPM spec files and patches,
    as opposed to upstream source code repositories. Patches from dist-git sources may
    be pure packaging changes that can be applied directly without the cherry-pick workflow.

    Returns a boolean: true if from dist-git, false if from upstream.
    """
    input_schema = DistgitDetectorInput

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "distgit", "detector"],
            creator=self,
        )

    def _check_distgit_source(self, url: str) -> bool:
        """Check if URL is from a dist-git source"""
        try:
            parsed = urlparse(url.lower())
            hostname = parsed.hostname or ""
            path = parsed.path.lower()

            # Fedora dist-git
            if "src.fedoraproject.org" in hostname:
                return True

            # RHEL/CentOS Stream GitLab
            if "gitlab.com" in hostname:
                return "/redhat/centos-stream/rpms/" in path or "/redhat/rhel/rpms/" in path

            # Not a recognized dist-git source
            return False

        except Exception:
            # If URL cannot be parsed, assume not dist-git
            return False

    async def _run(
        self, tool_input: DistgitDetectorInput, options: ToolRunOptions | None, context: RunContext
    ) -> DistgitDetectorOutput:
        is_distgit = self._check_distgit_source(tool_input.url)

        result = DistgitDetectorResult(is_distgit=is_distgit)

        return DistgitDetectorOutput(result)
