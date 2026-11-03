import json

from ymir.mr_review import post


def _record(body):
    return {"type": "review", "body": body}


def _write_review(tmp_path, record):
    safe_outputs = tmp_path / "safe-outputs"
    safe_outputs.mkdir()
    (safe_outputs / "review.json").write_text(json.dumps(record))


def _set_ci_env(monkeypatch, *, token="x"):
    monkeypatch.setenv("CI_API_V4_URL", "https://gitlab.example.invalid/api/v4")
    monkeypatch.setenv("CI_PROJECT_ID", "1")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "9")
    if token is None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("GITLAB_TOKEN", token)


def _fake_gitlab(monkeypatch, *, notes, me="bot"):
    """Capture every API call; returns per-method call lists."""
    calls = {"get": [], "put": [], "post": []}

    class FakeGitLab:
        def __init__(self, api_url, token):
            pass

        def get(self, path, params=None):
            calls["get"].append((path, params))
            if path == "/user":
                return {"username": me}
            return notes

        def put(self, path, body):
            calls["put"].append((path, body))
            return {}

        def post(self, path, body):
            calls["post"].append((path, body))
            return {}

    monkeypatch.setattr(post, "GitLab", FakeGitLab)
    return calls


def test_missing_artifact_is_a_clean_no_op(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_ci_env(monkeypatch)
    calls = _fake_gitlab(monkeypatch, notes=[])
    assert post.main() == 0
    assert calls["get"] == []


def test_malformed_record_is_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_ci_env(monkeypatch)
    safe_outputs = tmp_path / "safe-outputs"
    safe_outputs.mkdir()
    (safe_outputs / "review.json").write_text("not json at all")
    assert post.main() == 1
    (safe_outputs / "review.json").write_text(json.dumps({"type": "comment", "body": "x"}))
    assert post.main() == 1


def test_wrong_type_is_rejected(tmp_path):
    import os
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(json.dumps({"type": "comment", "body": "x"}))
        path = f.name
    try:
        try:
            post.load_review(path)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    finally:
        os.unlink(path)


def test_summary_slicing():
    record = _record("preamble chatter\n\n## Summary\n\nNo concerns found.\n")
    assert post.review_body(record) == "## Summary\n\nNo concerns found."


def test_summary_fallback_to_whole_body():
    record = _record("No concerns found.")
    assert post.review_body(record) == "No concerns found."


def test_trailer_composition(monkeypatch):
    monkeypatch.setenv("MR_REVIEW_IMPL_URL", "[impl](https://example/impl)")
    monkeypatch.setenv("AGENTHOME_IMAGE", "reg.example/agenthome:t1")
    monkeypatch.setenv("CI_JOB_URL", "https://gitlab.example/j/1")
    assert (
        post.build_trailer() == "*Agentic review — [impl](https://example/impl)"
        " · agenthome image `reg.example/agenthome:t1`"
        " · [run logs](https://gitlab.example/j/1)*"
    )


def test_note_composition(monkeypatch):
    monkeypatch.delenv("MR_REVIEW_IMPL_URL", raising=False)
    monkeypatch.delenv("AGENTHOME_IMAGE", raising=False)
    monkeypatch.delenv("CI_JOB_URL", raising=False)
    note = post.compose_note(_record("## Summary\n\nBlockers:\n- spec:1 bad"))
    assert note.startswith("### Agentic review\n\n## Summary\n\nBlockers:\n- spec:1 bad\n\n---\n")
    assert note.endswith("*Agentic review — *")


def test_edits_own_note_in_place(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_review(tmp_path, _record("## Summary\n\nNo concerns found."))
    _set_ci_env(monkeypatch)
    monkeypatch.setenv("AGENTHOME_IMAGE", "reg.example/agenthome:t1")
    notes = [
        {"id": 9, "author": {"username": "human"}, "body": "### Agentic review\nnot ours"},
        {"id": 5, "author": {"username": "bot"}, "body": "### Agentic review\nold"},
    ]
    calls = _fake_gitlab(monkeypatch, notes=notes)

    assert post.main() == 0
    # Only our own note is edited; other users' notes are untouched.
    assert [c[0] for c in calls["put"]] == ["/projects/1/merge_requests/9/notes/5"]
    assert calls["post"] == []
    note = calls["put"][0][1]["body"]
    assert note.startswith("### Agentic review\n\n## Summary\n\nNo concerns found.\n\n---\n")
    assert "agenthome image" in note


def test_posts_when_no_previous_note(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_review(tmp_path, _record("## Summary\n\nNo concerns found."))
    _set_ci_env(monkeypatch)
    calls = _fake_gitlab(monkeypatch, notes=[])

    assert post.main() == 0
    assert calls["put"] == []
    assert len(calls["post"]) == 1
    assert calls["post"][0][0] == "/projects/1/merge_requests/9/notes"


def test_no_token_posts_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_review(tmp_path, _record("## Summary\n\nNo concerns found."))
    _set_ci_env(monkeypatch, token=None)
    calls = _fake_gitlab(monkeypatch, notes=[])
    assert post.main() == 0
    assert calls["get"] == []
