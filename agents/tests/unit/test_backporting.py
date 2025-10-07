import pytest
from flexmock import flexmock
from specfile import Specfile

from agents.backport_agent import get_unpacked_sources

@pytest.mark.parametrize(
    "buildsubdir",
    [
        "minimal",
        "minimal/baz",
    ],
)
def test_get_unpacked_sources(tmp_path, buildsubdir, minimal_spec):
    assert minimal_spec.exists()
    flexmock(Specfile).should_receive("expand").and_return(buildsubdir)
    (tmp_path / buildsubdir).mkdir(parents=True)
    result = get_unpacked_sources(tmp_path, "minimal")

    assert result == tmp_path / "minimal"
