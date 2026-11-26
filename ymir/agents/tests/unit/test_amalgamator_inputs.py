import json
import os
import subprocess
from pathlib import Path

import pytest

from ymir.agents.amalgamator import inputs

# Synthetic commit SHAs for the URLs and API payloads below; named here so the
# secrets scanner does not read them as high-entropy strings.
GITLAB_SHA = "3e0dcf0b1d0c1f7c0f7a5c1a3f7d3f3e2b4b8c1d"  # pragma: allowlist secret
GITHUB_SHA = "1f5a2e9e1a2f0b6a3e4d5c6b7a8f9e0d1c2b3a4f"  # pragma: allowlist secret
GITWEB_SHA = "2c1b0e7d4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c"  # pragma: allowlist secret
MERGE_SHA = "ac7a27e1f034c06e6be590ce98c36db9f8f5eedc"  # pragma: allowlist secret

GITLAB_COMMIT_URL = f"https://gitlab.com/libtiff/libtiff/-/commit/{GITLAB_SHA}.patch"
GITHUB_PR_URL = "https://github.com/libexpat/libexpat/pull/1034.patch"
GITHUB_COMMIT_URL = f"https://github.com/nginx/nginx/commit/{GITHUB_SHA}.patch"
GITWEB_URL = f"http://thekelleys.org.uk/gitweb/?p=dnsmasq.git;a=patch;h={GITWEB_SHA}"
QT_DIFF_URL = "https://download.qt.io/archive/qt/6.10/CVE-2025-14576-qtdeclarative-6.10.diff"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (GITLAB_COMMIT_URL, "https://gitlab.com/libtiff/libtiff.git"),
        (GITHUB_PR_URL, "https://github.com/libexpat/libexpat.git"),
        (GITHUB_COMMIT_URL, "https://github.com/nginx/nginx.git"),
        (
            "https://gitlab.com/redhat/centos-stream/rpms/expat/-/merge_requests/42.patch",
            "https://gitlab.com/redhat/centos-stream/rpms/expat.git",
        ),
        (
            "https://gitlab.gnome.org/GNOME/glib/-/merge_requests/4321.patch",
            "https://gitlab.gnome.org/GNOME/glib.git",
        ),
        (
            "https://gitlab.com/redhat/console-services/teams/backend/-/commit/abc1234.patch",
            "https://gitlab.com/redhat/console-services/teams/backend.git",
        ),
        (GITWEB_URL, None),
        (QT_DIFF_URL, None),
        ("not a url", None),
    ],
)
def test_upstream_repo_url(url, expected):
    assert inputs.upstream_repo_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (GITLAB_COMMIT_URL, GITLAB_SHA),
        (GITHUB_COMMIT_URL, GITHUB_SHA),
        (GITWEB_URL, GITWEB_SHA),
        (GITWEB_URL.replace("h=", "id="), GITWEB_SHA),
        (GITHUB_PR_URL, None),
        (QT_DIFF_URL, None),
    ],
)
def test_fix_commit_from_patch_url(url, expected):
    assert inputs.fix_commit_from_patch_url(url) == expected


def test_read_spec_version_falls_back_to_the_version_line(tmp_path):
    spec = tmp_path / "libexpat.spec"
    spec.write_text("Name: libexpat\nVersion: 2.2.5\nRelease: 1%{?dist}\n")

    assert inputs.read_spec_version(spec) == "2.2.5"
    assert inputs.read_spec_version(tmp_path / "missing.spec") is None


def test_read_spec_version_expands_a_global_indirection(tmp_path):
    spec = tmp_path / "nodejs.spec"
    spec.write_text("%global major 22\nVersion: %{major}.11.0\n")

    assert inputs.read_spec_version(spec) == "22.11.0"


def test_read_spec_version_expands_the_expat_sed_idiom(tmp_path):
    # centos-stream expat 2.2.5 derives Version from %{unversion}.
    spec = tmp_path / "expat.spec"
    spec.write_text("%global unversion 2_2_5\nVersion: %(echo %{unversion} | sed 's/_/./g')\n")

    assert inputs.read_spec_version(spec) == "2.2.5"


def test_read_spec_version_leaves_unknown_macros_unresolved(tmp_path):
    spec = tmp_path / "weird.spec"
    spec.write_text("Version: %{undefined_macro}\n")

    assert inputs.read_spec_version(spec) is None


def test_read_spec_version_ignores_a_comment_after_a_macro_definition(tmp_path):
    spec = tmp_path / "expat.spec"
    spec.write_text("%global unversion 2_2_5  # release series\nVersion: %{unversion}\n")

    assert inputs.read_spec_version(spec) == "2_2_5"


@pytest.mark.parametrize(
    ("url", "api_url", "payload", "expected"),
    [
        (
            GITHUB_PR_URL,
            "https://api.github.com/repos/libexpat/libexpat/pulls/1034",
            {
                "merge_commit_sha": MERGE_SHA,
                "merged_at": "2026-01-15T10:00:00Z",
            },
            MERGE_SHA,
        ),
        (
            # An open PR has a merge_commit_sha field but no merge commit yet.
            GITHUB_PR_URL,
            "https://api.github.com/repos/libexpat/libexpat/pulls/1034",
            {"merge_commit_sha": "a" * 40, "merged_at": None},
            None,
        ),
        (
            "https://gitlab.com/redhat/centos-stream/rpms/expat/-/merge_requests/42",
            "https://gitlab.com/api/v4/projects/redhat%2Fcentos-stream%2Frpms%2Fexpat/merge_requests/42",
            {"state": "merged", "merge_commit_sha": "b" * 40},
            "b" * 40,
        ),
        (
            "https://gitlab.com/redhat/centos-stream/rpms/expat/-/merge_requests/42",
            "https://gitlab.com/api/v4/projects/redhat%2Fcentos-stream%2Frpms%2Fexpat/merge_requests/42",
            {"state": "merged", "merge_commit_sha": None, "squash_commit_sha": "c" * 40},
            "c" * 40,
        ),
        (
            "https://gitlab.com/redhat/centos-stream/rpms/expat/-/merge_requests/42",
            "https://gitlab.com/api/v4/projects/redhat%2Fcentos-stream%2Frpms%2Fexpat/merge_requests/42",
            {"state": "opened", "merge_commit_sha": "b" * 40},
            None,
        ),
        (GITHUB_COMMIT_URL, None, {}, None),
    ],
)
def test_merge_commit_from_pr_url(monkeypatch, url, api_url, payload, expected):
    seen = []

    def fake_fetch(target, timeout=30):
        seen.append(target)
        return payload

    monkeypatch.setattr(inputs, "_fetch_json", fake_fetch)

    assert inputs.merge_commit_from_pr_url(url) == expected
    if api_url is None:
        assert seen == []
    else:
        assert seen == [api_url]


def test_resolve_fix_commit_prefers_a_commit_named_by_the_url(monkeypatch):
    def fail_fetch(*args, **kwargs):
        raise AssertionError("the forge API must not be queried for commit URLs")

    monkeypatch.setattr(inputs, "_fetch_json", fail_fetch)

    assert inputs.resolve_fix_commit(GITHUB_COMMIT_URL) == GITHUB_SHA


def test_resolve_fix_commit_reports_none_when_the_forge_has_no_merge_commit(monkeypatch):
    monkeypatch.setattr(inputs, "_fetch_json", lambda *a, **k: {})

    assert inputs.resolve_fix_commit(GITHUB_PR_URL) is None


def test_read_jira_summary_reads_the_extensionless_mock_file(tmp_path):
    (tmp_path / "RHEL-15216").write_text(
        json.dumps({"fields": {"summary": "Segmentation fault occurs in dnsmasq-2.79-26 in RHEL8"}})
    )

    assert inputs.read_jira_summary("RHEL-15216", tmp_path) == (
        "Segmentation fault occurs in dnsmasq-2.79-26 in RHEL8"
    )
    assert inputs.read_jira_summary("RHEL-99999", tmp_path) is None


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


@pytest.fixture
def upstream_repo(tmp_path):
    repo = tmp_path / "libexpat-upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("expat\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "release 2.2.5")
    _git(repo, "tag", "v2.2.5")
    (repo / "README").write_text("expat, newer\n")
    _git(repo, "commit", "-q", "-am", "mainline")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return repo


def test_resolve_base_commit_uses_the_agent_tag_conventions(upstream_repo):
    sha = inputs.resolve_base_commit(upstream_repo, "2.2.5", "libexpat")

    assert sha is not None
    assert len(sha) == 40
    assert (
        sha
        != subprocess.run(
            ["git", "-C", str(upstream_repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )


def test_resolve_base_commit_accepts_package_suffixed_tags(tmp_path):
    repo = tmp_path / "libtiff-upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    (repo / "README").write_text("tiff\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "release")
    _git(repo, "tag", "libtiff-4_5_0")

    assert inputs.resolve_base_commit(repo, "4.5.0", "libtiff") is not None


def test_resolve_base_commit_accepts_uppercase_underscore_tags(tmp_path):
    # libexpat tags releases as R_2_2_5; dnsmasq uses v2.79.
    repo = tmp_path / "libexpat-upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("expat\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "release")
    _git(repo, "tag", "R_2_2_5")

    assert inputs.resolve_base_commit(repo, "2.2.5", "libexpat") is not None


def test_resolve_base_commit_returns_none_without_a_matching_tag(upstream_repo):
    assert inputs.resolve_base_commit(upstream_repo, "9.9.9", "libexpat") is None


def test_default_branch_ref_follows_origin_head(upstream_repo):
    assert inputs.default_branch_ref(upstream_repo) == "origin/main"


def test_commit_exists_distinguishes_known_from_unknown_objects(upstream_repo):
    head = subprocess.run(
        ["git", "-C", str(upstream_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert inputs.commit_exists(upstream_repo, head)
    assert not inputs.commit_exists(upstream_repo, "0" * 40)


def test_fetch_commit_resolves_a_short_sha_by_refreshing_remote_refs(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "README").write_text("base\n")
    _git(origin, "add", "README")
    _git(origin, "commit", "-q", "-m", "base")
    _git(origin, "checkout", "-q", "-b", "topic")
    (origin / "README").write_text("fix\n")
    _git(origin, "commit", "-q", "-am", "fix")
    topic_sha = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "remote", "add", "origin", str(origin))
    # Fetch only main, so the topic commit is not available locally. A short
    # SHA is not a valid fetch refspec; the fallback fetches all remote refs.
    _git(work, "fetch", "-q", "origin", "main")

    assert not inputs.commit_exists(work, topic_sha)

    assert inputs.fetch_commit(work, topic_sha[:7])
    assert inputs.commit_exists(work, topic_sha)


def test_fetch_commit_returns_false_when_the_remote_cannot_supply_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", str(tmp_path / "missing"))

    assert not inputs.fetch_commit(repo, "0" * 40)
