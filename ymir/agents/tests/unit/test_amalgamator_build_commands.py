import json

from ymir.agents.tests.e2e.backport_agent import amalgamator_arm


def test_shipped_commands_are_autotools_for_expat():
    """The bundled commands must build expat the way RHEL's spec does.

    The CMake build is broken in both expat trees by RHEL's downstream
    patches, so CMake commands made Amalgamator repair unrelated breakage
    before it could start.
    """
    for issue in ("RHEL-114618", "RHEL-114643"):
        commands = amalgamator_arm.build_commands_for(issue)
        assert "configure --without-docbook" in commands["build"]
        assert "cmake" not in commands["test"].lower()


def test_unknown_issue_gets_no_commands(monkeypatch, tmp_path):
    """nginx has no entry on purpose: its patched code needs PCRE1."""
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"RHEL-1": {"build": "make", "test": "make check"}}))
    monkeypatch.setenv("AMALGAMATOR_BUILD_COMMANDS", str(path))

    assert amalgamator_arm.build_commands_for("RHEL-178684") == {}


def test_env_var_overrides_shipped_file(monkeypatch, tmp_path):
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"RHEL-114618": {"build": "override", "test": ""}}))
    monkeypatch.setenv("AMALGAMATOR_BUILD_COMMANDS", str(path))

    assert amalgamator_arm.build_commands_for("RHEL-114618") == {
        "build": "override",
        "test": "",
    }


def test_missing_file_warns_and_returns_nothing(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("AMALGAMATOR_BUILD_COMMANDS", str(tmp_path / "absent.json"))

    with caplog.at_level("WARNING"):
        assert amalgamator_arm.build_commands_for("RHEL-114618") == {}
    assert "verify will run nothing" in caplog.text
