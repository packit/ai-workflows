import subprocess

import pytest
from specfile import Specfile

from ymir.agents import ystream_inherit
from ymir.agents.ystream_inherit import (
    AlreadyInheritedError,
    BrewSource,
    ImmutablePatchError,
    InheritCandidateError,
    apply_zstream_change,
    ensure_single_ymir_attribution,
    find_zstream_fix_commit,
    inspect_commit_files,
    reset_inherit_attempt,
    resolve_brew_source,
    resolves_keys,
    rewrite_commit_message,
    same_major_candidate,
    spec_matches_brew_version,
    validate_inherited_adaptation,
    verify_inherited_patches,
)
from ymir.common.models import ShippedZStreamCandidate
from ymir.common.utils import get_all_patches


def _candidate(key: str, nvr: str, *fix_versions: str) -> ShippedZStreamCandidate:
    return ShippedZStreamCandidate(
        issue_key=key,
        fixed_in_build=nvr,
        fix_versions=list(fix_versions),
    )


@pytest.mark.asyncio
async def test_resolve_brew_source(monkeypatch):
    monkeypatch.setattr(
        ystream_inherit,
        "_get_koji_build",
        lambda _url, _nvr: {
            "name": "curl",
            "epoch": 1,
            "version": "8.0.1",
            "source": f"git+https://gitlab.com/redhat/rhel/rpms/curl#{'a' * 40}",
        },
    )

    source = await resolve_brew_source("curl-8.0.1-2.el9_7", "curl")

    assert source == BrewSource(
        nvr="curl-8.0.1-2.el9_7",
        repository_url="https://gitlab.com/redhat/rhel/rpms/curl",
        commit_sha="a" * 40,
        epoch=1,
        version="8.0.1",
    )


@pytest.mark.parametrize(
    "source",
    [
        None,
        "https://gitlab.com/redhat/rhel/rpms/curl#abc",
        f"git+http://gitlab.com/redhat/rhel/rpms/curl#{'a' * 40}",
        f"git+https://example.com/redhat/rhel/rpms/curl#{'a' * 40}",
        f"git+https://gitlab.com/redhat/rhel/rpms/wget#{'a' * 40}",
    ],
)
@pytest.mark.asyncio
async def test_resolve_brew_source_rejects_untrusted_source(monkeypatch, source):
    monkeypatch.setattr(
        ystream_inherit,
        "_get_koji_build",
        lambda _url, _nvr: {
            "name": "curl",
            "version": "8.0.1",
            "source": source,
        },
    )

    with pytest.raises(InheritCandidateError):
        await resolve_brew_source("curl-8.0.1-2.el9_7", "curl")


def test_same_major_candidate_selects_target_major():
    candidates = [
        _candidate("RHEL-810", "curl-1-1.el8_10", "rhel-8.10.z"),
        _candidate("RHEL-102", "curl-1-1.el10_2", "rhel-10.2.z"),
        _candidate("RHEL-97", "curl-1-1.el9_7", "rhel-9.7.z"),
    ]

    assert same_major_candidate(candidates, "rhel-9.8") == candidates[2]


def test_same_major_candidate_rejects_multiple_sources_for_target_major():
    candidates = [
        _candidate("RHEL-96", "curl-1-1.el9_6", "rhel-9.6.z"),
        _candidate("RHEL-97", "curl-1-1.el9_7", "rhel-9.7.z"),
    ]

    assert same_major_candidate(candidates, "rhel-9.8") is None


def test_same_major_candidate_rejects_ambiguous_fix_versions():
    candidate = _candidate(
        "RHEL-97",
        "curl-1-1.el9_7",
        "rhel-9.7.z",
        "rhel-9.6.z",
    )

    assert same_major_candidate([candidate], "rhel-9.8") is None


def test_spec_matches_brew_epoch_version(tmp_path):
    spec_path = tmp_path / "curl.spec"
    spec_path.write_text(
        "Name: curl\n"
        "Epoch: 1\n"
        "Version: 8.0.1\n"
        "Release: 2%{?dist}\n"
        "Summary: test\n"
        "License: MIT\n"
        "\n%description\ntest\n"
    )
    source = BrewSource(
        nvr="curl-8.0.1-2.el9_7",
        repository_url="https://gitlab.com/redhat/rhel/rpms/curl",
        commit_sha="a" * 40,
        epoch=1,
        version="8.0.1",
    )

    assert spec_matches_brew_version(spec_path, source)
    assert not spec_matches_brew_version(
        spec_path,
        source.model_copy(update={"version": "8.1.0"}),
    )
    assert not spec_matches_brew_version(
        spec_path,
        source.model_copy(update={"epoch": 0}),
    )


def test_resolves_keys_matches_exact_footer_keys():
    assert resolves_keys("Fix curl\n\nResolves: RHEL-1, RHEL-10\nRelated: RHEL-20") == {
        "RHEL-1",
        "RHEL-10",
    }


def _git(repo, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo, filename, content, message):
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history_repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "ymir@example.com")
    _git(tmp_path, "config", "user.name", "Ymir")
    base = _commit(tmp_path, "package.spec", "Version: 1\n", "Base")
    _git(tmp_path, "branch", "y", base)
    _git(tmp_path, "branch", "z", base)
    _git(tmp_path, "checkout", "z")
    fix = _commit(tmp_path, "fix.patch", "patch\n", "Fix CVE\n\nResolves: RHEL-123")
    z_head = _commit(tmp_path, "notes", "build\n", "Build")
    _git(tmp_path, "checkout", "y")
    y_head = _commit(tmp_path, "y-change", "change\n", "Y change")
    return tmp_path, y_head, fix, z_head


@pytest.mark.asyncio
async def test_find_zstream_fix_commit_in_diverged_history(history_repo, monkeypatch):
    repo, y_head, fix, z_head = history_repo
    original_check_subprocess = ystream_inherit.check_subprocess
    log_commands = []

    async def track_git_log(command, **kwargs):
        if command[:2] == ["git", "log"]:
            log_commands.append(command)
        return await original_check_subprocess(command, **kwargs)

    monkeypatch.setattr(ystream_inherit, "check_subprocess", track_git_log)

    assert await find_zstream_fix_commit(repo, y_head, z_head, "RHEL-123") == fix
    assert len(log_commands) == 1


@pytest.mark.asyncio
async def test_find_zstream_fix_commit_rejects_multi_issue_commit(history_repo):
    repo, y_head, _fix, _z_head = history_repo
    _git(repo, "checkout", "z")
    multi_fix = _commit(
        repo,
        "multi.patch",
        "patch\n",
        "Squashed fixes\n\nResolves: RHEL-123, RHEL-456",
    )

    with pytest.raises(InheritCandidateError, match="other Jira"):
        await find_zstream_fix_commit(repo, y_head, multi_fix, "RHEL-123")


@pytest.mark.asyncio
async def test_find_zstream_fix_commit_rejects_already_inherited(history_repo):
    repo, _y_head, _fix, z_head = history_repo

    with pytest.raises(AlreadyInheritedError):
        await find_zstream_fix_commit(repo, z_head, z_head, "RHEL-123")


def _spec(patches: str, prep: str) -> str:
    return f"""Name: package
Version: 1
Release: 1
Summary: test
License: MIT
Source0: package.tar
{patches}

%description
test

%prep
{prep}

%changelog
"""


@pytest.mark.asyncio
async def test_apply_zstream_change_and_cleanup(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "ymir@example.com")
    _git(tmp_path, "config", "user.name", "Ymir")
    base_spec = _spec("", "%autosetup -p1")
    base = _commit(tmp_path, "package.spec", base_spec, "Base")
    _git(tmp_path, "checkout", "-b", "z")
    (tmp_path / "package.spec").write_text(_spec("Patch0: cve.patch", "%autosetup -p1"))
    (tmp_path / "cve.patch").write_text("fix\n")
    _git(tmp_path, "add", "package.spec", "cve.patch")
    _git(tmp_path, "commit", "-m", "Fix CVE\n\nResolves: RHEL-123")
    fix = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-b", "y", base)

    result = await apply_zstream_change(tmp_path, "package", fix)

    assert result.changed_files == ["package.spec", "cve.patch"]
    assert result.patch_files == ["cve.patch"]
    assert result.patch_blob_ids == {"cve.patch": _git(tmp_path, "rev-parse", f"{fix}:cve.patch")}
    assert "Patch0: cve.patch" in result.source_spec_diff
    assert (tmp_path / "cve.patch").read_text() == "fix\n"
    with Specfile(tmp_path / "package.spec") as spec:
        assert list(get_all_patches(spec)) == []

    with pytest.raises(InheritCandidateError, match="active Patch declaration"):
        await validate_inherited_adaptation(tmp_path, "package", base, result)

    (tmp_path / "package.spec").write_text(_spec("Patch0: cve.patch", "%autosetup -N"))
    with pytest.raises(InheritCandidateError, match="applied exactly once"):
        await validate_inherited_adaptation(tmp_path, "package", base, result)

    (tmp_path / "package.spec").write_text(_spec("Patch0: cve.patch", "%autosetup -p1"))
    await validate_inherited_adaptation(tmp_path, "package", base, result)

    await reset_inherit_attempt(tmp_path, base, result.changed_files)
    assert not (tmp_path / "cve.patch").exists()
    assert _git(tmp_path, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_verify_inherited_patches_rejects_modified_patch(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "ymir@example.com")
    _git(tmp_path, "config", "user.name", "Ymir")
    base = _commit(tmp_path, "package.spec", _spec("", "%autosetup -p1"), "Base")
    _git(tmp_path, "checkout", "-b", "z")
    (tmp_path / "package.spec").write_text(_spec("Patch0: cve.patch", "%autosetup -p1"))
    (tmp_path / "cve.patch").write_text("original patch\n")
    _git(tmp_path, "add", "package.spec", "cve.patch")
    _git(tmp_path, "commit", "-m", "Fix CVE\n\nResolves: RHEL-123")
    fix = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-b", "y", base)
    change = await apply_zstream_change(tmp_path, "package", fix)

    (tmp_path / "cve.patch").write_text("adapted patch\n")

    with pytest.raises(ImmutablePatchError, match=r"cve\.patch"):
        await verify_inherited_patches(tmp_path, change)


@pytest.mark.asyncio
async def test_validate_spec_only_adaptation(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "ymir@example.com")
    _git(tmp_path, "config", "user.name", "Ymir")
    base = _commit(tmp_path, "package.spec", _spec("", "%autosetup -p1"), "Base")
    _git(tmp_path, "checkout", "-b", "z")
    spec = tmp_path / "package.spec"
    spec.write_text(spec.read_text().replace("Summary: test", "Summary: secured package"))
    _git(tmp_path, "add", "package.spec")
    _git(tmp_path, "commit", "-m", "Fix spec\n\nResolves: RHEL-123")
    fix = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-b", "y", base)
    change = await apply_zstream_change(tmp_path, "package", fix)

    assert change.patch_files == []
    assert "secured package" in change.source_spec_diff
    spec.write_text(spec.read_text().replace("Summary: test", "Summary: secured package"))
    await validate_inherited_adaptation(tmp_path, "package", base, change)


@pytest.mark.parametrize(
    ("old", "new", "protected_field"),
    [
        ("Name: package", "Name: other", "Name"),
        ("Name: package", "Name: package\nEpoch: 1", "Epoch"),
        ("Version: 1", "Version: 2", "Version"),
        ("Release: 1", "Release: 2", "Release"),
        ("Source0: package.tar", "Source0: other.tar", "Source"),
        ("%changelog\n", "%changelog\n- injected\n", "%changelog"),
    ],
)
@pytest.mark.asyncio
async def test_validate_adaptation_rejects_packaging_metadata_changes(
    tmp_path,
    old,
    new,
    protected_field,
):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "ymir@example.com")
    _git(tmp_path, "config", "user.name", "Ymir")
    base = _commit(tmp_path, "package.spec", _spec("", "%autosetup -p1"), "Base")
    change = ystream_inherit.IntegratedChange(
        commit_sha="a" * 40,
        commit_message="Fix",
        changed_files=["package.spec"],
        source_spec_diff="spec diff",
    )
    spec = tmp_path / "package.spec"
    spec.write_text(spec.read_text().replace(old, new))

    with pytest.raises(InheritCandidateError, match=protected_field):
        await validate_inherited_adaptation(tmp_path, "package", base, change)


@pytest.mark.asyncio
async def test_validate_adaptation_rejects_unexpected_file(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "ymir@example.com")
    _git(tmp_path, "config", "user.name", "Ymir")
    base = _commit(tmp_path, "package.spec", _spec("", "%autosetup -p1"), "Base")
    change = ystream_inherit.IntegratedChange(
        commit_sha="a" * 40,
        commit_message="Fix",
        changed_files=["package.spec"],
        source_spec_diff="spec diff",
    )
    (tmp_path / "unexpected").write_text("not allowed\n")

    with pytest.raises(InheritCandidateError, match="unsupported files"):
        await validate_inherited_adaptation(tmp_path, "package", base, change)


@pytest.mark.asyncio
async def test_reset_inherit_attempt_removes_known_ignored_build_tree(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "ymir@example.com")
    _git(tmp_path, "config", "user.name", "Ymir")
    head = _commit(tmp_path, ".gitignore", "build/\n", "Ignore build output")
    build_file = tmp_path / "build" / "nested" / "result"
    build_file.parent.mkdir(parents=True)
    build_file.write_text("generated\n")

    await reset_inherit_attempt(tmp_path, head, ["build/nested/result"])

    assert not (tmp_path / "build").exists()


@pytest.mark.asyncio
async def test_inspect_commit_files_rejects_sources(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "ymir@example.com")
    _git(tmp_path, "config", "user.name", "Ymir")
    _commit(tmp_path, "package.spec", _spec("", "%autosetup"), "Base")
    commit = _commit(tmp_path, "sources", "SHA512 (source.tar) = abc\n", "Change source")

    with pytest.raises(InheritCandidateError, match="unsupported packaging file"):
        await inspect_commit_files(tmp_path, commit, "package")


def test_rewrite_commit_message_changes_only_exact_footer_reference():
    original = "Fix RHEL-123 in prose\n\nRelated: RHEL-1234\nResolves: RHEL-123"

    assert rewrite_commit_message(original, "RHEL-123", "RHEL-999") == (
        "Fix RHEL-123 in prose\n\nRelated: RHEL-1234\nResolves: RHEL-999"
    )


def test_ymir_attribution_is_added_when_source_was_not_created_by_ymir():
    assert ensure_single_ymir_attribution("Fix CVE\n\nResolves: RHEL-999") == (
        "Fix CVE\n\nResolves: RHEL-999\n\nAssisted-by: Ymir\n"
    )


def test_existing_ymir_attribution_is_not_duplicated():
    source_message = (
        "Fix CVE\n\n"
        "This commit was backported by Ymir, a Red Hat Enterprise Linux software maintenance "
        "AI agent.\n\n"
        "Assisted-by: Ymir\n"
    )

    assert ensure_single_ymir_attribution(source_message) == source_message
    assert ensure_single_ymir_attribution(source_message + "Assisted-by: Ymir\n") == source_message
