from flexmock import flexmock

from ymir.mr_cleanup.mr_cleanup import Action, MRCleanup


def _make_cleanup(*, bot_notes=None):
    cleanup = object.__new__(MRCleanup)
    posted_notes = []
    closed_mrs = []
    labels_added = []
    bot_notes = [] if bot_notes is None else bot_notes

    flexmock(cleanup).should_receive("extract_jira_keys_from_mr").replace_with(lambda mr: mr["jira_keys"])
    flexmock(cleanup).should_receive("_fetch_bot_notes").replace_with(lambda _: bot_notes)
    flexmock(cleanup).should_receive("_post_mr_note").replace_with(
        lambda mr, body: posted_notes.append((mr, body))
    )
    flexmock(cleanup).should_receive("_close_mr").replace_with(lambda mr: closed_mrs.append(mr))
    flexmock(cleanup).should_receive("_add_label").replace_with(
        lambda mr, label: labels_added.append((mr, label))
    )
    return cleanup, posted_notes, closed_mrs, labels_added


def _make_mr(mr_id, jira_keys, *, labels=None):
    return {
        "id": mr_id,
        "web_url": f"https://gitlab.com/redhat/rhel/rpms/pkg/-/merge_requests/{mr_id}",
        "project_id": 1,
        "iid": mr_id,
        "labels": labels or [],
        "jira_keys": set(jira_keys),
    }


def test_comments_without_closing_when_some_referenced_jira_is_closed():
    mr = _make_mr(10, ["RHEL-100", "RHEL-200"], labels=["ymir_backport"])
    cleanup, posted_notes, closed_mrs, labels_added = _make_cleanup()

    action = cleanup.process_mr(
        mr,
        mr["jira_keys"],
        {"RHEL-100": "Closed", "RHEL-200": "In Progress"},
    )

    assert action == Action.PARTIAL_CLOSURE_COMMENTED
    assert closed_mrs == []
    assert labels_added == []
    note = posted_notes[0][1]
    assert "RHEL-100" in note
    assert "RHEL-200" in note
    assert "Some referenced Jira issues are already closed" in note
    assert "were not reported as closed" in note
    assert "left open for manual review" in note
    assert "ymir_todo" in note
    assert "https://ymir.pages.redhat.com/docs/triggering/" in note
    assert "rerun consolidation manually" in note
    assert "https://ymir.pages.redhat.com/docs/agents/mr-consolidation/#label-triggered-consolidation" in note
    assert "For rebase consolidations" in note
    assert "#how-to-manually-trigger-rebase-consolidation" in note


def test_closes_mr_when_all_referenced_jiras_are_closed():
    mr = _make_mr(10, ["RHEL-100"])
    cleanup, posted_notes, closed_mrs, labels_added = _make_cleanup()

    action = cleanup.process_mr(mr, mr["jira_keys"], {"RHEL-100": "Closed"})

    assert action == Action.CLOSED
    assert closed_mrs == [mr]
    assert labels_added == [(mr, "ymir_cleaned_up")]
    assert "all referenced Jira issues" in posted_notes[0][1]


def test_keeps_mr_open_when_no_referenced_jira_is_closed():
    mr = _make_mr(10, ["RHEL-100", "RHEL-200"])
    cleanup, posted_notes, closed_mrs, labels_added = _make_cleanup()

    action = cleanup.process_mr(
        mr,
        mr["jira_keys"],
        {"RHEL-100": "Open", "RHEL-200": "In Progress"},
    )

    assert action == Action.SKIPPED_OPEN_JIRAS
    assert closed_mrs == []
    assert posted_notes == []
    assert labels_added == []


def test_partial_closure_comment_is_not_duplicated():
    mr = _make_mr(10, ["RHEL-100", "RHEL-200"])
    bot_notes = []
    cleanup, posted_notes, closed_mrs, labels_added = _make_cleanup(bot_notes=bot_notes)
    jira_statuses = {"RHEL-100": "Closed", "RHEL-200": "Open"}

    first_action = cleanup.process_mr(mr, mr["jira_keys"], jira_statuses)
    bot_notes.append(posted_notes[0][1])
    second_action = cleanup.process_mr(mr, mr["jira_keys"], jira_statuses)

    assert first_action == Action.PARTIAL_CLOSURE_COMMENTED
    assert second_action == Action.PARTIAL_CLOSURE_COMMENTED
    assert len(posted_notes) == 1
    assert closed_mrs == []
    assert labels_added == []


def test_partial_closure_comment_is_posted_when_closed_jira_subset_changes():
    mr = _make_mr(10, ["RHEL-100", "RHEL-200", "RHEL-300"])
    bot_notes = []
    cleanup, posted_notes, closed_mrs, labels_added = _make_cleanup(bot_notes=bot_notes)

    cleanup.process_mr(
        mr,
        mr["jira_keys"],
        {"RHEL-100": "Closed", "RHEL-200": "Open", "RHEL-300": "Open"},
    )
    bot_notes.append(posted_notes[-1][1])

    cleanup.process_mr(
        mr,
        mr["jira_keys"],
        {"RHEL-100": "Closed", "RHEL-200": "Closed", "RHEL-300": "Open"},
    )
    bot_notes.append(posted_notes[-1][1])

    cleanup.process_mr(
        mr,
        mr["jira_keys"],
        {"RHEL-100": "Closed", "RHEL-200": "Open", "RHEL-300": "Open"},
    )

    assert len(posted_notes) == 2
    assert "Some referenced Jira issues are already closed: RHEL-100." in posted_notes[0][1]
    assert "Some referenced Jira issues are already closed: RHEL-100, RHEL-200." in posted_notes[1][1]
    assert closed_mrs == []
    assert labels_added == []


def test_partial_closure_keeps_mr_jira_keys_active():
    mr = _make_mr(10, ["RHEL-100", "RHEL-200"])
    cleanup, _, closed_mrs, labels_added = _make_cleanup()
    flexmock(cleanup).should_receive("fetch_jira_statuses").replace_with(
        lambda _: {"RHEL-100": "Closed", "RHEL-200": "Open"}
    )

    active_keys = cleanup._run_stale_mr_cleanup([mr])

    assert active_keys == {"RHEL-100", "RHEL-200"}
    assert closed_mrs == []
    assert labels_added == []
