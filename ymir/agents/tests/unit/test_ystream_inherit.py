import subprocess

import pytest

from ymir.agents import ystream_inherit
from ymir.agents.ystream_inherit import (
    AlreadyInheritedError,
    BrewSource,
    InheritCandidateError,
    find_zstream_fix_commit,
    resolve_brew_source,
    resolves_keys,
    same_major_candidate,
    spec_matches_brew_version,
)
from ymir.common.models import ShippedZStreamCandidate


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


def test_spec_matches_brew_epoch_version(tmp_path):
    spec_path = tmp_path / "curl.spec"
    spec_path.write_text("Name: curl\nEpoch: 1\nVersion: 8.0.1\nRelease: 2%{?dist}\n")
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
async def test_find_zstream_fix_commit_in_diverged_history(history_repo):
    repo, y_head, fix, z_head = history_repo

    assert await find_zstream_fix_commit(repo, y_head, z_head, "RHEL-123") == fix


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
