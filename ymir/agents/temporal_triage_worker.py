"""Bootstrap the worker container for one provisioned triage execution."""

import asyncio
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from temporalio.client import Client

from ymir.agents.pydantic_agent import configured_model
from ymir.agents.temporal_triage_analysis import create_analysis_worker
from ymir.agents.temporal_worker import serve_worker_pod
from ymir.agents.utils import resolve_chat_model_override
from ymir.common.mock_repos import cleanup_mock_gitconfig, setup_mock_repos_from_env


@asynccontextmanager
async def start_triage_worker(workspace: Path, client: Client, task_queue: str):
    issue = os.environ["YMIR_TRIAGE_ISSUE"]
    if os.environ.get("MOCK_REPOS_DIR") and (
        os.getenv("MOCK_JIRA", "").lower() == "true" and os.getenv("DRY_RUN", "").lower() == "true"
    ):
        clones = workspace / "mock-repos"
        prepared = clones / ".prepared"
        if prepared.exists():
            os.environ["MOCK_BLOCKED_URLS"] = prepared.read_text()
        else:
            shutil.rmtree(clones, ignore_errors=True)
            clones.mkdir()
            cleanup_mock_gitconfig()
            await asyncio.to_thread(setup_mock_repos_from_env, issue, clones)
            prepared.write_text(os.getenv("MOCK_BLOCKED_URLS", ""))
    resolve_chat_model_override("triage")
    async with (
        configured_model() as model,
        await create_analysis_worker(client, task_queue, model, gateway_connection_attempts=30),
    ):
        yield


if __name__ == "__main__":
    asyncio.run(serve_worker_pod(start_triage_worker))
