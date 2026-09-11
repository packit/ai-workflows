from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from openshift.scripts import deployment_release


def test_extract_release_notes_from_pr_description():
    description = """Some context.

RELEASE NOTES BEGIN
Added support for Java packages.
This also updates the build image.
RELEASE NOTES END
"""

    assert deployment_release.extract_release_notes(description) == (
        "Added support for Java packages.\nThis also updates the build image."
    )


def test_extract_release_notes_accepts_crlf_and_ignores_empty_values():
    description = "RELEASE NOTES BEGIN\r\nN/A\r\nRELEASE NOTES END\r\n"

    assert deployment_release.extract_release_notes(description) is None


def test_format_changelog_includes_multiline_notes_and_missing_prs():
    changelog = deployment_release.format_changelog(
        "deployed/20260910T100000Z-old",
        [
            {
                "number": 42,
                "url": "https://github.com/packit/ai-workflows/pull/42",
                "notes": "First line\nSecond line",
            }
        ],
        [43],
    )

    assert "Changes since `deployed/20260910T100000Z-old`:" in changelog
    assert "- First line ([#42](https://github.com/packit/ai-workflows/pull/42))" in changelog
    assert "  Second line" in changelog
    assert "PRs without a usable release-notes section: #43" in changelog


def test_deployment_base_requires_an_existing_deployed_tag(monkeypatch):
    monkeypatch.setattr(deployment_release, "latest_deployment_tag", lambda _repo, _remote: None)

    with pytest.raises(
        deployment_release.ReleaseError,
        match="no deployed/\\* tag exists on remote 'upstream'",
    ):
        deployment_release.deployment_base(Path("."), "upstream")


def test_latest_deployment_tag_ignores_local_only_tags(monkeypatch):
    remote_tag = "deployed/20260910T100000Z"
    local_only_tag = "deployed/20260911T100000Z"

    def fake_git(_repo, *arguments, **_kwargs):
        if arguments[:4] == ("ls-remote", "--tags", "--refs", "upstream"):
            return SimpleNamespace(stdout=f"tag-object refs/tags/{remote_tag}\n")
        if arguments[0] == "for-each-ref":
            return SimpleNamespace(stdout=f"{local_only_tag}\n{remote_tag}\n")
        raise AssertionError(arguments)

    monkeypatch.setattr(deployment_release, "git", fake_git)

    assert deployment_release.latest_deployment_tag(Path("."), "upstream") == remote_tag


def test_changelog_parser_defaults_head_to_upstream_main():
    args = deployment_release.build_parser().parse_args(["changelog", "deployed/20260910T100000Z"])

    assert args.command == "changelog"
    assert args.base == "deployed/20260910T100000Z"
    assert args.head is None
    assert args.remote == "upstream"


def test_print_changelog_resolves_refs_and_prints_result(monkeypatch, capsys):
    resolved_refs = []
    monkeypatch.setattr(deployment_release, "repository_root", lambda: Path("."))
    monkeypatch.setattr(
        deployment_release,
        "repository_remote",
        lambda _repo, remote: remote,
    )

    def fake_resolve_commit(_repo, ref):
        resolved_refs.append(ref)
        return f"{ref}-commit"

    monkeypatch.setattr(deployment_release, "resolve_commit", fake_resolve_commit)
    monkeypatch.setattr(
        deployment_release,
        "collect_release_notes",
        lambda _repo, _base, _head: ([], [], []),
    )
    monkeypatch.setattr(
        deployment_release,
        "format_changelog",
        lambda base, _prs, _missing: f"changelog from {base}",
    )

    deployment_release.print_changelog("deployed/base", None, "origin")

    assert resolved_refs == ["deployed/base", "origin/main"]
    assert capsys.readouterr().out == "changelog from deployed/base\n"


def test_associated_pull_requests_rejects_non_numeric_number(monkeypatch):
    monkeypatch.setattr(
        deployment_release,
        "github_api_get",
        lambda _path: [{"number": "not-a-number"}],
    )

    with pytest.raises(deployment_release.ReleaseError, match="non-numeric PR number"):
        deployment_release.associated_pull_requests("packit/ai-workflows", "a" * 40)


def test_github_api_get_uses_github_token_when_provided(monkeypatch):
    captured = {}

    class Response:
        def __init__(self):
            self.status = 200
            self.headers = {}

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, **kwargs):
        captured["request"] = request
        captured["kwargs"] = kwargs
        return Response()

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(deployment_release, "urlopen", fake_urlopen)

    assert deployment_release.github_api_get("/test") == {"ok": True}
    assert captured["request"].full_url == "https://api.github.com/test"
    assert captured["request"].get_header("Authorization") == "Bearer test-token"
    assert captured["kwargs"]["timeout"] == deployment_release.GITHUB_API_TIMEOUT


def test_github_api_get_is_anonymous_without_github_token(monkeypatch):
    captured = {}

    class Response:
        def __init__(self):
            self.status = 200
            self.headers = {}

        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, **kwargs):
        captured["request"] = request
        captured.update(kwargs)
        return Response()

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(deployment_release, "urlopen", fake_urlopen)

    assert deployment_release.github_api_get("/test") == []
    assert captured["request"].get_header("Authorization") is None


def test_collect_release_notes_uses_commit_pull_request_data(monkeypatch):
    commit = "a" * 40
    api_paths = []

    monkeypatch.setattr(
        deployment_release,
        "git",
        lambda _repo, *arguments, **_kwargs: SimpleNamespace(stdout=f"{commit}\n"),
    )

    def fake_api(path):
        api_paths.append(path)
        return [
            {
                "number": 42,
                "html_url": "https://github.com/packit/ai-workflows/pull/42",
                "body": "RELEASE NOTES BEGIN\nAdded a feature.\nRELEASE NOTES END",
            }
        ]

    monkeypatch.setattr(deployment_release, "github_api_get", fake_api)

    prs, missing_notes, commits_without_prs = deployment_release.collect_release_notes(
        Path("."), "base", "head"
    )

    assert api_paths == [f"/repos/packit/ai-workflows/commits/{commit}/pulls?per_page=100"]
    assert prs == [
        {
            "number": 42,
            "url": "https://github.com/packit/ai-workflows/pull/42",
            "notes": "Added a feature.",
        }
    ]
    assert missing_notes == []
    assert commits_without_prs == []


def test_deploy_runs_openshift_before_finalization(monkeypatch):
    context = object()
    events = []

    def fake_prepare(dry_run, remote):
        events.append(("prepare", dry_run, remote))
        return context

    def fake_finalize(received_context, remote):
        events.append(("finalize", received_context, remote))

    monkeypatch.setattr(deployment_release, "prepare", fake_prepare)
    monkeypatch.setattr(
        deployment_release,
        "run_openshift_deployment",
        lambda: events.append("openshift"),
    )
    monkeypatch.setattr(deployment_release, "finalize", fake_finalize)

    deployment_release.deploy(False, "upstream")

    assert events == [
        ("prepare", False, "upstream"),
        "openshift",
        ("finalize", context, "upstream"),
    ]


def test_deploy_dry_run_skips_openshift_and_finalization(monkeypatch):
    events = []
    monkeypatch.setattr(
        deployment_release,
        "prepare",
        lambda dry_run, remote: events.append(("prepare", dry_run, remote)),
    )
    monkeypatch.setattr(
        deployment_release,
        "run_openshift_deployment",
        lambda: events.append("openshift"),
    )
    monkeypatch.setattr(
        deployment_release,
        "finalize",
        lambda _context, _remote: events.append("finalize"),
    )

    deployment_release.deploy(True, "upstream")

    assert events == [("prepare", True, "upstream")]


def test_proposed_tag_name_is_timestamped_and_immutable(monkeypatch):
    monkeypatch.setattr(deployment_release, "local_tag_exists", lambda *_: False)
    monkeypatch.setattr(deployment_release, "remote_tag_exists", lambda *_: False)

    tag = deployment_release.proposed_tag_name(
        Path("."),
        "upstream",
        now=datetime(2026, 9, 10, 14, 30, 15, tzinfo=UTC),
    )

    assert tag == "deployed/20260910T143015Z"


def test_proposed_tag_name_adds_suffix_on_collision(monkeypatch):
    existing = {"deployed/20260910T143015Z"}
    monkeypatch.setattr(
        deployment_release,
        "local_tag_exists",
        lambda _repo, tag: tag in existing,
    )
    monkeypatch.setattr(deployment_release, "remote_tag_exists", lambda *_: False)

    tag = deployment_release.proposed_tag_name(
        Path("."),
        "upstream",
        now=datetime(2026, 9, 10, 14, 30, 15, tzinfo=UTC),
    )

    assert tag == "deployed/20260910T143015Z-2"


def test_proposed_tag_name_stops_after_collision_limit(monkeypatch):
    monkeypatch.setattr(deployment_release, "local_tag_exists", lambda *_: True)
    monkeypatch.setattr(deployment_release, "remote_tag_exists", lambda *_: False)

    with pytest.raises(deployment_release.ReleaseError, match="10 attempts"):
        deployment_release.proposed_tag_name(
            Path("."),
            "upstream",
            now=datetime(2026, 9, 10, 14, 30, 15, tzinfo=UTC),
        )


def test_finalize_tags_source_revision_with_minimal_annotation(monkeypatch):
    source_head = "a" * 40
    config_head = "b" * 40
    tag = "deployed/20260910T143015Z"
    commands = []

    monkeypatch.setattr(
        deployment_release,
        "resolve_commit",
        lambda _repo, _ref: config_head,
    )
    monkeypatch.setattr(deployment_release, "ensure_clean_worktree", lambda _repo: None)
    monkeypatch.setattr(deployment_release, "local_tag_exists", lambda *_: False)
    monkeypatch.setattr(deployment_release, "remote_tag_exists", lambda *_: False)

    def fake_git(_repo, *arguments, **_kwargs):
        commands.append(arguments)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(deployment_release, "git", fake_git)

    deployment_release.finalize(
        {
            "repo": Path("."),
            "base_label": "deployed/20260901T100000Z",
            "source_head": source_head,
            "config_head": config_head,
            "tag": tag,
            "changelog": "Changes since the previous deployment:",
        },
        "upstream",
    )

    tag_command = commands[0]
    assert tag_command[:3] == ("tag", "--annotate", "--message")
    assert tag_command[3] == "Automated deployment"
    assert tag_command[-1] == source_head
    assert commands[1] == ("push", "upstream", tag)
