import pytest

from ymir.tools.unprivileged.distgit_detector import DistgitDetectorTool


@pytest.fixture
def detector():
    return DistgitDetectorTool()


@pytest.mark.parametrize(
    "url",
    [
        "https://src.fedoraproject.org/rpms/kernel",
        "https://src.fedoraproject.org/rpms/kernel/c/abc123",
        "https://SRC.FEDORAPROJECT.ORG/rpms/kernel",
    ],
)
def test_fedora_distgit(detector, url):
    assert detector._check_distgit_source(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://pkgs.devel.redhat.com/cgit/rpms/ncurses",
        "https://pkgs.devel.redhat.com/cgit/rpms/ncurses/patch/?h=rhel-10-main&id=abc123def456",
        "https://pkgs.devel.redhat.com/",
        "https://PKGS.DEVEL.REDHAT.COM/cgit/rpms/foo",
    ],
)
def test_rhel_distgit(detector, url):
    assert detector._check_distgit_source(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/redhat/centos-stream/rpms/kernel",
        "https://gitlab.com/redhat/rhel/rpms/kernel",
        "https://gitlab.com/redhat/centos-stream/rpms/kernel/-/commit/abc",
        "https://GITLAB.COM/redhat/rhel/rpms/foo",
    ],
)
def test_gitlab_distgit(detector, url):
    assert detector._check_distgit_source(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/redhat/some-other-repo",
        "https://gitlab.com/other-org/centos-stream/rpms/kernel",
        "https://gitlab.com/someone/project",
    ],
)
def test_gitlab_non_distgit_paths(detector, url):
    assert detector._check_distgit_source(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/torvalds/linux",
        "https://github.com/openshift/origin",
        "https://kernel.org/pub/linux/kernel",
        "https://example.com",
    ],
)
def test_upstream_urls(detector, url):
    assert detector._check_distgit_source(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://src.fedoraproject.org.evil.com",
        "https://src.fedoraproject.org.evil.com/rpms/kernel",
        "https://pkgs.devel.redhat.com.evil.com",
        "https://pkgs.devel.redhat.com.evil.com/cgit/rpms/foo",
        "https://gitlab.com.evil.com",
        "https://gitlab.com.evil.com/redhat/rhel/rpms/kernel",
        "https://evil-src.fedoraproject.org/rpms/kernel",
        "https://evil-pkgs.devel.redhat.com/cgit/rpms/foo",
        "https://evil.com/pkgs.devel.redhat.com",
        "https://evil-gitlab.com/redhat/rhel/rpms/kernel",
    ],
)
def test_hostname_spoofing_rejected(detector, url):
    assert detector._check_distgit_source(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "://missing-scheme",
    ],
)
def test_invalid_urls(detector, url):
    assert detector._check_distgit_source(url) is False
