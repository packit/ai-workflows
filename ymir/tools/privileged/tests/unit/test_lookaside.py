import git
import pyrpkg.errors
import pyrpkg.lookaside
import pytest
from flexmock import flexmock

from ymir.tools.privileged import lookaside as lookaside_module
from ymir.tools.privileged.lookaside import (
    DownloadSourcesTool,
    UploadSourcesTool,
    _get_config,
    _update_gitignore,
)


async def _noop():
    pass


def _mock_kerberos():
    flexmock(lookaside_module).should_receive("_try_init_kerberos").replace_with(_noop)


def test_get_config_centos_stream():
    config = _get_config("c9s")
    assert config.download_url == "https://sources.stream.centos.org/sources"
    assert config.upload_url == "https://sources.stream.rdu2.redhat.com/lookaside/upload.cgi"
    assert config.hashtype == "sha512"
    assert config.namespaced is True


def test_get_config_rhel():
    config = _get_config("rhel-9-main")
    assert config.download_url == "https://pkgs.devel.redhat.com/repo/"
    assert config.upload_url == "https://pkgs.devel.redhat.com/lookaside/upload.cgi"
    assert config.hashtype == "sha512"
    assert config.namespaced is True


@pytest.mark.parametrize("branch", ["c9s", "rhel-9-main"])
@pytest.mark.asyncio
async def test_download_sources(branch, tmp_path):
    sources_file = tmp_path / "sources"
    sources_file.write_text("SHA512 (foo-1.0.tar.gz) = abc123\n")

    mock_cache = flexmock()
    mock_cache.should_receive("download").once()

    _mock_kerberos()
    flexmock(pyrpkg.lookaside).should_receive("CGILookasideCache").and_return(mock_cache)

    result = (
        await DownloadSourcesTool().run(
            input={
                "dist_git_path": str(tmp_path),
                "package": "foo",
                "dist_git_branch": branch,
            }
        )
    ).result
    assert result.startswith("Successfully")


@pytest.mark.asyncio
async def test_download_sources_handles_download_error(tmp_path):
    sources_file = tmp_path / "sources"
    sources_file.write_text("SHA512 (foo-1.0.tar.gz) = abc123\n")

    mock_cache = flexmock()
    mock_cache.should_receive("download").and_raise(pyrpkg.errors.DownloadError("404 Not Found"))

    _mock_kerberos()
    flexmock(pyrpkg.lookaside).should_receive("CGILookasideCache").and_return(mock_cache)

    with pytest.raises(lookaside_module.ToolError, match="Failed to download"):
        await DownloadSourcesTool().run(
            input={
                "dist_git_path": str(tmp_path),
                "package": "foo",
                "dist_git_branch": "rhel-9-main",
            }
        )


@pytest.mark.parametrize("branch", ["c10s", "rhel-10-main"])
@pytest.mark.asyncio
async def test_upload_sources(branch, tmp_path):
    source = tmp_path / "foo-2.0.tar.gz"
    source.write_text("fake tarball")
    sources_file = tmp_path / "sources"
    sources_file.write_text("SHA512 (foo-1.0.tar.gz) = oldhash\n")
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("/foo-1.0.tar.gz\n")

    mock_cache = flexmock(hashtype="sha512")
    mock_cache.should_receive("hash_file").and_return("newhash").once()
    mock_cache.should_receive("upload").once()

    mock_index = flexmock()
    mock_index.should_receive("add").with_args(["sources", ".gitignore"]).once()
    mock_repo = flexmock(index=mock_index)
    flexmock(git).should_receive("Repo").with_args(tmp_path).and_return(mock_repo).once()

    async def init_kerberos_ticket():
        return True

    flexmock(lookaside_module).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket)
    flexmock(pyrpkg.lookaside).should_receive("CGILookasideCache").and_return(mock_cache)

    result = (
        await UploadSourcesTool().run(
            input={
                "dist_git_path": str(tmp_path),
                "package": "foo",
                "dist_git_branch": branch,
                "new_sources": ["foo-2.0.tar.gz"],
            }
        )
    ).result
    assert result.startswith("Successfully")

    sources_content = sources_file.read_text()
    assert "foo-2.0.tar.gz" in sources_content
    assert "foo-1.0.tar.gz" not in sources_content

    gitignore_content = gitignore.read_text()
    assert "/foo-1.0.tar.gz" in gitignore_content
    assert "/foo-2.0.tar.gz" in gitignore_content


@pytest.mark.asyncio
async def test_upload_sources_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    result = (
        await UploadSourcesTool().run(
            input={
                "dist_git_path": str(tmp_path),
                "package": "foo",
                "dist_git_branch": "rhel-10-main",
                "new_sources": ["foo-1.0.tar.gz"],
            }
        )
    ).result
    assert "Dry run" in result


@pytest.mark.asyncio
async def test_download_sources_rejects_path_traversal(tmp_path):
    sources_file = tmp_path / "sources"
    sources_file.write_text("SHA512 (../../../etc/shadow) = abc123\n")

    mock_cache = flexmock()
    mock_cache.should_receive("download").never()

    _mock_kerberos()
    flexmock(pyrpkg.lookaside).should_receive("CGILookasideCache").and_return(mock_cache)

    with pytest.raises(lookaside_module.ToolError, match="Invalid source filename"):
        await DownloadSourcesTool().run(
            input={
                "dist_git_path": str(tmp_path),
                "package": "foo",
                "dist_git_branch": "rhel-9-main",
            }
        )


@pytest.mark.asyncio
async def test_upload_sources_rejects_path_traversal(tmp_path):
    sources_file = tmp_path / "sources"
    sources_file.write_text("")

    async def init_kerberos_ticket():
        return True

    flexmock(lookaside_module).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket)

    with pytest.raises(lookaside_module.ToolError, match="Invalid source file path"):
        await UploadSourcesTool().run(
            input={
                "dist_git_path": str(tmp_path),
                "package": "foo",
                "dist_git_branch": "rhel-10-main",
                "new_sources": ["../../../etc/shadow"],
            }
        )


def test_update_gitignore_adds_new_entries(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\n/old-source.tar.gz\n")

    _update_gitignore(tmp_path, {"new-source.tar.gz"})

    content = gitignore.read_text()
    assert "/new-source.tar.gz" in content
    assert "*.pyc" in content
    assert "/old-source.tar.gz" in content


def test_update_gitignore_skips_already_matched(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.tar.gz\n")

    _update_gitignore(tmp_path, {"foo-1.0.tar.gz"})

    lines = gitignore.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0] == "*.tar.gz"


def test_update_gitignore_creates_file(tmp_path):
    _update_gitignore(tmp_path, {"foo-1.0.tar.gz"})

    content = (tmp_path / ".gitignore").read_text()
    assert "/foo-1.0.tar.gz" in content
