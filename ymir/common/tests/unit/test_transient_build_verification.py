"""Service failures must reach the existing retry handler, not a clarification."""

import pytest
from flexmock import flexmock

import ymir.common.utils as utils


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["candidate", "active", "rpms", "logs", "binary"])
async def test_transient_build_verification_failures_propagate(stage):
    async def search(*args, **kwargs):
        if "status in (Closed, Done, Resolved)" in kwargs.get("jql", ""):
            return [{"key": "RHEL-1", "fields": {"customfield_10578": "pkg-1-1.el9"}}]
        if stage == "active":
            return [{"key": "RHEL-2", "fields": {"customfield_10578": "pkg-2-1.el9"}}]
        return []

    package = {"name": "pkg", "epoch": 0, "version": "1", "release": "1.el9", "build_id": 1}
    fixed = {"name": "dep", "epoch": 1, "version": "1", "release": "1.el9", "build_id": 2}

    def build(url, nvr):
        if (stage == "candidate" and nvr.startswith("pkg-")) or (stage == "active" and nvr == "pkg-2-1.el9"):
            raise ConnectionError("test outage")
        return fixed if nvr.startswith("dep-") else package

    def rpms(buildID):
        if stage == "rpms":
            raise utils.koji.GenericError("test outage")
        return [{"name": "pkg" if buildID == 1 else "dep", "arch": "x86_64"}]

    async def logs(*args, **kwargs):
        if stage == "logs":
            raise utils.TransientInfrastructureError("test outage")
        return [("https://brew/logs/x86_64/installed_pkgs.log", "dep-1-1.el9.x86_64 1 2 abc installed\n")]

    flexmock(utils).should_receive("run_tool").replace_with(search)
    flexmock(utils).should_receive("_get_koji_build").replace_with(build)
    flexmock(utils).should_receive("_fetch_installed_pkgs_log").replace_with(logs)
    session = flexmock()
    session.should_receive("listRPMs").replace_with(rpms)
    session.should_receive("getRPM").and_raise(utils.koji.GenericError("test outage"))
    flexmock(utils.koji).should_receive("ClientSession").and_return(session)

    with pytest.raises(utils.TransientInfrastructureError, match="test outage"):
        await utils.check_package_built_with_fixed_dependency("pkg", "rhel-9.z", utils.BrewBuild(fixed), [])


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["closed", "active"])
async def test_jira_service_failures_propagate(scope):
    async def search(*args, **kwargs):
        closed = "status in (Closed, Done, Resolved)" in kwargs["jql"]
        if closed == (scope == "closed"):
            raise ConnectionError("test outage")
        return []

    flexmock(utils).should_receive("run_tool").replace_with(search)
    with pytest.raises(utils.TransientInfrastructureError, match="Jira query failed"):
        await utils.check_package_built_with_fixed_dependency(
            "pkg",
            "rhel-9.z",
            utils.BrewBuild({"name": "dep", "epoch": 1, "version": "1", "release": "1.el9"}),
            [],
        )


@pytest.mark.asyncio
async def test_buildroot_transport_error_is_retryable():
    import ymir.common.config as config

    async def rhel_config():
        return {}

    flexmock(config).should_receive("load_rhel_config").replace_with(rhel_config)
    flexmock(utils).should_receive("_resolve_buildroot_checks").and_return([("hub", "tag")])
    flexmock(utils).should_receive("_get_latest_koji_build").and_raise(utils.koji.GenericError("test outage"))
    build = utils.BrewBuild({"name": "dep", "epoch": 1, "version": "1", "release": "1.el9"})
    with pytest.raises(utils.TransientInfrastructureError, match="test outage"):
        await utils.check_build_in_buildroot("rhel-9", "dep", build.nevr)


@pytest.mark.asyncio
async def test_koji_generic_error_during_candidate_lookup_retries():
    """A koji.GenericError during a candidate lookup is an outage, not bad data.

    Unparseable NVRs are rejected before Brew is queried, so any exception from
    the lookup itself (including GenericError) must reach the retry handler
    instead of producing a clarification ticket.
    """
    fixed = {"name": "dep", "epoch": 1, "version": "1", "release": "1.el9", "build_id": 2}

    async def search(*args, **kwargs):
        if "status in (Closed, Done, Resolved)" in kwargs.get("jql", ""):
            return [{"key": "RHEL-1", "fields": {"customfield_10578": "pkg-1-1.el9"}}]
        return []

    def build(_url, _nvr):
        raise utils.koji.GenericError("brew hub temporarily unavailable")

    flexmock(utils).should_receive("run_tool").replace_with(search)
    flexmock(utils).should_receive("_get_koji_build").replace_with(build)

    with pytest.raises(utils.TransientInfrastructureError, match="brew hub temporarily unavailable"):
        await utils.check_package_built_with_fixed_dependency("pkg", "rhel-9.z", utils.BrewBuild(fixed), [])
