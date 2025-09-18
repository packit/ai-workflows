import aiohttp
from pydantic import BaseModel, Field

from beeai_framework.context import RunContext
from beeai_framework.emitter import Emitter
from beeai_framework.tools import StringToolOutput, Tool, ToolRunOptions


class ReadReadmeInput(BaseModel):
    repo_url: str = Field(description="URL of git repository to read README from")


README_PATTERNS = [
    ("https://gitlab.com/", "/-/raw/main/README.md?ref_type=heads&inline=false"),
    (
        "https://gitlab.cee.redhat.com/",
        "/-/raw/main/README.md?ref_type=heads&inline=false",
    ),
    ("https://pkgs.devel.redhat.com/", "/plain/README"),
]


class ReadReadmeTool(Tool[ReadReadmeInput, ToolRunOptions, StringToolOutput]):
    name = "read_readme"  # type: ignore
    description = "Read README file from git repository"  # type: ignore
    input_schema = ReadReadmeInput  # type: ignore

    def _create_emitter(self) -> Emitter:
        return Emitter.root().child(
            namespace=["tool", "read_readme"],
            creator=self,
        )

    async def _run(
        self,
        input: ReadReadmeInput,
        options: ToolRunOptions | None,
        context: RunContext,
    ) -> StringToolOutput:
        async with aiohttp.ClientSession() as session:
            url = None
            for prefix, suffix in README_PATTERNS:
                if input.repo_url.startswith(prefix):
                    url = input.repo_url.removesuffix("/") + suffix
                    async with session.get(url) as response:
                        if response.status == 200:
                            return StringToolOutput(
                                result=await response.text(),
                            )
        return StringToolOutput(result=f"Failed to find README.md for {input.repo_url}")
