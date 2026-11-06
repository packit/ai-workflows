"""Final Brew metadata problems preserve every candidate for clarification."""

import pytest
from flexmock import flexmock
from specfile.utils import NEVR

import ymir.common.utils as utils


@pytest.mark.asyncio
async def test_candidate_failures_are_collected_before_requesting_clarification(caplog):
    closed = [("RHEL-1", "pkg-1-1.el9"), ("RHEL-2", "pkg-2-1.el9"), ("RHEL-3", "pkg-3-1.el9")]
    active = [("RHEL-4", "pkg-4-1.el9")]

    async def search(*args, **kwargs):
        issues = closed if "status in (Closed, Done, Resolved)" in kwargs.get("jql", "") else active
        return [{"key": key, "fields": {"customfield_10578": nvr}} for key, nvr in issues]

    flexmock(utils).should_receive("run_tool").replace_with(search)
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

    fixed_build = utils.BrewBuild({"name": "dep", "epoch": 1, "version": "1", "release": "1.el9"})
    result = await utils.check_package_built_with_fixed_dependency("pkg", "rhel-9.z", fixed_build, [])

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
    fixed_nevr = fixed_build.nevr
    assert (fixed_nevr.name, fixed_nevr.epoch, fixed_nevr.version, fixed_nevr.release) == (
        "dep",
        1,
        "1",
        "1.el9",
    )
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

    async def search(*args, **kwargs):
        if "status in (Closed, Done, Resolved)" in kwargs.get("jql", ""):
            return [{"key": key, "fields": {"customfield_10578": nvr}} for key, nvr in closed]
        return []

    async def logs(*args, **kwargs):
        raise ConnectionError("Log server unavailable")

    flexmock(utils).should_receive("run_tool").replace_with(search)
    flexmock(utils).should_receive("_get_koji_build").replace_with(lambda url, nvr: builds[nvr])
    flexmock(utils).should_receive("_fetch_installed_pkgs_log").replace_with(logs)
    session = flexmock()
    session.should_receive("listRPMs").and_return([{"arch": "x86_64"}])
    flexmock(utils.koji).should_receive("ClientSession").and_return(session)

    with pytest.raises(utils.TransientInfrastructureError, match="Log server unavailable"):
        await utils.check_package_built_with_fixed_dependency(
            "pkg", "rhel-9.z", utils.BrewBuild(builds["dep-1-1.el9"]), []
        )


@pytest.mark.asyncio
async def test_failed_closed_search_retries():
    async def search(*args, **kwargs):
        if "status in (Closed, Done, Resolved)" in kwargs["jql"]:
            raise ConnectionError("Closed search unavailable")
        return [{"key": "RHEL-2", "fields": {"customfield_10578": "pkg-2-1.el9"}}]

    flexmock(utils).should_receive("run_tool").replace_with(search)
    flexmock(utils).should_receive("_get_koji_build").with_args(utils.BREWHUB_URL, "pkg-2-1.el9").never()
    with pytest.raises(utils.TransientInfrastructureError, match="Closed search unavailable"):
        await utils.check_package_built_with_fixed_dependency(
            "pkg",
            "rhel-9.z",
            utils.BrewBuild({"name": "dep", "epoch": 1, "version": "1", "release": "1.el9"}),
            [],
        )


@pytest.mark.asyncio
async def test_parser_collects_all_binary_failures():
    async def resolve(brew_url, nevra, component):
        raise ConnectionError(f"Brew failed for {nevra.name}")

    flexmock(utils).should_receive("_get_source_nevr").replace_with(resolve).twice()
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

    flexmock(utils).should_receive("_get_source_nevr").replace_with(resolve).twice()
    log = "dep-one-1-1.el9.x86_64 1 2 abc installed\ndep-two-1-1.el9.x86_64 1 2 abc installed\n"
    nevr, names = await utils._parse_dependency_from_installed_pkgs_log(
        log, "dep", ["dep-one", "dep-two"], utils.BREWHUB_URL
    )
    assert nevr is None
    assert names == ["dep-one", "dep-two"]


def test_describe_caps_candidate_list_to_fit_jira_comment():
    """describe(max_chars=...) keeps the report within Jira's comment size limit."""
    report = utils.BuildVerificationReport(
        candidates=[(f"RHEL-{index}", f"pkg-{index}-1.el9", "closed") for index in range(500)],
        problems=["RHEL-1 (pkg-1-1.el9): build not found in Brew"],
    )

    full = report.describe()
    assert len(full) > 10_000

    capped = report.describe(max_chars=1_000)
    assert len(capped) <= 1_000
    assert capped.startswith("Referenced builds:")
    assert "RHEL-0" in capped  # leading candidates are kept
    assert "more build reference(s) omitted" in capped
    # Verification problems must survive the cap.
    assert "build not found in Brew" in capped


def test_describe_returns_full_report_without_cap():
    report = utils.BuildVerificationReport(
        candidates=[("RHEL-1", "pkg-1-1.el9", "closed")],
        active_problems=["RHEL-2 (pkg-2-1.el9): build not found in Brew"],
    )

    text = report.describe()

    assert "RHEL-1 (closed), Fixed in Build: pkg-1-1.el9" in text
    assert "Verification problems:" in text
    assert "RHEL-2 (pkg-2-1.el9): build not found in Brew" in text
    assert "omitted" not in text


@pytest.mark.asyncio
async def test_broken_cross_stream_candidate_does_not_block_rebuild():
    """A broken build from another release stream must not block a rebuild decision.

    The Z-stream ticket references an in-stream closed build and a Y-stream
    (.el10) build that is missing from Brew. The out-of-stream build would be
    ignored by the stream filter, so its lookup problem must not turn the
    rebuild decision into a clarification.
    """
    closed = [("RHEL-1", "pkg-1-1.el10_0"), ("RHEL-2", "pkg-2-1.el10")]
    builds = {
        "pkg-1-1.el10_0": {"name": "pkg", "epoch": 0, "version": "1", "release": "1.el10_0", "build_id": 1},
        # RHEL-2's build is missing from Brew (lookup returns None).
    }

    async def search(*args, **kwargs):
        if "status in (Closed, Done, Resolved)" in kwargs.get("jql", ""):
            return [{"key": key, "fields": {"customfield_10578": nvr}} for key, nvr in closed]
        return []

    async def logs(*args, **kwargs):
        return [("https://brew/logs/x86_64/installed_pkgs.log", "log")]

    async def names(*args, **kwargs):
        return ["dep"], 1, "dep"

    async def parse(*args):
        return NEVR(name="dep", epoch=0, version="1", release="1.el10_0"), ["dep"]

    flexmock(utils).should_receive("run_tool").replace_with(search)
    flexmock(utils).should_receive("_get_koji_build").replace_with(lambda _url, nvr: builds.get(nvr))
    flexmock(utils).should_receive("_fetch_installed_pkgs_log").replace_with(logs)
    flexmock(utils).should_receive("_get_known_package_names").replace_with(names)
    flexmock(utils).should_receive("_parse_dependency_from_installed_pkgs_log").replace_with(parse)
    session = flexmock()
    session.should_receive("listRPMs").with_args(buildID=1).and_return([{"arch": "x86_64"}])
    flexmock(utils.koji).should_receive("ClientSession").and_return(session)
    fixed = utils.BrewBuild({"name": "dep", "epoch": 0, "version": "2", "release": "1.el10_0"})

    result = await utils.check_package_built_with_fixed_dependency("pkg", "rhel-10.0.z", fixed, [])

    assert result == (False, "RHEL-1", utils._nevr_from_build(builds["pkg-1-1.el10_0"]), None)
