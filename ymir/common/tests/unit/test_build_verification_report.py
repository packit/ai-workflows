"""Final Brew metadata problems preserve every candidate for clarification."""

import pytest
from flexmock import flexmock
from specfile.utils import NEVR

import ymir.common.utils as utils


@pytest.mark.asyncio
async def test_candidate_failures_are_collected_before_requesting_clarification(caplog):
    closed = [("RHEL-1", "pkg-1-1.el9"), ("RHEL-2", "pkg-2-1.el9"), ("RHEL-3", "pkg-3-1.el9")]
    active = [("RHEL-4", "pkg-4-1.el9")]

    async def candidates(*args):
        return closed, active

    flexmock(utils).should_receive("_find_completed_builds_jira").replace_with(candidates)
    metadata = {
        "pkg-1-1.el9": {"name": "pkg", "version": "1", "release": "1.el9"},
        "pkg-2-1.el9": {"name": "pkg", "epoch": "invalid", "version": "2", "release": "1.el9"},
        "pkg-3-1.el9": {"name": "pkg", "epoch": 2, "version": "3", "release": "1.el9"},
    }
    for nvr, build in metadata.items():
        flexmock(utils).should_receive("_get_koji_build").with_args(utils.BREWHUB_URL, nvr).and_return(
            build
        ).once()
    flexmock(utils).should_receive("_get_koji_build").with_args(utils.BREWHUB_URL, "pkg-4-1.el9").and_return(
        {"name": "pkg", "epoch": "invalid", "version": "4", "release": "1.el9"}
    ).once()

    fixed = NEVR.from_string("dep-1-1.el9")
    fixed_before = (fixed.name, fixed.epoch, fixed.version, fixed.release)
    result = await utils.check_package_built_with_fixed_dependency("pkg", "rhel-9.z", "dep", fixed, [])

    assert result[0] is None
    report = result[3]
    assert isinstance(report, utils.BuildVerificationReport)
    details = report.describe()
    for key, nvr in closed + active:
        assert key in details
        assert nvr in details
    assert "epoch" in details
    assert "Invalid Brew epoch" in details
    assert "pkg-2:3-1.el9" in details
    assert (fixed.name, fixed.epoch, fixed.version, fixed.release) == fixed_before
    assert "RHEL-1" in caplog.text
    assert "RHEL-2" in caplog.text
    assert "RHEL-4" in caplog.text


@pytest.mark.asyncio
async def test_log_failure_retries():
    closed = [("RHEL-1", "pkg-1-1.el9"), ("RHEL-2", "pkg-2-1.el9")]
    builds = {
        nvr: {"name": "pkg", "epoch": 0, "version": str(index), "release": "1.el9", "build_id": index}
        for index, (_, nvr) in enumerate(closed, 1)
    }
    builds["dep-1-1.el9"] = {"name": "dep", "epoch": 1, "version": "1", "release": "1.el9"}

    async def candidates(*args):
        return closed, []

    async def logs(*args, **kwargs):
        raise ConnectionError("Log server unavailable")

    flexmock(utils).should_receive("_find_completed_builds_jira").replace_with(candidates)
    flexmock(utils).should_receive("_get_koji_build").replace_with(lambda url, nvr: builds[nvr])
    flexmock(utils).should_receive("_fetch_installed_pkgs_log").replace_with(logs)
    session = flexmock()
    session.should_receive("listRPMs").and_return([{"arch": "x86_64"}])
    flexmock(utils.koji).should_receive("ClientSession").and_return(session)

    with pytest.raises(utils.TransientInfrastructureError, match="Log server unavailable"):
        await utils.check_package_built_with_fixed_dependency(
            "pkg", "rhel-9.z", "dep", NEVR.from_string("dep-1-1.el9"), []
        )


@pytest.mark.asyncio
async def test_failed_closed_search_retries():
    async def search(*args, **kwargs):
        if "status in (Closed, Done)" in kwargs["jql"]:
            raise ConnectionError("Closed search unavailable")
        return [{"key": "RHEL-2", "fields": {"customfield_10578": "pkg-2-1.el9"}}]

    flexmock(utils).should_receive("run_tool").replace_with(search)
    flexmock(utils).should_receive("_get_koji_build").with_args(utils.BREWHUB_URL, "pkg-2-1.el9").never()
    with pytest.raises(utils.TransientInfrastructureError, match="Closed search unavailable"):
        await utils.check_package_built_with_fixed_dependency(
            "pkg", "rhel-9.z", "dep", NEVR.from_string("dep-1-1.el9"), []
        )


@pytest.mark.asyncio
async def test_parser_collects_all_binary_failures():
    async def resolve(brew_url, nevra, component):
        raise ConnectionError(f"Brew failed for {nevra.name}")

    flexmock(utils).should_receive("_resolve_binary_rpm_to_source_rpm").replace_with(resolve).twice()
    log = "dep-one-1-1.el9.x86_64 1 2 abc installed\ndep-two-1-1.el9.x86_64 1 2 abc installed\n"
    with pytest.raises(utils.TransientInfrastructureError) as exc:
        await utils._parse_dependency_from_installed_pkgs_log(
            log, "dep", ["dep-one", "dep-two"], utils.BREWHUB_URL
        )
    assert "dep-one" in str(exc.value)
    assert "dep-two" in str(exc.value)


@pytest.mark.asyncio
async def test_parser_does_not_ignore_an_unresolved_binary():
    async def resolve(brew_url, nevra, component):
        return None if nevra.name == "dep-one" else NEVR.from_string("dep-1-1.el9")

    flexmock(utils).should_receive("_resolve_binary_rpm_to_source_rpm").replace_with(resolve).twice()
    log = "dep-one-1-1.el9.x86_64 1 2 abc installed\ndep-two-1-1.el9.x86_64 1 2 abc installed\n"
    nevr, names = await utils._parse_dependency_from_installed_pkgs_log(
        log, "dep", ["dep-one", "dep-two"], utils.BREWHUB_URL
    )
    assert nevr is None
    assert names == ["dep-one", "dep-two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("epoch", ["invalid", -1, True, 1.5])
async def test_invalid_epoch_does_not_stop_later_candidates(epoch):
    candidates = [("RHEL-1", "pkg-1-1.el9"), ("RHEL-2", "pkg-2-1.el9")]
    for index, (_, nvr) in enumerate(candidates, 1):
        metadata = {
            "name": "pkg",
            "epoch": epoch if index == 1 else 2,
            "version": str(index),
            "release": "1.el9",
        }
        flexmock(utils).should_receive("_get_koji_build").with_args(utils.BREWHUB_URL, nvr).and_return(
            metadata
        ).once()
    report = await utils._select_highest_evr_build(candidates, "pkg")
    assert isinstance(report, utils.BuildVerificationReport)
    assert "Invalid Brew epoch" in report.describe()
    assert "RHEL-1" in report.describe()
    assert "pkg-2:2-1.el9" in report.describe()
