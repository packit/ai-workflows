"""Bootstrap the worker container for one provisioned triage execution."""

import asyncio
import os
import shutil
import signal
import tempfile
from pathlib import Path

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor

from ymir.agents.observability import setup_observability
from ymir.agents.pydantic_agent import configured_model
from ymir.agents.temporal_triage_analysis import create_analysis_worker
from ymir.agents.temporal_triage_provisioning import READY_FILE
from ymir.agents.utils import resolve_chat_model_override
from ymir.common.mock_repos import cleanup_mock_gitconfig, setup_mock_repos_from_env


async def main() -> None:
    ready = Path(READY_FILE)
    ready.unlink(missing_ok=True)
    workspace = Path(os.environ["GIT_REPO_BASEPATH"])
    tmpdir = workspace / ".tmp"
    tmpdir.mkdir(exist_ok=True)
    tempfile.tempdir = str(tmpdir)
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
    if endpoint := os.getenv("COLLECTOR_ENDPOINT"):
        setup_observability(endpoint)
    resolve_chat_model_override("triage")
    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"],
        namespace=os.environ["TEMPORAL_NAMESPACE"],
        plugins=[PydanticAIPlugin()],
        interceptors=[TracingInterceptor()],
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with (
        configured_model() as model,
        await create_analysis_worker(
            client, os.environ["TEMPORAL_TASK_QUEUE"], model, publication=True, gateway_connection_attempts=30
        ),
    ):
        ready.touch()
        try:
            await stop.wait()
        finally:
            ready.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
