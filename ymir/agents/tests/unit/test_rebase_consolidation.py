from ymir.agents.rebase_consolidation import build_rebase_siblings_jql


def test_build_rebase_siblings_jql():
    jql = build_rebase_siblings_jql("RHEL-100", "dotnet10.0", "rhel-10.2")
    assert 'component = "dotnet10.0"' in jql
    assert 'fixVersion in ("rhel-10.2", "rhel-10.2.z")' in jql
    assert 'key != "RHEL-100"' in jql
    assert 'labels = "SecurityTracking"' in jql
    assert "labels not in" in jql
    assert '"ymir_triaged_rebase"' in jql  # Exclude to prevent circular consolidation
    assert '"ymir_triaged_not_affected"' in jql
    assert '"ymir_triaged_backport"' in jql
    assert '"ymir_triaged_rebuild"' in jql
    assert '"ymir_triaged_postponed"' in jql
    assert 'status in ("New", "Planning")' in jql


def test_build_rebase_siblings_jql_escapes_component_quotes():
    jql = build_rebase_siblings_jql("RHEL-100", 'comp"name', "rhel-9.8.z")
    assert r'component = "comp\"name"' in jql
    assert 'fixVersion in ("rhel-9.8", "rhel-9.8.z")' in jql


def test_build_rebase_siblings_jql_excludes_correct_labels():
    """Verify that rebase consolidation excludes terminal triage labels to prevent circular consolidation."""
    jql = build_rebase_siblings_jql("RHEL-100", "python3.12", "rhel-9.8")
    # Should exclude issues already triaged (prevents circular consolidation)
    assert '"ymir_triaged_not_affected"' in jql
    assert '"ymir_triaged_backport"' in jql
    assert '"ymir_triaged_rebuild"' in jql
    assert '"ymir_triaged_rebase"' in jql  # Prevents circular consolidation
    assert '"ymir_triaged_postponed"' in jql
