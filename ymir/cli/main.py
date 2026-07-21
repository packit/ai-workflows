"""CLI for the Ymir triage workflow."""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

import typer
from dotenv import load_dotenv

from ymir.cli.compose import run_agent, start_services, stop_services

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
) -> None:
    """Run triage analysis on a single Jira issue."""
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

        check_credentials(mock_jira=bool(mock_jira))

        if work_dir:
            work_dir_path = Path(work_dir).resolve()
            work_dir_path.mkdir(parents=True, exist_ok=True)
        else:
            work_dir_path = Path(tempfile.mkdtemp(prefix="ymir-"))

        compose_path = Path(compose_file).resolve()
        issue_upper = issue.upper()

        agent_env = {
            "JIRA_ISSUE": issue_upper,
            "DRY_RUN": "true" if dry_run else "false",
            "FORCE_CVE_TRIAGE": "true" if force_cve_triage else "false",
            "AUTO_CHAIN": "false",
            "USER_TRIGGERED": "true",
        }
        if mock_jira:
            agent_env["MOCK_JIRA"] = "true"

        os.environ["GIT_REPOS_HOST"] = str(work_dir_path)

        agent_failed = False
        try:
            start_services(compose_path)
            run_agent(compose_path, agent_env)
        except subprocess.CalledProcessError:
            agent_failed = True
            typer.echo("Error: container exited with non-zero status.", err=True)
        except Exception as exc:
            agent_failed = True
            typer.echo(f"Error: {exc}", err=True)
        finally:
            result_file = work_dir_path / issue_upper / "triage_result.json"
            if result_file.is_file():
                typer.echo(result_file.read_text(encoding="utf-8"))
            else:
                typer.echo("Warning: no triage result file found.", err=True)

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
