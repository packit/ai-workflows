from flexmock import flexmock

from ymir.mr_review import gate


def _run_gate(monkeypatch, tmp_path):
    """Run the gate with the rules repo clone replaced by a local fixture."""
    monkeypatch.setenv("CI_PROJECT_NAME", "testpkg")
    flexmock(gate).should_receive("clone_rules_repo").replace_with(
        lambda project, dest: _populate_rules(dest, tmp_path)
    )
    return gate.main()


def _populate_rules(dest, tmp_path):
    """Copy the fixture rules repo into the clone destination."""
    import shutil

    shutil.copytree(tmp_path / "rules", dest, dirs_exist_ok=True)
    return True


def _write_config(tmp_path, content):
    (tmp_path / "rules").mkdir(exist_ok=True)
    (tmp_path / "rules" / "ymir.yaml").write_text(content)


def test_off_label_wins_over_on(monkeypatch):
    monkeypatch.setenv("CI_MERGE_REQUEST_LABELS", f"{gate.LABEL_ON},{gate.LABEL_OFF}")
    assert gate.main() == 1


def test_on_label_forces_enabled(monkeypatch):
    monkeypatch.delenv("CI_MERGE_REQUEST_LABELS", raising=False)
    # Even a missing rules repo must not matter for the label path.
    flexmock(gate).should_receive("clone_rules_repo").and_return(False).never()
    monkeypatch.setenv("CI_MERGE_REQUEST_LABELS", f"other-label,{gate.LABEL_ON}")
    assert gate.main() == 0


def test_off_label_disables(monkeypatch):
    monkeypatch.setenv("CI_MERGE_REQUEST_LABELS", gate.LABEL_OFF)
    assert gate.main() == 1


def test_no_project_name(monkeypatch):
    monkeypatch.delenv("CI_MERGE_REQUEST_LABELS", raising=False)
    monkeypatch.delenv("CI_PROJECT_NAME", raising=False)
    assert gate.main() == 1


def test_clone_failure_means_not_opted_in(monkeypatch):
    monkeypatch.delenv("CI_MERGE_REQUEST_LABELS", raising=False)
    monkeypatch.setenv("CI_PROJECT_NAME", "no-such-package-xyz")
    flexmock(gate).should_receive("clone_rules_repo").and_return(False)
    assert gate.main() == 1


def test_missing_config(monkeypatch, tmp_path):
    (tmp_path / "rules").mkdir()
    assert _run_gate(monkeypatch, tmp_path) == 1


def test_enabled_config(monkeypatch, tmp_path):
    _write_config(tmp_path, "review:\n  enabled: true\n")
    assert _run_gate(monkeypatch, tmp_path) == 0


def test_flow_style_enabled(monkeypatch, tmp_path):
    _write_config(tmp_path, "review: {enabled: true}\n")
    assert _run_gate(monkeypatch, tmp_path) == 0


def test_enabled_false(monkeypatch, tmp_path):
    _write_config(tmp_path, "review:\n  enabled: false\n")
    assert _run_gate(monkeypatch, tmp_path) == 1


def test_enabled_must_be_boolean_true(monkeypatch, tmp_path):
    _write_config(tmp_path, 'review:\n  enabled: "true"\n')
    assert _run_gate(monkeypatch, tmp_path) == 1


def test_wrong_section(monkeypatch, tmp_path):
    _write_config(tmp_path, "other:\n  enabled: true\n")
    assert _run_gate(monkeypatch, tmp_path) == 1


def test_malformed_yaml_fails_closed(monkeypatch, tmp_path):
    _write_config(tmp_path, "review: [\n")
    assert _run_gate(monkeypatch, tmp_path) == 1


def test_second_top_level_section(monkeypatch, tmp_path):
    _write_config(tmp_path, "other:\n  enabled: true\nreview:\n  enabled: true\n")
    assert _run_gate(monkeypatch, tmp_path) == 0


def test_clone_url_anonymous_and_token(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    assert gate.clone_url("pkg") == "https://gitlab.com/redhat/centos-stream/rules/pkg.git"
    monkeypatch.setenv("GITLAB_TOKEN", "sekrit")
    assert gate.clone_url("pkg") == "https://oauth2:sekrit@gitlab.com/redhat/centos-stream/rules/pkg.git"


def test_clone_discards_output(monkeypatch):
    """The clone must not leak its command line (it can embed a token)."""

    class Result:
        returncode = 0

    captured = {}
    flexmock(gate.subprocess).should_receive("run").replace_with(
        lambda args, **kwargs: captured.update(args=args, kwargs=kwargs) or Result()
    )
    monkeypatch.setenv("GITLAB_TOKEN", "sekrit")
    assert gate.clone_rules_repo("pkg", "/tmp/dest")
    assert captured["args"][0:4] == ["git", "clone", "--depth", "1"]
    assert captured["args"][4] == "https://oauth2:sekrit@gitlab.com/redhat/centos-stream/rules/pkg.git"
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["check"] is False
