"""Only authoritative Brew identities participate in version comparisons."""

import pytest
from flexmock import flexmock
from specfile.utils import NEVR

import ymir.common.utils as utils


@pytest.mark.asyncio
@pytest.mark.parametrize("epoch", [None, 0, 2])
async def test_resolve_brew_build_uses_authoritative_epoch(epoch):
    metadata = {"name": "pkg", "epoch": epoch, "version": "1", "release": "1.el9"}
    flexmock(utils).should_receive("_get_koji_build").with_args(utils.BREWHUB_URL, "pkg-1-1.el9").and_return(
        metadata
    ).once()

    build = await utils.resolve_brew_build("pkg-1-1.el9")

    assert build.nevr == NEVR(name="pkg", epoch=epoch or 0, version="1", release="1.el9")
    assert build.metadata == metadata


@pytest.mark.parametrize("converter", [utils._nevr_from_build, utils._evr_from_build])
def test_missing_epoch_is_incomplete_metadata(converter):
    with pytest.raises(KeyError, match="epoch"):
        converter({"name": "pkg", "version": "1", "release": "1.el9"})


@pytest.mark.asyncio
async def test_parser_requires_source_resolution():
    async def unresolved(*args):
        return None

    flexmock(utils).should_receive("_resolve_binary_rpm_to_source_rpm").with_args(
        utils.BREWHUB_URL, object, "perl"
    ).replace_with(unresolved)

    nevr, names = await utils._parse_dependency_from_installed_pkgs_log(
        "perl-Errno-1.37-1.el9.x86_64 1 2 abc installed\n",
        "perl",
        ["perl-Errno"],
        utils.BREWHUB_URL,
    )

    assert nevr is None
    assert names == ["perl-Errno"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_fixed_build", [True, False])
@pytest.mark.parametrize(
    "active_state", ["older", "newer", "missing", "outage", "incomplete", "wrong_package"]
)
async def test_active_candidates_reuse_metadata_and_preserve_unknown_builds(active_state, reuse_fixed_build):
    package_metadata = {"name": "pkg", "epoch": 2, "version": "1", "release": "1.el9", "build_id": 1}
    fixed_metadata = {"name": "dep", "epoch": 2, "version": "1", "release": "1.el9", "build_id": 2}
    used_nevr = NEVR(name="dep", epoch=1, version="9", release="1.el9")

    async def candidates(*args):
        return [("RHEL-1", "pkg-1-1.el9")], [("RHEL-2", "pkg-9-1.el9")]

    async def logs(*args, **kwargs):
        return [("https://brew/logs/x86_64/installed_pkgs.log", "log")]

    async def names(*args, **kwargs):
        return ["dep"], 1, utils._nevr_from_build(fixed_metadata), "dep"

    async def parse(*args):
        return used_nevr, ["dep"]

    flexmock(utils).should_receive("_find_completed_builds_jira").replace_with(candidates)
    flexmock(utils).should_receive("_fetch_installed_pkgs_log").replace_with(logs)
    flexmock(utils).should_receive("_get_known_package_names").replace_with(names)
    flexmock(utils).should_receive("_parse_dependency_from_installed_pkgs_log").replace_with(parse)
    flexmock(utils).should_receive("_get_koji_build").with_args(utils.BREWHUB_URL, "pkg-1-1.el9").and_return(
        package_metadata
    ).once()
    active_lookup = (
        flexmock(utils).should_receive("_get_koji_build").with_args(utils.BREWHUB_URL, "pkg-9-1.el9").once()
    )
    if active_state == "outage":
        active_lookup.and_raise(ConnectionError("Brew unavailable"))
    elif active_state == "missing":
        active_lookup.and_return(None)
    elif active_state == "incomplete":
        active_lookup.and_return({"name": "pkg", "version": "9", "release": "1.el9"})
    else:
        active_lookup.and_return(
            {
                "name": "other" if active_state == "wrong_package" else "pkg",
                "epoch": 1 if active_state == "older" else 2,
                "version": "9",
                "release": "1.el9",
            }
        )
    session = flexmock()
    session.should_receive("listRPMs").with_args(buildID=1).and_return([{"arch": "x86_64"}])
    flexmock(utils.koji).should_receive("ClientSession").and_return(session)
    fixed_build = utils.ResolvedBrewBuild(fixed_metadata)
    if reuse_fixed_build:
        fixed_dep_nevr = fixed_build.nevr
    else:
        # Jira's epochless NVR parses with epoch zero. Comparing it directly with
        # the installed epoch-one dependency would incorrectly report already fixed.
        fixed_dep_nevr = NEVR.from_string("dep-1-1.el9")
        flexmock(utils).should_receive("_get_koji_build").with_args(
            utils.BREWHUB_URL, "dep-1-1.el9"
        ).and_return(fixed_metadata).times(0 if active_state in ("missing", "outage", "incomplete") else 1)
    if active_state == "outage":
        with pytest.raises(utils.TransientInfrastructureError, match="Brew unavailable"):
            await utils.check_package_built_with_fixed_dependency(
                "pkg",
                "rhel-9.z",
                "dep",
                fixed_dep_nevr,
                [],
                fixed_dep_build=fixed_build if reuse_fixed_build else None,
            )
        return
    result = await utils.check_package_built_with_fixed_dependency(
        "pkg",
        "rhel-9.z",
        "dep",
        fixed_dep_nevr,
        [],
        fixed_dep_build=fixed_build if reuse_fixed_build else None,
    )

    assert result[2] == utils._nevr_from_build(package_metadata)
    if active_state in ("older", "wrong_package"):
        assert result == (False, "RHEL-1", result[2], None)
    else:
        assert result[:3] == (None, "RHEL-1", result[2])
        assert isinstance(result[3], utils.BuildVerificationReport)
        assert "RHEL-2" in result[3].describe()
        assert "pkg-9-1.el9" in result[3].describe()
        if active_state == "newer":
            assert result[3].reason == "active_builds_not_closed:RHEL-2"
        else:
            assert result[3].problems


@pytest.mark.asyncio
async def test_buildroot_reuses_resolved_brew_build():
    import ymir.common.config as config

    async def rhel_config():
        return {}

    flexmock(config).should_receive("load_rhel_config").replace_with(rhel_config)
    metadata = {"name": "pkg", "epoch": 2, "version": "1", "release": "1.el9"}
    flexmock(utils).should_receive("_get_koji_build").never()
    flexmock(utils).should_receive("_resolve_buildroot_checks").and_return([("hub", "tag")])
    flexmock(utils).should_receive("_get_latest_koji_build").with_args("hub", "tag", "pkg").and_return(
        metadata
    )

    assert await utils.check_build_in_buildroot("rhel-9", "pkg", utils.ResolvedBrewBuild(metadata))
