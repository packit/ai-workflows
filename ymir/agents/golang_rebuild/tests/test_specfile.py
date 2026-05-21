"""
Unit tests for Spec File Parser
"""

import pytest

from ymir.agents.golang_rebuild.specfile import SpecFile, bump_spec_for_golang_rebuild


@pytest.fixture
def sample_spec_content():
    return """Name: buildah
Version: 1.33.13
Release: 3%{?dist}
Epoch: 2
Summary: Container image builder

%description
Buildah is a tool for building OCI container images.

%changelog
* Mon Apr 15 2026 Previous Author <prev@redhat.com> - 2:1.33.13-3
- Previous change
- Resolves: RHEL-100000
"""


@pytest.fixture
def spec_file(tmp_path, sample_spec_content):
    spec_path = tmp_path / "buildah.spec"
    spec_path.write_text(sample_spec_content)
    return SpecFile(spec_path)


class TestSpecFile:
    def test_get_name(self, spec_file):
        assert spec_file.get_name() == "buildah"

    def test_get_version(self, spec_file):
        assert spec_file.get_version() == "1.33.13"

    def test_get_epoch(self, spec_file):
        assert spec_file.get_epoch() == "2"

    def test_get_release(self, spec_file):
        assert spec_file.get_release() == "3%{?dist}"

    def test_get_nvr(self, spec_file):
        name, version, release = spec_file.get_nvr()
        assert name == "buildah"
        assert version == "1.33.13"
        assert release == "3%{?dist}"

    def test_get_full_nvr(self, spec_file):
        nvr = spec_file.get_full_nvr()
        assert nvr == "2:buildah-1.33.13-3%{?dist}"

    def test_bump_release_first_time(self, spec_file):
        old, new = spec_file.bump_release()
        assert old == "3%{?dist}"
        assert new == "3%{?dist}.1"
        assert spec_file.get_release() == "3%{?dist}.1"

    def test_bump_release_increment_minor(self, tmp_path):
        content = "Name: buildah\nVersion: 1.33.13\nRelease: 3%{?dist}.1\n\n%changelog\n* Test entry\n"
        spec_path = tmp_path / "buildah.spec"
        spec_path.write_text(content)
        spec = SpecFile(spec_path)
        old, new = spec.bump_release()
        assert old == "3%{?dist}.1"
        assert new == "3%{?dist}.2"

    def test_bump_release_no_dist_macro(self, tmp_path):
        content = "Name: test\nVersion: 1.0\nRelease: 5\n\n%changelog\n"
        spec_path = tmp_path / "test.spec"
        spec_path.write_text(content)
        spec = SpecFile(spec_path)
        old, new = spec.bump_release()
        assert old == "5"
        assert new == "5.1"

    def test_find_changelog_line(self, spec_file):
        idx = spec_file.find_changelog_line()
        assert spec_file.lines[idx] == "%changelog"

    def test_add_changelog_entry(self, spec_file):
        spec_file.add_changelog_entry(
            golang_version="1.25.8",
            cves=["CVE-2025-12345", "CVE-2025-67890"],
            jiras=["RHEL-158645", "RHEL-149580"],
            author_name="Test User",
            author_email="test@redhat.com",
        )
        latest = spec_file.get_latest_changelog_entry()
        assert "Test User <test@redhat.com>" in latest
        assert "2:buildah-1.33.13-3%{?dist}" in latest
        assert "Rebuilding with new golang 1.25.8" in latest
        assert "CVE-2025-12345" in latest
        assert "RHEL-149580" in latest

    def test_validate_spec_valid(self, spec_file):
        errors = spec_file.validate_spec()
        assert len(errors) == 0

    def test_validate_spec_missing_fields(self, tmp_path):
        content = "Summary: Invalid spec\n%description\nMissing required fields"
        spec_path = tmp_path / "invalid.spec"
        spec_path.write_text(content)
        spec = SpecFile(spec_path)
        errors = spec.validate_spec()
        assert "Name: field not found" in errors
        assert "Version: field not found" in errors
        assert "Release: field not found" in errors
        assert "%changelog section not found" in errors

    def test_find_spec_file_single(self, tmp_path):
        spec_path = tmp_path / "test.spec"
        spec_path.write_text("Name: test")
        found = SpecFile.find_spec_file(tmp_path)
        assert found == spec_path

    def test_find_spec_file_multiple_error(self, tmp_path):
        (tmp_path / "test1.spec").write_text("Name: test1")
        (tmp_path / "test2.spec").write_text("Name: test2")
        with pytest.raises(ValueError, match=r"Multiple \.spec files"):
            SpecFile.find_spec_file(tmp_path)

    def test_find_spec_file_none(self, tmp_path):
        found = SpecFile.find_spec_file(tmp_path)
        assert found is None

    def test_update_commit0(self, tmp_path):
        content = """%global commit0 aabbccdd11223344
Name: buildah
Version: 1.33.13
Release: 3%{?dist}

%changelog
"""
        spec_path = tmp_path / "buildah.spec"
        spec_path.write_text(content)
        spec = SpecFile(spec_path)

        old = spec.update_commit0("ff00ff00ff00ff00")
        assert old == "aabbccdd11223344"
        # Verify the line was updated
        assert "%global commit0 ff00ff00ff00ff00" in "\n".join(spec.lines)

    def test_update_commit0_not_found(self, tmp_path):
        content = "Name: test\nVersion: 1.0\nRelease: 1\n\n%changelog\n"
        spec_path = tmp_path / "test.spec"
        spec_path.write_text(content)
        spec = SpecFile(spec_path)
        assert spec.update_commit0("abc123") is None

    def test_custom_message_in_changelog(self, tmp_path, sample_spec_content):
        spec_path = tmp_path / "buildah.spec"
        spec_path.write_text(sample_spec_content)
        spec = SpecFile(spec_path)
        spec.add_changelog_entry(
            golang_version="1.25.8",
            cves=["CVE-2025-12345"],
            jiras=["RHEL-149580"],
            author_name="Test User",
            author_email="test@redhat.com",
            custom_message="Custom rebuild reason for net/http vulnerability",
        )
        latest = spec.get_latest_changelog_entry()
        assert "Custom rebuild reason for net/http vulnerability" in latest
        assert "Rebuilding with new golang" not in latest

    def test_bump_spec_for_golang_rebuild(self, tmp_path, sample_spec_content):
        spec_path = tmp_path / "buildah.spec"
        spec_path.write_text(sample_spec_content)
        old, new = bump_spec_for_golang_rebuild(
            spec_path=spec_path,
            golang_version="1.25.8",
            cves=["CVE-2025-12345"],
            jiras=["RHEL-149580"],
            author_name="Test User",
            author_email="test@redhat.com",
        )
        assert old == "3%{?dist}"
        assert new == "3%{?dist}.1"
        spec = SpecFile(spec_path)
        assert spec.get_release() == "3%{?dist}.1"
        latest = spec.get_latest_changelog_entry()
        assert "Rebuilding with new golang 1.25.8" in latest
