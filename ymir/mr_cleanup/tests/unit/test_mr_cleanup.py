from flexmock import flexmock

from ymir.mr_cleanup.mr_cleanup import Action, MRCleanup


def _make_cleanup():
    cleanup = object.__new__(MRCleanup)
    posted_notes = []
    closed_mrs = []

    flexmock(cleanup).should_receive("extract_jira_keys_from_mr").replace_with(lambda mr: mr["jira_keys"])
    flexmock(cleanup).should_receive("_fetch_bot_notes").and_return([])
    flexmock(cleanup).should_receive("_post_mr_note").replace_with(
        lambda mr, body: posted_notes.append((mr, body))
    )
    flexmock(cleanup).should_receive("_close_mr").replace_with(lambda mr: closed_mrs.append(mr))
    flexmock(cleanup).should_receive("_add_label").replace_with(lambda *_args: None)
    return cleanup, posted_notes, closed_mrs


def _make_mr(mr_id, jira_keys, *, labels=None):
    return {
        "id": mr_id,
        "web_url": f"https://gitlab.com/redhat/rhel/rpms/pkg/-/merge_requests/{mr_id}",
        "project_id": 1,
        "iid": mr_id,
        "labels": labels or [],
        "jira_keys": set(jira_keys),
    }


def test_closes_mr_when_any_referenced_jira_is_closed():
    mr = _make_mr(10, ["RHEL-100", "RHEL-200"], labels=["ymir_backport"])
    cleanup, posted_notes, closed_mrs = _make_cleanup()

    action = cleanup.process_mr(
        mr,
        mr["jira_keys"],
        {"RHEL-100": "Closed", "RHEL-200": "In Progress"},
    )

    assert action == Action.CLOSED
    assert closed_mrs == [mr]
    note = posted_notes[0][1]
    assert "RHEL-100" in note
    assert "RHEL-200" in note
    assert "no longer usable as-is" in note
    assert "were not reported as closed" in note
    assert "reopen it" in note
    assert "ymir_todo" in note
    assert "https://ymir.pages.redhat.com/docs/triggering/" in note
    assert "rerun consolidation manually" in note
    assert "https://ymir.pages.redhat.com/docs/agents/mr-consolidation/#label-triggered-consolidation" in note


def test_closes_mr_when_all_referenced_jiras_are_closed():
    mr = _make_mr(10, ["RHEL-100"])
    cleanup, posted_notes, closed_mrs = _make_cleanup()

    action = cleanup.process_mr(mr, mr["jira_keys"], {"RHEL-100": "Closed"})

    assert action == Action.CLOSED
    assert closed_mrs == [mr]
    assert "all referenced Jira issues" in posted_notes[0][1]


def test_keeps_mr_open_when_no_referenced_jira_is_closed():
    mr = _make_mr(10, ["RHEL-100", "RHEL-200"])
    cleanup, posted_notes, closed_mrs = _make_cleanup()

    action = cleanup.process_mr(
        mr,
        mr["jira_keys"],
        {"RHEL-100": "Open", "RHEL-200": "In Progress"},
    )

    assert action == Action.SKIPPED_OPEN_JIRAS
    assert closed_mrs == []
    assert posted_notes == []


def test_partial_closure_is_not_treated_as_an_active_open_mr():
    mr = _make_mr(10, ["RHEL-100", "RHEL-200"])
    cleanup, _, closed_mrs = _make_cleanup()
    flexmock(cleanup).should_receive("fetch_jira_statuses").replace_with(
        lambda _: {"RHEL-100": "Closed", "RHEL-200": "Open"}
    )

    active_keys = cleanup._run_stale_mr_cleanup([mr])

    assert active_keys == set()
    assert closed_mrs == [mr]
