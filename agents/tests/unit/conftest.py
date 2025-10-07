import pytest
from pathlib import Path
from textwrap import dedent


@pytest.fixture
def minimal_spec(tmp_path) -> Path:
    spec = tmp_path / "minimal.spec"
    spec.write_text(
        dedent(
            """
            Name:           test
            Version:        0.1
            Release:        2%{?dist}
            Summary:        Test package

            License:        MIT

            %description
            Test package

            %changelog
            * Thu Jun 07 2018 Nikola Forró <nforro@redhat.com> - 0.1-1
            - first version
            """
        )
    )
    return spec
