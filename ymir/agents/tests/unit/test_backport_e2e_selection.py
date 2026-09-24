from types import SimpleNamespace

import pytest

from ymir.agents.tests.e2e.backport_agent import test_backport as backport_e2e


def _request_for_cases(cases, selection):
    items = [
        SimpleNamespace(
            callspec=SimpleNamespace(params={"test_case": case}),
            iter_markers=lambda name, skipped=(index == 1 and selection == "skipped"): (
                [pytest.mark.skip()] if skipped and name == "skip" else []
            ),
        )
        for index, case in enumerate(cases)
        if index == 0 or selection != "deselected"
    ]
    return SimpleNamespace(session=SimpleNamespace(items=items), config=SimpleNamespace(stash={}))


@pytest.mark.parametrize("selection", ["skipped", "deselected", "selected"])
@pytest.mark.parametrize("bad_ref", [None, "", "conflicting-ref"])
def test_build_ref_validation_only_checks_selected_cases(monkeypatch, selection, bad_ref):
    configs = {
        "RHEL-1": {
            "input": {
                "jira_issue": "RHEL-1",
                "package": "curl",
                "dist_git_branch": "rhel-9.8.0",
            },
            "zstream_build_ref": "fixed-ref",
        },
        "RHEL-2": {
            "input": {
                "jira_issue": "RHEL-2",
                "package": "curl",
                "dist_git_branch": "rhel-9.8.0",
            },
        },
    }
    if bad_ref is not None:
        configs["RHEL-2"]["zstream_build_ref"] = bad_ref
    cases = [backport_e2e.BackportAgentTestCase(config) for config in configs.values()]
    monkeypatch.setattr(backport_e2e, "test_cases", cases)
    ran = []

    async def run(case):
        ran.append(case.jira_issue)
        for lookup in (
            backport_e2e.agent_tasks.get_latest_candidate_build,
            backport_e2e.agent_tasks.get_latest_z_pending_build,
        ):
            _, ref = await lookup(case.input["package"], case.input["dist_git_branch"])
            assert ref == "fixed-ref"

    monkeypatch.setattr(backport_e2e.BackportAgentTestCase, "run", run)
    request = _request_for_cases(cases, selection)
    runner = backport_e2e.run_test_cases_concurrently.__wrapped__(request, configs)

    if selection == "selected":
        with pytest.raises(ValueError, match=r"zstream_build_ref|conflicting z-stream build refs"):
            next(runner)
        assert ran == []
    else:
        try:
            next(runner)
            assert ran == ["RHEL-1"]
        finally:
            runner.close()


@pytest.mark.parametrize("selection", ["skipped", "deselected", "selected"])
def test_repository_setup_only_prepares_selected_cases(monkeypatch, tmp_path, selection):
    configs = {
        issue: {
            "input": {"jira_issue": issue},
            "repos": [{"fixture": issue}],
            "zstream_override": {"9": "rhel-9.8.z"},
        }
        for issue in ("RHEL-1", "RHEL-2")
    }
    cases = [backport_e2e.BackportAgentTestCase(config) for config in configs.values()]
    monkeypatch.setattr(backport_e2e, "test_cases", cases)
    monkeypatch.setattr(backport_e2e, "SHARED_BARE_REPOS_DIR", tmp_path / "mock_bare")
    monkeypatch.setattr(backport_e2e, "load_all_fixture_configs", lambda _: configs)
    monkeypatch.setattr(backport_e2e, "cleanup_mock_gitconfig", lambda: None)
    prepared = []

    def setup(repos, issue, base_dir):
        if issue == "RHEL-2":
            raise RuntimeError("Unavailable repository for RHEL-2")
        prepared.append(issue)

    monkeypatch.setattr(backport_e2e, "setup_mock_repos", setup)
    request = _request_for_cases(cases, selection)
    runner = backport_e2e.mock_centos_stream_repos.__wrapped__(request)
    if selection == "selected":
        with pytest.raises(RuntimeError, match="Unavailable repository for RHEL-2"):
            next(runner)
    else:
        try:
            assert next(runner) == {"RHEL-1": configs["RHEL-1"]}
            assert prepared == ["RHEL-1"]
            assert cases[0].zstream_override == {"9": "rhel-9.8.z"}
            assert cases[1].zstream_override is None
            with pytest.raises(StopIteration):
                next(runner)
        finally:
            runner.close()
