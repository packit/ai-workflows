import os
import subprocess
import sys


def test_backport_and_consolidation_import_without_jira_tools_or_flexmock():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys; "
            "sys.modules['flexmock'] = None; "
            "sys.modules['ymir.tools.privileged.jira'] = None; "
            "[importlib.import_module(name) for name in "
            "('ymir.agents.backport_agent', 'ymir.agents.rebase_consolidation', "
            "'ymir.agents.rebuild_consolidation')]",
        ],
        env={**os.environ, "MOCK_JIRA": "true", "DRY_RUN": "true"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
