from ymir.agents.rebuild_consolidation import build_rebuild_siblings_jql


def test_build_rebuild_siblings_jql():
    jql = build_rebuild_siblings_jql("RHEL-100", "git-lfs", "rhel-9.8")
    assert 'component = "git-lfs"' in jql
    assert 'fixVersion = "rhel-9.8"' in jql
    assert 'key != "RHEL-100"' in jql
    assert 'labels = "SecurityTracking"' in jql
    assert "labels not in" in jql
    assert '"ymir_triaged_rebuild"' in jql
    assert '"ymir_rebuilt"' in jql
    assert '"ymir_triaged_not_affected"' in jql
    assert '"ymir_triaged_backport"' in jql
    assert '"ymir_triaged_rebase"' in jql
    assert 'status in ("New", "Planning")' in jql


def test_build_rebuild_siblings_jql_escapes_quotes():
    jql = build_rebuild_siblings_jql("RHEL-100", 'comp"name', 'rhel-9.8"z')
    assert r'component = "comp\"name"' in jql
    assert r'fixVersion = "rhel-9.8\"z"' in jql
