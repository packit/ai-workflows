"""Connect and serve Temporal workers in Ymir execution pods."""

import asyncio
import os
import signal
import tempfile
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor

from ymir.agents.observability import setup_observability
from ymir.agents.temporal_worker_pods import READY_FILE


async def connect_worker_client(address: str, namespace: str) -> Client:
    if endpoint := os.getenv("COLLECTOR_ENDPOINT"):
        setup_observability(endpoint)
    return await Client.connect(
        address,
        namespace=namespace,
        plugins=[PydanticAIPlugin()],
        interceptors=[TracingInterceptor()],
    )


async def serve_worker_pod(
    start_worker: Callable[[Path, Client, str], AbstractAsyncContextManager[None]],
) -> None:
    """Prepare pod storage and run the workflow's worker until termination."""
    ready = Path(READY_FILE)
    ready.unlink(missing_ok=True)
    workspace = Path(os.environ["GIT_REPO_BASEPATH"])
    tmpdir = workspace / ".tmp"
    tmpdir.mkdir(exist_ok=True)
    tempfile.tempdir = str(tmpdir)
    client = await connect_worker_client(os.environ["TEMPORAL_ADDRESS"], os.environ["TEMPORAL_NAMESPACE"])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with start_worker(workspace, client, os.environ["TEMPORAL_TASK_QUEUE"]):
        ready.touch()
        try:
            await stop.wait()
        finally:
            ready.unlink(missing_ok=True)
