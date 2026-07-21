import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer

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


class TestCheckCredentials:
    def test_all_present(self):
        with patch.dict(os.environ, _BASE_CREDS, clear=True):
            check_credentials()

    @pytest.mark.parametrize("var", ["JIRA_TOKEN", "JIRA_EMAIL", "GITLAB_TOKEN"])
    def test_missing_base_credential(self, var):
        env = {k: v for k, v in _BASE_CREDS.items() if k != var}
        with patch.dict(os.environ, env, clear=True), pytest.raises(typer.Exit):
            check_credentials()

    def test_missing_chat_model(self):
        env = {k: v for k, v in _BASE_CREDS.items() if k != "CHAT_MODEL"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(typer.Exit):
            check_credentials()

    def test_gemini_missing_key(self):
        env = {k: v for k, v in _BASE_CREDS.items() if k != "GEMINI_API_KEY"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(typer.Exit):
            check_credentials()

    def test_anthropic_missing_key(self):
        env = {
            **_BASE_CREDS,
            "CHAT_MODEL": "anthropic:claude-sonnet-4-20250514",
        }
        env.pop("GEMINI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True), pytest.raises(typer.Exit):
            check_credentials()

    def test_anthropic_present(self):
        env = {
            **_BASE_CREDS,
            "CHAT_MODEL": "anthropic:claude-sonnet-4-20250514",
            "ANTHROPIC_API_KEY": "ak",  # pragma: allowlist secret
        }
        with patch.dict(os.environ, env, clear=True):
            check_credentials()

    def test_vertexai_missing_credentials(self):
        env = {
            **_BASE_CREDS,
            "CHAT_MODEL": "vertexai:gemini-2.5-pro",
        }
        env.pop("GEMINI_API_KEY", None)
        with patch.dict(os.environ, env, clear=True), pytest.raises(typer.Exit):
            check_credentials()

    def test_vertexai_present(self):
        env = {
            **_BASE_CREDS,
            "CHAT_MODEL": "vertexai:gemini-2.5-pro",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/sa.json",
        }
        with patch.dict(os.environ, env, clear=True):
            check_credentials()

    def test_unknown_prefix_warns(self, caplog):
        env = {
            **_BASE_CREDS,
            "CHAT_MODEL": "ollama:llama3",
        }
        with patch.dict(os.environ, env, clear=True), caplog.at_level(logging.WARNING):
            check_credentials()
        assert "Unknown CHAT_MODEL prefix" in caplog.text

    def test_reports_all_missing_at_once(self, caplog):
        with (
            patch.dict(os.environ, {}, clear=True),
            caplog.at_level(logging.ERROR),
            pytest.raises(typer.Exit),
        ):
            check_credentials()
        assert "JIRA_TOKEN" in caplog.text
        assert "JIRA_EMAIL" in caplog.text
        assert "GITLAB_TOKEN" in caplog.text
        assert "CHAT_MODEL" in caplog.text

    def test_mock_jira_skips_jira_credentials(self):
        env = {k: v for k, v in _BASE_CREDS.items() if k not in ("JIRA_TOKEN", "JIRA_EMAIL")}
        with patch.dict(os.environ, env, clear=True):
            check_credentials(mock_jira=True)

    @pytest.mark.parametrize("var", ["GITLAB_TOKEN"])
    def test_mock_jira_still_requires_non_jira_credentials(self, var):
        env = {k: v for k, v in _BASE_CREDS.items() if k not in ("JIRA_TOKEN", "JIRA_EMAIL", var)}
        with patch.dict(os.environ, env, clear=True), pytest.raises(typer.Exit):
            check_credentials(mock_jira=True)

    def test_mock_jira_still_requires_chat_model(self):
        env = {k: v for k, v in _BASE_CREDS.items() if k not in ("JIRA_TOKEN", "JIRA_EMAIL", "CHAT_MODEL")}
        with patch.dict(os.environ, env, clear=True), pytest.raises(typer.Exit):
            check_credentials(mock_jira=True)

    def test_mock_jira_still_requires_inference_key(self):
        env = {
            k: v for k, v in _BASE_CREDS.items() if k not in ("JIRA_TOKEN", "JIRA_EMAIL", "GEMINI_API_KEY")
        }
        with patch.dict(os.environ, env, clear=True), pytest.raises(typer.Exit):
            check_credentials(mock_jira=True)

    def test_mock_jira_reports_only_non_jira_missing(self, caplog):
        with (
            patch.dict(os.environ, {}, clear=True),
            caplog.at_level(logging.ERROR),
            pytest.raises(typer.Exit),
        ):
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

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent"),
        ):
            triage(
                issue="RHEL-99999",
                dry_run=True,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="compose.yaml",
                mock_jira=None,
                work_dir=str(tmp_path / "work"),
            )


class TestAgentEnvVars:
    """Verify the env dict forwarded to run_agent for each CLI flag."""

    def _run(self, tmp_path, **overrides):
        secrets_dir = _make_secrets(tmp_path)
        defaults = {
            "issue": "RHEL-99999",
            "dry_run": False,
            "force_cve_triage": False,
            "secrets": str(secrets_dir),
            "compose_file": "compose.yaml",
            "mock_jira": None,
            "work_dir": str(tmp_path / "work"),
        }
        defaults.update(overrides)
        mock_run_agent = MagicMock()
        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent", mock_run_agent),
        ):
            triage(**defaults)
        return mock_run_agent

    def test_jira_issue_uppercased(self, tmp_path):
        mock = self._run(tmp_path, issue="rhel-12345")
        assert mock.call_args[0][1]["JIRA_ISSUE"] == "RHEL-12345"

    def test_dry_run_true(self, tmp_path):
        mock = self._run(tmp_path, dry_run=True)
        assert mock.call_args[0][1]["DRY_RUN"] == "true"

    def test_dry_run_false(self, tmp_path):
        mock = self._run(tmp_path, dry_run=False)
        assert mock.call_args[0][1]["DRY_RUN"] == "false"

    def test_force_cve_triage_true(self, tmp_path):
        mock = self._run(tmp_path, force_cve_triage=True)
        assert mock.call_args[0][1]["FORCE_CVE_TRIAGE"] == "true"

    def test_force_cve_triage_false(self, tmp_path):
        mock = self._run(tmp_path, force_cve_triage=False)
        assert mock.call_args[0][1]["FORCE_CVE_TRIAGE"] == "false"

    def test_auto_chain_always_false(self, tmp_path):
        mock = self._run(tmp_path)
        assert mock.call_args[0][1]["AUTO_CHAIN"] == "false"

    def test_user_triggered_always_true(self, tmp_path):
        mock = self._run(tmp_path)
        assert mock.call_args[0][1]["USER_TRIGGERED"] == "true"

    def test_mock_jira_included_when_set(self, tmp_path):
        mock_dir = tmp_path / "jiras"
        mock_dir.mkdir()
        mock = self._run(tmp_path, mock_jira=str(mock_dir))
        assert mock.call_args[0][1]["MOCK_JIRA"] == "true"

    def test_mock_jira_absent_when_none(self, tmp_path):
        mock = self._run(tmp_path, mock_jira=None)
        assert "MOCK_JIRA" not in mock.call_args[0][1]


class TestTriageContainerLifecycle:
    def test_start_and_stop_called(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)
        mock_start = MagicMock()
        mock_stop = MagicMock()

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services", mock_start),
            patch("ymir.cli.main.stop_services", mock_stop),
            patch("ymir.cli.main.run_agent"),
        ):
            triage(
                issue="RHEL-99999",
                dry_run=True,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="compose.yaml",
                mock_jira=None,
                work_dir=str(tmp_path / "work"),
            )

        mock_start.assert_called_once()
        mock_stop.assert_called_once()

    def test_stop_called_on_agent_failure(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)
        mock_stop = MagicMock()

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services", mock_stop),
            patch(
                "ymir.cli.main.run_agent",
                side_effect=subprocess.CalledProcessError(1, "podman"),
            ),
            pytest.raises((SystemExit, typer.Exit)),
        ):
            triage(
                issue="RHEL-99999",
                dry_run=False,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="compose.yaml",
                mock_jira=None,
                work_dir=str(tmp_path / "work"),
            )

        mock_stop.assert_called_once()

    def test_compose_file_resolved(self, tmp_path):
        secrets_dir = _make_secrets(tmp_path)
        mock_start = MagicMock()

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services", mock_start),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent"),
        ):
            triage(
                issue="RHEL-99999",
                dry_run=True,
                force_cve_triage=False,
                secrets=str(secrets_dir),
                compose_file="custom/compose.yaml",
                mock_jira=None,
                work_dir=str(tmp_path / "work"),
            )

        passed_path = mock_start.call_args[0][0]
        assert isinstance(passed_path, Path)
        assert passed_path.is_absolute()
        assert str(passed_path).endswith("custom/compose.yaml")

    def test_stop_failure_does_not_mask_agent_error(self, tmp_path, capsys):
        """When both run_agent and stop_services fail, the agent error reaches the user."""
        secrets_dir = _make_secrets(tmp_path)

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services", side_effect=RuntimeError("podman down")),
            patch("ymir.cli.main.run_agent", side_effect=RuntimeError("workflow failed")),
            pytest.raises((SystemExit, typer.Exit)),
        ):
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

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services", side_effect=RuntimeError("podman down")),
            patch("ymir.cli.main.run_agent"),
            patch.dict(os.environ, {}, clear=False),
        ):
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

        def capture_env(*_args, **_kwargs):
            captured_env["GIT_REPOS_HOST"] = os.environ.get("GIT_REPOS_HOST")

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent", side_effect=capture_env),
            patch.dict(os.environ, {}, clear=False),
        ):
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

        def capture_env(*_args, **_kwargs):
            captured_env["GIT_REPOS_HOST"] = os.environ.get("GIT_REPOS_HOST")

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent", side_effect=capture_env),
            patch.dict(os.environ, {}, clear=False),
        ):
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

        def capture_env(*_args, **_kwargs):
            captured_env["GIT_REPOS_HOST"] = os.environ.get("GIT_REPOS_HOST")

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent", side_effect=capture_env),
            patch.dict(
                os.environ,
                {"GIT_REPOS_HOST": "/custom-mount"},
                clear=False,
            ),
        ):
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

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent"),
        ):
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

        def capture_env(*_args, **_kwargs):
            captured_env["MOCK_JIRA"] = os.environ.get("MOCK_JIRA")
            captured_env["JIRA_MOCK_FILES_HOST"] = os.environ.get("JIRA_MOCK_FILES_HOST")

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent", side_effect=capture_env),
            patch.dict(os.environ, {}, clear=False),
        ):
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

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent"),
            patch.dict(os.environ, {}, clear=False),
        ):
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

        def capture_env(*args, **kwargs):
            captured_env.update(os.environ)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent", side_effect=capture_env),
        ):
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


class TestResultReading:
    """Verify triage result file is read and echoed."""

    def test_result_file_echoed(self, tmp_path, capsys):
        secrets_dir = _make_secrets(tmp_path)
        work_dir = tmp_path / "work"
        result_dir = work_dir / "RHEL-99999"
        result_dir.mkdir(parents=True)
        (result_dir / "triage_result.json").write_text('{"resolution": "backport"}')

        with (
            patch.dict(os.environ, {}, clear=False),
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent"),
        ):
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

        captured = capsys.readouterr()
        assert '{"resolution": "backport"}' in captured.out

    def test_no_result_file_warning(self, tmp_path, capsys):
        secrets_dir = _make_secrets(tmp_path)
        work_dir = tmp_path / "work"

        with (
            patch.dict(os.environ, {}, clear=False),
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent"),
        ):
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

        captured = capsys.readouterr()
        assert "no triage result file found" in captured.err

    def test_agent_error_raises_exit(self, tmp_path, capsys):
        secrets_dir = _make_secrets(tmp_path)

        with (
            patch.dict(os.environ, {}, clear=False),
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch(
                "ymir.cli.main.run_agent",
                side_effect=subprocess.CalledProcessError(1, "podman"),
            ),
            pytest.raises((SystemExit, typer.Exit)),
        ):
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
    """Non-CalledProcessError exceptions from run_agent produce typer.Exit(1)."""

    def test_runtime_error_produces_exit(self, tmp_path, capsys):
        secrets_dir = _make_secrets(tmp_path)

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent", side_effect=RuntimeError("compose crashed")),
            pytest.raises((SystemExit, typer.Exit)),
        ):
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

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent", side_effect=OSError("permission denied")),
            pytest.raises((SystemExit, typer.Exit)),
        ):
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

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch("ymir.cli.main.run_agent"),
        ):
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

        with (
            patch("ymir.cli.main.check_credentials"),
            patch("ymir.cli.main.start_services"),
            patch("ymir.cli.main.stop_services"),
            patch(
                "ymir.cli.main.run_agent",
                side_effect=subprocess.CalledProcessError(1, "podman"),
            ),
            pytest.raises((SystemExit, typer.Exit)),
        ):
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
