"""CLI for the Ymir triage workflow."""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from dotenv import load_dotenv

from ymir.cli.compose import start_services, stop_services, submit_triage
from ymir.common.temporal_triage import (
    TriageIntent,
    connect_triage_ingress,
    submit_triage_intent,
    temporal_triage_accepts_requests,
    triage_issue_workflow_id,
)

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="ymir",
    help="Ymir CLI — run triage workflows locally.",
    no_args_is_help=True,
)

_INFERENCE_CREDENTIALS: dict[str, tuple[str, str]] = {
    "gemini:": ("GEMINI_API_KEY", "Google Gemini API key"),
    "anthropic:": ("ANTHROPIC_API_KEY", "Anthropic API key"),
    "vertexai:": ("GOOGLE_APPLICATION_CREDENTIALS", "Path to Google Vertex AI service account JSON"),
}


def check_credentials(mock_jira: bool = False) -> None:
    """Verify that required credentials are present in the environment.

    Must be called after load_env_file() so .env values are available.
    Checks JIRA, GitLab, and inference provider credentials.
    When *mock_jira* is True, Jira credentials are not required.
    """
    missing: list[tuple[str, str]] = []

    if not mock_jira:
        for var, desc in [
            ("JIRA_TOKEN", "Jira authentication token"),
            ("JIRA_EMAIL", "Jira account email for Basic Auth"),
        ]:
            if not os.getenv(var):
                missing.append((var, desc))

    for var, desc in [
        ("GITLAB_TOKEN", "GitLab authentication token"),
    ]:
        if not os.getenv(var):
            missing.append((var, desc))

    chat_model = os.environ.get("CHAT_MODEL", "")
    if not chat_model:
        missing.append(("CHAT_MODEL", "Name of model to use (e.g. gemini:gemini-2.5-pro)"))
    else:
        matched = False
        for prefix, (var, desc) in _INFERENCE_CREDENTIALS.items():
            if chat_model.startswith(prefix):
                matched = True
                if not os.getenv(var):
                    missing.append((var, desc))
                break
        if not matched:
            logger.warning("Unknown CHAT_MODEL prefix in %r; skipping API key check", chat_model)

    if missing:
        logger.error("Missing required environment variables:")
        for var, desc in missing:
            logger.error("  %s — %s", var, desc)
        raise typer.Exit(1)


async def _submit_triage(intent: TriageIntent, *, wait: bool) -> str | dict:
    client = await connect_triage_ingress()
    return await submit_triage_intent(client, intent, wait=wait)


@app.command()
def triage(
    issue: str = typer.Argument(help="Jira issue key (e.g. RHEL-12345)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Skip Jira writes and downstream queues"),
    force_cve_triage: bool = typer.Option(
        False, "--force-cve-triage", help="Force triage even when CVE eligibility check says to skip"
    ),
    secrets: str = typer.Option(
        ".secrets",
        "--secrets",
        help="Path to secrets directory containing rhel-config.json, vertex credentials etc.",
    ),
    compose_file: str = typer.Option(
        "compose.yaml",
        "--compose-file",
        help="Path to compose.yaml for infrastructure services.",
    ),
    mock_jira: str | None = typer.Option(
        None,
        "--mock-jira",
        help="Path to mock Jira data directory (enables MOCK_JIRA mode).",
    ),
    work_dir: str | None = typer.Option(
        None,
        "--work-dir",
        help="Directory for working files (git clones). Created as a temp dir if not specified.",
    ),
    request_id: Annotated[
        str | None,
        typer.Option(
            "--request-id", help="Reuse an ID to retry a Temporal submission without starting a new run."
        ),
    ] = None,
    full: Annotated[bool, typer.Option("--full", help="Run the downstream fix pipeline")] = False,
    detach: Annotated[
        bool, typer.Option("--detach", help="Return after Temporal accepts the request")
    ] = False,
) -> None:
    """Run triage on a single Jira issue."""
    if os.getenv("TEMPORAL_ADDRESS"):
        if not temporal_triage_accepts_requests():
            typer.echo("Temporal triage admissions are paused for this issue.", err=True)
            raise typer.Exit(1)
        if mock_jira:
            raise typer.BadParameter("--mock-jira requires the local Compose worker")
        intent = TriageIntent(
            issue.strip().upper(),
            f"cli:{request_id or uuid4()}",
            "cli",
            dry_run=dry_run,
            user_triggered=True,
            triage_only=not full,
            force_cve_triage=force_cve_triage,
        )
        try:
            result = asyncio.run(_submit_triage(intent, wait=not detach))
        except Exception as error:
            typer.echo(f"Temporal submission failed for {intent.request_id}: {error}", err=True)
            raise typer.Exit(1) from error
        if isinstance(result, str):
            typer.echo(f"{result} {intent.request_id} in {triage_issue_workflow_id(issue)}")
            return
        if result["output"] is not None:
            output = json.dumps(result["output"], indent=2)
            if work_dir:
                result_file = Path(work_dir) / intent.issue / "triage_result.json"
                result_file.parent.mkdir(parents=True, exist_ok=True)
                result_file.write_text(output, encoding="utf-8")
            typer.echo(output)
        else:
            typer.echo(result.get("detail") or result["status"], err=True)
        if result["status"] in {"failed", "unstarted"}:
            raise typer.Exit(1)
        return
    if request_id or detach or full:
        raise typer.BadParameter("--request-id, --detach and --full require TEMPORAL_ADDRESS")

    secrets_dir = Path(secrets).resolve()

    saved_env = os.environ.copy()

    rhel_config_path = secrets_dir / "rhel-config.json"
    if not rhel_config_path.exists():
        logger.error("RHEL config file not found: %s", rhel_config_path)
        raise typer.Exit(1)
    try:
        beeai_agent_env = secrets_dir / "beeai-agent.env"
        mcp_gateway_env = secrets_dir / "mcp-gateway.env"
        for env_file in (beeai_agent_env, mcp_gateway_env):
            if not env_file.exists():
                logger.error("File %s not found.", env_file)
                raise typer.Exit(1)
            load_dotenv(env_file, override=False)

        os.environ["RHEL_CONFIG_PATH"] = str(rhel_config_path)

        if mock_jira:
            os.environ["MOCK_JIRA"] = "true"
            os.environ["JIRA_MOCK_FILES_HOST"] = str(Path(mock_jira).resolve())

        check_credentials(mock_jira=os.getenv("MOCK_JIRA", "").lower() == "true")

        if work_dir:
            work_dir_path = Path(work_dir).resolve()
            work_dir_path.mkdir(parents=True, exist_ok=True)
        else:
            work_dir_path = Path(tempfile.mkdtemp(prefix="ymir-"))

        compose_path = Path(compose_file).resolve()
        issue_upper = issue.strip().upper()
        dry_run = dry_run or os.getenv("JIRA_DRY_RUN", "").lower() == "true"
        os.environ["DRY_RUN"] = "true" if dry_run else "false"
        os.environ["GIT_REPOS_HOST"] = str(work_dir_path)

        agent_failed = False
        try:
            start_services(compose_path)
            output = submit_triage(
                compose_path, issue_upper, dry_run=dry_run, force_cve_triage=force_cve_triage
            )
            if output:
                result_file = work_dir_path / issue_upper / "triage_result.json"
                result_file.parent.mkdir(parents=True, exist_ok=True)
                result_file.write_text(output, encoding="utf-8")
                typer.echo(output, nl=False)
        except subprocess.CalledProcessError:
            agent_failed = True
            typer.echo("Error: container exited with non-zero status.", err=True)
        except Exception as exc:
            agent_failed = True
            typer.echo(f"Error: {exc}", err=True)
        finally:
            try:
                stop_services(compose_path)
            except Exception:
                logger.warning("Failed to stop infrastructure services", exc_info=True)

            typer.echo(f"Work directory: {work_dir_path}")

            typer.echo(f"Trace viewer running at: http://localhost:8082/#/issues/{issue_upper}")

        if agent_failed:
            raise typer.Exit(1)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


if __name__ == "__main__":
    app()
