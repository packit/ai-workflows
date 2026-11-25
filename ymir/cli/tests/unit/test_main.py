import logging
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import typer
from flexmock import flexmock

from ymir.cli import main as cli_main
from ymir.cli.main import (
    check_credentials,
    triage,
)

_BASE_CREDS = {
    "JIRA_TOKEN": "tok",
    "JIRA_EMAIL": "a@b.c",
    "GITLAB_TOKEN": "gl",
    "CHAT_MODEL": "gemini:gemini-2.5-pro",
    "GEMINI_API_KEY": "gkey",  # pragma: allowlist secret
}


def _set_mock_env(env: dict | None = None, drop: Sequence[str] = ()):
    if env is None:
        env = {k: v for k, v in _BASE_CREDS.items() if k not in drop}
    elif drop:
        env = {k: v for k, v in env.items() if k not in drop}
    flexmock(os, environ=env)
    return env


class TestCheckCredentials:
    def test_all_present(self):
        _set_mock_env()
        check_credentials()

    @pytest.mark.parametrize("var", list(_BASE_CREDS.keys()))
    def test_missing_base_credential(self, var):
        _set_mock_env(drop=[var])
        with pytest.raises(typer.Exit):
            check_credentials()

    def test_anthropic_missing_key(self):
        _set_mock_env(
            env={**_BASE_CREDS, "CHAT_MODEL": "anthropic:claude-sonnet-4-20250514"},
            drop=["GEMINI_API_KEY"],
        )
        with pytest.raises(typer.Exit):
            check_credentials()

    def test_anthropic_present(self):
        _set_mock_env(
            env={
                **_BASE_CREDS,
                "CHAT_MODEL": "anthropic:claude-sonnet-4-20250514",
                "ANTHROPIC_API_KEY": "ak",  # pragma: allowlist secret
            }
        )
        check_credentials()

    def test_vertexai_missing_credentials(self):
        _set_mock_env(
            env={**_BASE_CREDS, "CHAT_MODEL": "vertexai:gemini-2.5-pro"},
            drop=["GEMINI_API_KEY"],
        )
        with pytest.raises(typer.Exit):
            check_credentials()

    def test_vertexai_present(self):
        _set_mock_env(
            env={
                **_BASE_CREDS,
                "CHAT_MODEL": "vertexai:gemini-2.5-pro",
                "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/sa.json",
            }
        )
        check_credentials()

    def test_unknown_prefix_warns(self, caplog):
        _set_mock_env(env={**_BASE_CREDS, "CHAT_MODEL": "ollama:llama3"})
        with caplog.at_level(logging.WARNING):
            check_credentials()
        assert "Unknown CHAT_MODEL prefix" in caplog.text

    def test_reports_all_missing_at_once(self, caplog):
        _set_mock_env(env={})
        with caplog.at_level(logging.ERROR), pytest.raises(typer.Exit):
            check_credentials()
        assert "JIRA_TOKEN" in caplog.text
        assert "JIRA_EMAIL" in caplog.text
        assert "GITLAB_TOKEN" in caplog.text
        assert "CHAT_MODEL" in caplog.text

    def test_mock_jira_skips_jira_credentials(self):
        _set_mock_env(drop=("JIRA_TOKEN", "JIRA_EMAIL"))
        check_credentials(mock_jira=True)

    @pytest.mark.parametrize("var", ["GITLAB_TOKEN", "CHAT_MODEL", "GEMINI_API_KEY"])
    def test_mock_jira_still_requires_non_jira_necessities(self, var):
        _set_mock_env(drop=("JIRA_TOKEN", "JIRA_EMAIL", var))
        with pytest.raises(typer.Exit):
            check_credentials(mock_jira=True)

    def test_mock_jira_reports_only_non_jira_missing(self, caplog):
        _set_mock_env(env={})
        with caplog.at_level(logging.ERROR), pytest.raises(typer.Exit):
            check_credentials(mock_jira=True)
        assert "JIRA_TOKEN" not in caplog.text
        assert "JIRA_EMAIL" not in caplog.text
        assert "GITLAB_TOKEN" in caplog.text
        assert "CHAT_MODEL" in caplog.text


def _make_secrets(tmp_path):
    """Create minimal secrets directory for testing."""
    secrets_dir = tmp_path / ".secrets"
    secrets_dir.mkdir()
    (secrets_dir / "rhel-config.json").write_text("{}")
    (secrets_dir / "beeai-agent.env").write_text("CHAT_MODEL=test:model\n")
    (secrets_dir / "mcp-gateway.env").touch()
    return secrets_dir


class TestRhelConfigCheck:
    def test_missing_rhel_config_exits(self, tmp_path):
        secrets_dir = tmp_path / ".secrets"
        secrets_dir.mkdir()
        (secrets_dir / "beeai-agent.env").write_text("CHAT_MODEL=test:model\n")
        (secrets_dir / "mcp-gateway.env").touch()

        with pytest.raises(typer.Exit):
            triage(
                issue="RHEL-99999",
                dry_run=True,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="compose.yaml",
                mock_jira=None,
                work_dir=str(tmp_path / "work"),
            )

    def test_present_rhel_config_continues(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage")

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=str(tmp_path / "work"),
        )


def test_compose_submission_receives_normalized_issue_and_run_options(tmp_path, monkeypatch):
    secrets_dir = _make_secrets(tmp_path)
    monkeypatch.setenv("JIRA_DRY_RUN", "true")

    def submit(_compose_file, issue, *, dry_run, force_cve_triage):
        assert issue == "RHEL-12345"
        assert dry_run and force_cve_triage
        assert os.environ["DRY_RUN"] == "true"

    flexmock(cli_main).should_receive("check_credentials")
    flexmock(cli_main).should_receive("start_services")
    flexmock(cli_main).should_receive("stop_services")
    flexmock(cli_main).should_receive("submit_triage").replace_with(submit).once()

    triage(
        issue="rhel-12345",
        dry_run=False,
        force_cve_triage=True,
        secrets=str(secrets_dir),
        compose_file="compose.yaml",
        mock_jira=None,
        work_dir=str(tmp_path / "work"),
    )


class TestTriageContainerLifecycle:
    def test_start_and_stop_called(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services").once()
        flexmock(cli_main).should_receive("stop_services").once()
        flexmock(cli_main).should_receive("submit_triage")

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=str(tmp_path / "work"),
        )

    def test_stop_called_on_agent_failure(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services").once()
        flexmock(cli_main).should_receive("submit_triage").and_raise(
            subprocess.CalledProcessError(1, "podman")
        )
        with pytest.raises((SystemExit, typer.Exit)):
            triage(
                issue="RHEL-99999",
                dry_run=False,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="compose.yaml",
                mock_jira=None,
                work_dir=str(tmp_path / "work"),
            )

    def test_compose_file_resolved(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)

        passed_path = None

        def _mock_start(compose_file):
            nonlocal passed_path
            passed_path = compose_file

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services").replace_with(_mock_start)
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage")

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="custom/compose.yaml",
            mock_jira=None,
            work_dir=str(tmp_path / "work"),
        )

        assert isinstance(passed_path, Path)
        assert passed_path.is_absolute()
        assert str(passed_path).endswith("custom/compose.yaml")

    def test_stop_failure_does_not_mask_agent_error(self, tmp_path, capsys):
        """When both submit_triage and stop_services fail, the agent error reaches the user."""
        secrets_dir = _make_secrets(tmp_path)

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services").and_raise(RuntimeError("podman down"))
        flexmock(cli_main).should_receive("submit_triage").and_raise(RuntimeError("workflow failed"))

        with pytest.raises((SystemExit, typer.Exit)):
            triage(
                issue="RHEL-99999",
                dry_run=False,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="compose.yaml",
                mock_jira=None,
                work_dir=str(tmp_path / "work"),
            )

        captured = capsys.readouterr()
        assert "workflow failed" in captured.err

    def test_work_dir_persists_when_stop_fails(self, tmp_path):
        """Work dir must survive even when stop_services fails."""
        secrets_dir = _make_secrets(tmp_path)
        work_dir = tmp_path / "work"

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services").and_raise(RuntimeError("podman down"))
        flexmock(cli_main).should_receive("submit_triage")
        flexmock(os, environ=os.environ.copy())

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=str(work_dir),
        )

        assert work_dir.exists()


class TestWorkDirEnv:
    """--work-dir controls GIT_REPOS_HOST for compose bind-mount."""

    def test_default_tmpdir_sets_git_repos_host(self, tmp_path):
        """Without --work-dir, an auto-created temp dir drives GIT_REPOS_HOST."""
        secrets_dir = _make_secrets(tmp_path)
        captured_env = {}

        def _capture_env(*_args, **_kwargs):
            captured_env["GIT_REPOS_HOST"] = os.environ.get("GIT_REPOS_HOST")

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").replace_with(_capture_env)
        flexmock(os, environ={})

        os.environ.pop("GIT_REPOS_HOST", None)

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=None,
        )

        assert captured_env["GIT_REPOS_HOST"] is not None
        assert captured_env["GIT_REPOS_HOST"].startswith("/tmp/ymir-")

    def test_explicit_work_dir_sets_git_repos_host(self, tmp_path):
        """When --work-dir is given, GIT_REPOS_HOST must equal it."""
        secrets_dir = _make_secrets(tmp_path)
        work_dir = tmp_path / "custom-repos"
        captured_env = {}

        def _capture_env(*_args, **_kwargs):
            captured_env["GIT_REPOS_HOST"] = os.environ.get("GIT_REPOS_HOST")

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").replace_with(_capture_env)
        flexmock(os, environ={})

        os.environ.pop("GIT_REPOS_HOST", None)

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=str(work_dir),
        )

        assert captured_env["GIT_REPOS_HOST"] == str(work_dir)

    def test_git_repos_host_restored_after_run(self, tmp_path):
        """GIT_REPOS_HOST is restored to its original value after triage() returns."""
        secrets_dir = _make_secrets(tmp_path)
        captured_env = {}

        def _capture_env(*_args, **_kwargs):
            captured_env["GIT_REPOS_HOST"] = os.environ.get("GIT_REPOS_HOST")

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").replace_with(_capture_env)
        flexmock(os, environ={"GIT_REPOS_HOST": "/custom-mount"})

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=None,
        )

        assert captured_env["GIT_REPOS_HOST"] != "/custom-mount"
        assert os.environ["GIT_REPOS_HOST"] == "/custom-mount"

    def test_work_dir_path_printed(self, tmp_path, capsys):
        """Work directory path must be printed at exit."""
        secrets_dir = _make_secrets(tmp_path)
        work_dir = tmp_path / "work"

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage")

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=str(work_dir),
        )

        captured = capsys.readouterr()
        assert f"Work directory: {work_dir}" in captured.out


class TestMockJira:
    def test_mock_jira_sets_env_vars(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)
        mock_dir = tmp_path / "jiras"
        mock_dir.mkdir()
        captured_env = {}

        def _capture_env(*_args, **_kwargs):
            captured_env["MOCK_JIRA"] = os.environ.get("MOCK_JIRA")
            captured_env["JIRA_MOCK_FILES_HOST"] = os.environ.get("JIRA_MOCK_FILES_HOST")

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").replace_with(_capture_env)
        flexmock(os, environ={})

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=str(mock_dir),
            work_dir=str(tmp_path / "work"),
        )

        assert captured_env["MOCK_JIRA"] == "true"
        assert captured_env["JIRA_MOCK_FILES_HOST"] == str(mock_dir)

    def test_mock_jira_none_leaves_env_unchanged(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)
        env_before = os.environ.copy()

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage")
        flexmock(os, environ=os.environ.copy())

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=str(tmp_path / "work"),
        )

        assert "MOCK_JIRA" not in os.environ or os.environ.get("MOCK_JIRA") == env_before.get("MOCK_JIRA")
        assert "JIRA_MOCK_FILES_HOST" not in os.environ or os.environ.get(
            "JIRA_MOCK_FILES_HOST"
        ) == env_before.get("JIRA_MOCK_FILES_HOST")


class TestEnvFileLoading:
    def test_loads_both_env_files(self, tmp_path):
        """Both beeai-agent.env and mcp-gateway.env must be loaded."""
        secrets_dir = tmp_path / ".secrets"
        secrets_dir.mkdir()
        (secrets_dir / "rhel-config.json").write_text("{}")
        (secrets_dir / "beeai-agent.env").write_text("CHAT_MODEL=test:model\n")
        (secrets_dir / "mcp-gateway.env").write_text("JIRA_TOKEN=tok\nJIRA_EMAIL=a@b.c\nGITLAB_TOKEN=gl\n")

        captured_env = {}

        def _capture_env(*args, **kwargs):
            captured_env.update(os.environ)

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").replace_with(_capture_env)
        flexmock(os, environ={})

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=str(tmp_path / "work"),
        )

        assert captured_env.get("CHAT_MODEL") == "test:model"
        assert captured_env.get("JIRA_TOKEN") == "tok"
        assert captured_env.get("JIRA_EMAIL") == "a@b.c"
        assert captured_env.get("GITLAB_TOKEN") == "gl"


class TestTriageResult:
    def test_result_from_temporal_is_printed_and_written(self, tmp_path, capsys):
        secrets_dir = _make_secrets(tmp_path)
        work_dir = tmp_path / "work"
        result_file = work_dir / "RHEL-99999" / "triage_result.json"
        output = '{"resolution": "backport"}\n'

        flexmock(os, environ={})
        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").and_return(output)

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=str(work_dir),
        )

        assert result_file.read_text() == output
        assert output in capsys.readouterr().out

    def test_skipped_request_does_not_print_stale_result(self, tmp_path, capsys):
        secrets_dir = _make_secrets(tmp_path)
        work_dir = tmp_path / "work"
        result_file = work_dir / "RHEL-99999" / "triage_result.json"
        result_file.parent.mkdir(parents=True)
        result_file.write_text('{"resolution": "backport"}')

        flexmock(os, environ={})
        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").and_return("")

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=None,
            work_dir=str(work_dir),
        )

        assert '"resolution"' not in capsys.readouterr().out

    def test_agent_error_raises_exit(self, tmp_path, capsys):
        secrets_dir = _make_secrets(tmp_path)

        flexmock(os, environ={})
        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").and_raise(
            subprocess.CalledProcessError(1, "podman")
        )
        with pytest.raises((SystemExit, typer.Exit)):
            os.environ.pop("GIT_REPOS_HOST", None)
            triage(
                issue="RHEL-99999",
                dry_run=False,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="compose.yaml",
                mock_jira=None,
                work_dir=str(tmp_path / "work"),
            )

        captured = capsys.readouterr()
        assert "container exited with non-zero status" in captured.err


class TestNonCalledProcessError:
    """Non-CalledProcessError exceptions from submit_triage produce typer.Exit(1)."""

    def test_runtime_error_produces_exit(self, tmp_path, capsys):
        secrets_dir = _make_secrets(tmp_path)

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").and_raise(RuntimeError("compose crashed"))

        with pytest.raises((SystemExit, typer.Exit)):
            triage(
                issue="RHEL-99999",
                dry_run=False,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="compose.yaml",
                mock_jira=None,
                work_dir=str(tmp_path / "work"),
            )

        captured = capsys.readouterr()
        assert "compose crashed" in captured.err

    def test_os_error_produces_exit(self, tmp_path, capsys):
        secrets_dir = _make_secrets(tmp_path)

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").and_raise(OSError("permission denied"))

        with pytest.raises((SystemExit, typer.Exit)):
            triage(
                issue="RHEL-99999",
                dry_run=False,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="compose.yaml",
                mock_jira=None,
                work_dir=str(tmp_path / "work"),
            )

        captured = capsys.readouterr()
        assert "permission denied" in captured.err


class TestEnvVarRestoration:
    """Verify os.environ is restored after triage() returns."""

    def test_env_vars_restored_on_success(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)
        env_before = {
            k: os.environ.get(k)
            for k in (
                "RHEL_CONFIG_PATH",
                "MOCK_JIRA",
                "JIRA_MOCK_FILES_HOST",
                "GIT_REPOS_HOST",
            )
        }

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage")

        triage(
            issue="RHEL-99999",
            dry_run=True,
            force_cve_triage=False,
            secrets=str(secrets_dir),
            compose_file="compose.yaml",
            mock_jira=str(tmp_path / "jiras"),
            work_dir=str(tmp_path / "work"),
        )

        env_after = {k: os.environ.get(k) for k in env_before}
        assert env_after == env_before

    def test_env_vars_restored_on_agent_failure(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)
        env_before = {
            k: os.environ.get(k)
            for k in (
                "RHEL_CONFIG_PATH",
                "MOCK_JIRA",
                "JIRA_MOCK_FILES_HOST",
                "GIT_REPOS_HOST",
            )
        }

        flexmock(cli_main).should_receive("check_credentials")
        flexmock(cli_main).should_receive("start_services")
        flexmock(cli_main).should_receive("stop_services")
        flexmock(cli_main).should_receive("submit_triage").and_raise(
            subprocess.CalledProcessError(1, "podman")
        )

        with pytest.raises((SystemExit, typer.Exit)):
            triage(
                issue="RHEL-99999",
                dry_run=False,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="compose.yaml",
                mock_jira=str(tmp_path / "jiras"),
                work_dir=str(tmp_path / "work"),
            )

        env_after = {k: os.environ.get(k) for k in env_before}
        assert env_after == env_before


def test_cli_waits_for_the_temporal_request_and_prints_json(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")
    monkeypatch.setenv("TEMPORAL_TRIAGE_ADMISSIONS", "true")

    async def submit(intent, *, wait):
        assert intent.issue == "RHEL-99999" and intent.request_id == "cli:test-retry"
        assert intent.user_triggered and intent.triage_only
        assert intent.dry_run and intent.force_cve_triage and wait
        return {"status": "completed", "detail": None, "output": {"resolution": "backport"}}

    flexmock(cli_main).should_receive("_submit_triage").replace_with(submit).once()
    flexmock(cli_main).should_receive("start_services").never()
    result = CliRunner().invoke(
        cli_main.app,
        [
            "rhel-99999",
            "--dry-run",
            "--force-cve-triage",
            "--request-id",
            "test-retry",
            "--work-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"resolution": "backport"' in result.output
    assert (tmp_path / "RHEL-99999" / "triage_result.json").read_text().strip() == result.output.strip()
