"""Service failures must reach the existing retry handler, not a clarification."""

import pytest
from flexmock import flexmock
from specfile.utils import NEVR

import ymir.common.utils as utils


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["candidate", "active", "missing", "fixed", "rpms", "logs", "binary"])
async def test_transient_build_verification_failures_propagate(stage):
    async def candidates(*args):
        active = [("RHEL-2", "pkg-2-1.el9")] if stage == "active" else []
        return [("RHEL-1", "pkg-1-1.el9")], active

    package = {"name": "pkg", "epoch": 0, "version": "1", "release": "1.el9", "build_id": 1}
    fixed = {"name": "dep", "epoch": 1, "version": "1", "release": "1.el9", "build_id": 2}

    def build(url, nvr):
        if (stage == "candidate" and nvr.startswith("pkg-")) or (stage == "fixed" and nvr.startswith("dep-")):
            raise ConnectionError("test outage")
        if stage == "active" and nvr == "pkg-2-1.el9":
            raise utils.TransientInfrastructureError("test outage")
        if stage == "missing":
            return None
        return fixed if nvr.startswith("dep-") else package

    def rpms(buildID):
        if stage == "rpms":
            raise utils.koji.GenericError("test outage")
        return [{"name": "pkg" if buildID == 1 else "dep", "arch": "x86_64"}]

    async def logs(*args, **kwargs):
        if stage == "logs":
            raise utils.TransientInfrastructureError("test outage")
        return [("https://brew/logs/x86_64/installed_pkgs.log", "dep-1-1.el9.x86_64 1 2 abc installed\n")]

    flexmock(utils).should_receive("_find_completed_builds_jira").replace_with(candidates)
    flexmock(utils).should_receive("_get_koji_build").replace_with(build)
    flexmock(utils).should_receive("_fetch_installed_pkgs_log").replace_with(logs)
    session = flexmock()
    session.should_receive("listRPMs").replace_with(rpms)
    session.should_receive("getRPM").and_raise(utils.koji.GenericError("test outage"))
    flexmock(utils.koji).should_receive("ClientSession").and_return(session)

    message = "not found in Brew" if stage == "missing" else "test outage"
    with pytest.raises(utils.TransientInfrastructureError, match=message):
        await utils.check_package_built_with_fixed_dependency(
            "pkg", "rhel-9.z", "dep", NEVR.from_string("dep-1-1.el9"), []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["closed", "active"])
async def test_jira_service_failures_propagate(scope):
    async def search(*args, **kwargs):
        closed = "status in (Closed, Done)" in kwargs["jql"]
        if closed == (scope == "closed"):
            raise ConnectionError("test outage")
        return []

    flexmock(utils).should_receive("run_tool").replace_with(search)
    with pytest.raises(utils.TransientInfrastructureError, match="Jira query failed"):
        await utils.check_package_built_with_fixed_dependency(
            "pkg", "rhel-9.z", "dep", NEVR.from_string("dep-1-1.el9"), []
        )


@pytest.mark.asyncio
async def test_retry_takes_priority_over_final_metadata_errors():
    for index in (1, 3):
        metadata = {
            "name": "pkg",
            "epoch": "invalid" if index == 1 else 2,
            "version": str(index),
            "release": "1.el9",
        }
        flexmock(utils).should_receive("_get_koji_build").with_args(
            utils.BREWHUB_URL, f"pkg-{index}-1.el9"
        ).and_return(metadata).once()
    flexmock(utils).should_receive("_get_koji_build").with_args(utils.BREWHUB_URL, "pkg-2-1.el9").and_raise(
        ConnectionError("test outage")
    ).once()

    with pytest.raises(utils.TransientInfrastructureError, match="test outage"):
        await utils._select_highest_evr_build(
            [(f"RHEL-{index}", f"pkg-{index}-1.el9") for index in (1, 2, 3)], "pkg"
        )


@pytest.mark.asyncio
async def test_buildroot_transport_error_is_retryable():
    import ymir.common.config as config

    async def rhel_config():
        return {}

    flexmock(config).should_receive("load_rhel_config").replace_with(rhel_config)
    flexmock(utils).should_receive("_resolve_buildroot_checks").and_return([("hub", "tag")])
    flexmock(utils).should_receive("_get_latest_koji_build").and_raise(utils.koji.GenericError("test outage"))
    build = utils.ResolvedBrewBuild({"name": "dep", "epoch": 1, "version": "1", "release": "1.el9"})
    with pytest.raises(utils.TransientInfrastructureError, match="test outage"):
        await utils.check_build_in_buildroot("rhel-9", "dep", build)
