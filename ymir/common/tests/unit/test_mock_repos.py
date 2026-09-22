import pytest

from ymir.common.mock_repos import get_zstream_build_refs


def test_get_zstream_build_refs_keys_fixed_refs_by_package_and_branch():
    configs = {
        "RHEL-1": {
            "input": {"package": "curl", "dist_git_branch": "rhel-9.8.0"},
            "zstream_build_ref": "abc123",
        },
        "RHEL-2": {
            "input": {"package": "bash", "dist_git_branch": "c10s"},
        },
    }

    assert get_zstream_build_refs(configs) == {("curl", "rhel-9.8.0"): "abc123"}


def test_get_zstream_build_refs_rejects_conflicting_fixture_baselines():
    configs = {
        "RHEL-1": {
            "input": {"package": "curl", "dist_git_branch": "rhel-9.8.0"},
            "zstream_build_ref": "first",
        },
        "RHEL-2": {
            "input": {"package": "curl", "dist_git_branch": "rhel-9.8.0"},
            "zstream_build_ref": "second",
        },
    }

    with pytest.raises(ValueError, match="conflicting z-stream build refs"):
        get_zstream_build_refs(configs)


def test_get_zstream_build_refs_requires_fixed_ref_for_zstream_fixture():
    configs = {
        "RHEL-1": {
            "input": {"package": "curl", "dist_git_branch": "rhel-9.8.0"},
        }
    }

    with pytest.raises(ValueError, match="requires zstream_build_ref"):
        get_zstream_build_refs(configs)


@pytest.mark.parametrize("value", ["", "   ", 123])
def test_get_zstream_build_refs_rejects_invalid_ref(value):
    configs = {
        "RHEL-1": {
            "input": {"package": "curl", "dist_git_branch": "rhel-9.8.0"},
            "zstream_build_ref": value,
        }
    }

    with pytest.raises(ValueError, match="invalid zstream_build_ref"):
        get_zstream_build_refs(configs)


def test_get_zstream_build_refs_normalizes_refs_before_comparing_baselines():
    configs = {
        "RHEL-1": {
            "input": {"package": "curl", "dist_git_branch": "rhel-9.8.0"},
            "zstream_build_ref": " \tabc123\n",
        },
        "RHEL-2": {
            "input": {"package": "curl", "dist_git_branch": "rhel-9.8.0"},
            "zstream_build_ref": "abc123",
        },
    }

    assert get_zstream_build_refs({"RHEL-1": configs["RHEL-1"]}) == {("curl", "rhel-9.8.0"): "abc123"}
    assert get_zstream_build_refs(configs) == {("curl", "rhel-9.8.0"): "abc123"}
