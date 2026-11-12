from ymir.common.upstream_tags import base_commit_tag_patterns, find_base_commit_tag


def test_plain_version_conventions_come_first():
    patterns = base_commit_tag_patterns("2.2.5", None)

    assert patterns[:3] == ["v2.2.5", "2.2.5", "release-2.2.5"]


def test_uppercase_underscore_conventions_are_covered():
    patterns = base_commit_tag_patterns("2.2.5", "expat")

    # libexpat tags releases as R_2_2_5; PostgreSQL as REL_15_0.
    assert "R_2_2_5" in patterns
    assert "REL_2_2_5" in patterns


def test_package_prefixed_tags_are_covered():
    patterns = base_commit_tag_patterns("4.5.0", "libtiff")

    assert "libtiff-4_5_0" in patterns
    assert "LIBCURL-4_5_0" not in patterns


def test_versioned_package_names_also_try_the_stripped_name():
    patterns = base_commit_tag_patterns("18.0", "nodejs22")

    assert "nodejs22-18_0" in patterns
    assert "nodejs-18_0" in patterns


def test_patterns_are_unique():
    patterns = base_commit_tag_patterns("1.0", "pkg")

    assert len(patterns) == len(set(patterns))


def test_find_base_commit_tag_returns_the_first_match():
    tags = {"release-1.2.3", "v1.2.3"}

    assert find_base_commit_tag(tags, "1.2.3", None) == "v1.2.3"
    assert find_base_commit_tag({"other"}, "1.2.3", None) is None


def test_find_base_commit_tag_matches_the_libexpat_convention():
    # FindBaseCommitTool gained this pattern when the list moved to
    # common/upstream_tags.py, so the agent resolves libexpat releases too.
    assert find_base_commit_tag({"R_2_2_5"}, "2.2.5", "libexpat") == "R_2_2_5"
