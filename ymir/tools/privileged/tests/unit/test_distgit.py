import git
import pytest
from beeai_framework.tools import ToolError
from flexmock import flexmock
from specfile.utils import EVR

from ymir.common.utils import NoBuildFoundError
from ymir.tools.constants import ZSTREAM_BRANCH_POLICY_REJECTED
from ymir.tools.privileged import distgit as distgit_tools
from ymir.tools.privileged.distgit import (
    CheckinPolicyRejection,
    CreateZstreamBranchTool,
    _format_checkin_policy_rejection,
    _is_transient_git_error,
    _parse_checkin_policy_rejection,
    _retry_transient,
)


@pytest.mark.parametrize(
    "branch_exists",
    [False, True],
)
@pytest.mark.asyncio
async def test_create_zstream_branch(branch_exists, monkeypatch):
    package = "bash"
    branch = "rhel-10.0"
    user = "bot"
    ref = "123456abcdef"  # pragma: allowlist secret

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(branch_exists).and_return(True).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)

    gitcmd.should_receive("push").with_args("origin", f"{ref}:refs/heads/{branch}").times(
        0 if branch_exists else 1
    ).and_return("")
    mock_repo = flexmock(
        git=gitcmd,
        remotes=flexmock(
            origin=flexmock(refs=[]),
        ),
    )
    mock_repo.should_receive("commit").with_args(ref).and_return(flexmock()).times(0 if branch_exists else 1)
    flexmock(git.Repo).should_receive("clone_from").and_return(mock_repo)

    async def mock_get_latest_z_pending_build(package, dist_git_branch):
        return EVR(version="1.0", release="1.el10"), ref

    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
        mock_get_latest_z_pending_build
    ).times(0 if branch_exists else 1)

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    result = (await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})).result
    if branch_exists:
        assert "already exists" in result
    else:
        assert result.startswith("Successfully")
        assert f"at {ref[:12]}" in result


@pytest.mark.asyncio
async def test_create_zstream_branch_distgit_has_branch(monkeypatch):
    """Branch already in dist-git but not yet mirrored to GitLab (retry after partial push success)."""
    package = "bash"
    branch = "rhel-10.0"
    user = "bot"

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    # GitLab check: branch not present yet
    gitcmd = flexmock().should_receive("ls_remote").and_return(False).and_return(True).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)

    mock_ref = flexmock(name=f"origin/{branch}")
    flexmock(git.Repo).should_receive("clone_from").and_return(
        flexmock(
            git=gitcmd,
            remotes=flexmock(
                origin=flexmock(refs=[mock_ref])
                .should_receive("push")
                .times(0)  # no push — branch is already in dist-git
                .mock(),
            ),
        ),
    )

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    result = (await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})).result
    assert result.startswith("Successfully")


@pytest.mark.asyncio
async def test_create_zstream_branch_push_silently_rejected(monkeypatch):
    """Push exits 0 but branch doesn't appear on dist-git — silent rejection by server ACL."""
    package = "bash"
    branch = "rhel-10.0"
    user = "bot"
    ref = "123456abcdef"  # pragma: allowlist secret

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    # First ls_remote checks GitLab (branch not there), subsequent calls verify
    # dist-git after push — all return empty to simulate silent ACL rejection.
    gitcmd = (
        flexmock()
        .should_receive("ls_remote")
        .and_return(False)
        .and_return("")
        .and_return("")
        .and_return("")
        .mock()
    )
    gitcmd.should_receive("push").with_args("origin", f"{ref}:refs/heads/{branch}").once().and_return("")
    flexmock(git.cmd.Git).new_instances(gitcmd)

    mock_repo = flexmock(
        git=gitcmd,
        remotes=flexmock(
            origin=flexmock(refs=[]),
        ),
    )
    mock_repo.should_receive("commit").with_args(ref).and_return(flexmock()).once()
    flexmock(git.Repo).should_receive("clone_from").and_return(mock_repo)

    async def mock_get_latest_z_pending_build(package, dist_git_branch):
        return EVR(version="1.0", release="1.el10"), ref

    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
        mock_get_latest_z_pending_build
    ).once()

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    with pytest.raises(ToolError, match="Failed to push branch to dist-git"):
        await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})


@pytest.mark.parametrize(
    "stderr, expected_transient",
    [
        ("Connection closed by 10.2.32.39 port 22\nfatal: Could not read from remote repository.", True),
        ("Connection reset by peer", True),
        ("ssh_exchange_identification: Connection closed by remote host", True),
        ("error: failed to push some refs to 'ssh://pkgs.devel.redhat.com/rpms/ruby'", False),
        ("Permission denied (publickey)", False),
        ("fatal: Authentication failed for 'https://example.com/'", False),
        ("fatal: Could not read from remote repository.", False),
    ],
)
def test_is_transient_git_error(stderr, expected_transient):
    exc = git.exc.GitCommandError(["git", "clone"], status=128, stderr=stderr)
    assert _is_transient_git_error(exc) == expected_transient


@pytest.mark.asyncio
async def test_retry_transient_clone_recovers():
    """Clone fails transiently twice then succeeds on third attempt."""
    call_count = 0

    async def flaky_clone():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise git.exc.GitCommandError(
                ["git", "clone"], status=128, stderr="Connection closed by 10.2.32.39 port 22"
            )
        return "cloned"

    result = await _retry_transient(flaky_clone, "test-clone", max_retries=3, base_delay=0)
    assert result == "cloned"
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_transient_permanent_error_no_retry():
    """Permanent errors are raised immediately without retrying."""
    call_count = 0

    async def permanent_fail():
        nonlocal call_count
        call_count += 1
        raise git.exc.GitCommandError(["git", "clone"], status=128, stderr="Permission denied (publickey)")

    with pytest.raises(git.exc.GitCommandError, match="Permission denied"):
        await _retry_transient(permanent_fail, "test-clone", max_retries=3, base_delay=0)
    assert call_count == 1


@pytest.mark.parametrize(
    "branch, remote_branches, expected",
    [
        ("rhel-9.7.0", ["rhel-9.8.0", "rhel-9.9.0", "rhel-9-main"], "rhel-9.8.0"),
        ("rhel-9.8.0", ["rhel-9.7.0", "rhel-9.9.0", "rhel-9-main"], "rhel-9.9.0"),
        ("rhel-9.9.0", ["rhel-9.7.0", "rhel-9.8.0", "rhel-9-main"], "rhel-9-main"),
        ("rhel-10.2", ["rhel-10.0", "rhel-10.3", "rhel-10-main"], "rhel-10.3"),
        ("rhel-10.3", ["rhel-10.0", "rhel-10.2", "rhel-10-main"], "rhel-10-main"),
        ("rhel-10.3", ["rhel-10.0", "rhel-10.2"], None),
        ("c9s", ["rhel-9.7.0", "rhel-9-main"], None),
    ],
)
def test_find_source_branch(branch, remote_branches, expected):
    mock_refs = [flexmock(name=f"origin/{b}") for b in remote_branches]
    repo = flexmock(remotes=flexmock(origin=flexmock(refs=mock_refs)))
    assert CreateZstreamBranchTool._find_source_branch(repo, branch) == expected


@pytest.mark.asyncio
async def test_create_zstream_branch_advances_ref_on_main(monkeypatch):
    """NVR walk advances the ref when the source branch is rhel-X-main."""
    package = "bash"
    branch = "rhel-10.2"
    user = "bot"
    build_ref = "aaa111"  # pragma: allowlist secret
    advanced_ref = "bbb222"  # pragma: allowlist secret

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).and_return(True).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)

    async def mock_get_latest_z_pending_build(package, dist_git_branch):
        return EVR(version="1.0", release="1.el10"), build_ref

    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
        mock_get_latest_z_pending_build
    ).once()

    mock_main_ref = flexmock(name="origin/rhel-10-main")
    gitcmd.should_receive("push").with_args(
        "origin", f"{advanced_ref}:refs/heads/{branch}"
    ).once().and_return("")
    mock_repo = flexmock(
        git=gitcmd,
        remotes=flexmock(
            origin=flexmock(refs=[mock_main_ref]),
        ),
    )
    mock_repo.should_receive("commit").with_args(advanced_ref).and_return(flexmock()).once()
    flexmock(git.Repo).should_receive("clone_from").and_return(mock_repo)

    async def mock_find_latest_same_nvr_ref(repo, pkg, ref, source):
        assert pkg == package
        assert ref == build_ref
        assert source == "rhel-10-main"
        return advanced_ref

    flexmock(CreateZstreamBranchTool).should_receive("_find_latest_same_nvr_ref").replace_with(
        mock_find_latest_same_nvr_ref
    ).once()

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    result = (await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})).result
    assert result.startswith("Successfully")
    assert "from rhel-10-main" in result
    assert f"at {advanced_ref[:12]}" in result


@pytest.mark.asyncio
async def test_create_zstream_branch_skips_nvr_walk_on_zstream_source(monkeypatch):
    """NVR walk is skipped when source branch is a z-stream branch (not -main)."""
    package = "isns-utils"
    branch = "rhel-10.0"
    user = "bot"
    build_ref = "a3276f38"  # pragma: allowlist secret

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).and_return(True).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)

    async def mock_get_latest_z_pending_build(package, dist_git_branch):
        return EVR(version="0.103", release="1.el10"), build_ref

    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
        mock_get_latest_z_pending_build
    ).once()

    # Source branch is rhel-10.2 (a z-stream, not -main) → NVR walk must be skipped
    mock_higher_ref = flexmock(name="origin/rhel-10.2")
    gitcmd.should_receive("push").with_args("origin", f"{build_ref}:refs/heads/{branch}").once().and_return(
        ""
    )
    mock_repo = flexmock(
        git=gitcmd,
        remotes=flexmock(
            origin=flexmock(refs=[mock_higher_ref]),
        ),
    )
    mock_repo.should_receive("commit").with_args(build_ref).and_return(flexmock()).once()
    flexmock(git.Repo).should_receive("clone_from").and_return(mock_repo)

    # _find_latest_same_nvr_ref must NOT be called
    flexmock(CreateZstreamBranchTool).should_receive("_find_latest_same_nvr_ref").times(0)

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    result = (await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})).result
    assert result.startswith("Successfully")


def _mock_spec_commit(hexsha, spec_content):
    """Create a mock commit whose tree contains a spec file with given content."""
    blob = flexmock(name="bash.spec", data_stream=flexmock(read=lambda: spec_content.encode()))
    return flexmock(hexsha=hexsha, tree=flexmock(blobs=[blob]))


_SPEC_V1 = "Name: bash\nVersion: 5.2.26\nRelease: 1.el10\nSummary: x\nLicense: x\n"
_SPEC_V2 = "Name: bash\nVersion: 5.2.27\nRelease: 1.el10\nSummary: x\nLicense: x\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build_files, fixup_files, advance",
    [
        pytest.param({"bash.spec": _SPEC_V1}, {"bash.spec": _SPEC_V1}, True, id="canonical"),
        pytest.param({"bash-extra.spec": _SPEC_V1}, {"bash-extra.spec": _SPEC_V1}, True, id="custom"),
        pytest.param({"Bash.spec": _SPEC_V1}, {"Bash.spec": _SPEC_V1}, True, id="case"),
        pytest.param({"bash-extra.spec": _SPEC_V1}, {"Bash.spec": _SPEC_V1}, True, id="renamed"),
        pytest.param(
            {"bash.spec": _SPEC_V1, "other.spec": _SPEC_V2},
            {"bash.spec": _SPEC_V1, "other.spec": _SPEC_V2},
            True,
            id="prefer-canonical",
        ),
        pytest.param(
            {"bash-extra.spec": _SPEC_V1, ".hidden.spec": _SPEC_V2, "nested.spec/file": "x"},
            {"bash-extra.spec": _SPEC_V1},
            True,
            id="ignore-hidden-and-directory",
        ),
        pytest.param({}, {"bash.spec": _SPEC_V1}, False, id="missing-build-spec"),
        pytest.param(
            {"bash-extra.spec": _SPEC_V1, "other.spec": _SPEC_V1},
            {"bash-extra.spec": _SPEC_V1},
            False,
            id="ambiguous-build-spec",
        ),
        pytest.param({"bash.spec": _SPEC_V1}, {}, False, id="missing-fixup-spec"),
        pytest.param(
            {"bash.spec": _SPEC_V1},
            {"bash-extra.spec": _SPEC_V1, "other.spec": _SPEC_V1},
            False,
            id="ambiguous-fixup-spec",
        ),
    ],
)
async def test_find_latest_same_nvr_ref_with_empty_checkout(
    tmp_path, monkeypatch, build_files, fixup_files, advance
):
    """Discover specs in commit trees even when the default checkout has no spec."""
    # Earlier new_instances() mocks can break Git's inherited allocator.
    # Restore allocation so GitPython can create real Git commands.
    monkeypatch.setattr(git.Git, "__new__", staticmethod(lambda cls, *args, **kwargs: object.__new__(cls)))
    repo = git.Repo.init(tmp_path)
    actor = git.Actor("Test", "test@example.com")
    default_commit = repo.index.commit("Empty default branch", author=actor, committer=actor)

    def commit_files(files, message):
        tracked = [path for path, stage in repo.index.entries if stage == 0]
        if tracked:
            repo.index.remove(tracked, working_tree=True)
        for name, content in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        if files:
            repo.index.add(list(files))
        return repo.index.commit(message, author=actor, committer=actor)

    build_commit = commit_files(build_files, "Build")
    fixup_commit = commit_files(fixup_files, "Fixup")
    bumped_files = dict.fromkeys(fixup_files, _SPEC_V2)
    bump_commit = commit_files(bumped_files, "Version bump")
    repo.git.update_ref("refs/remotes/origin/rhel-10-main", bump_commit.hexsha)
    repo.git.checkout(default_commit.hexsha)

    result = await CreateZstreamBranchTool._find_latest_same_nvr_ref(
        repo, "bash", build_commit.hexsha, "rhel-10-main"
    )

    assert result == (fixup_commit.hexsha if advance else build_commit.hexsha)
    assert repo.head.commit == default_commit
    assert not repo.is_dirty(untracked_files=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit_specs, expected_hexsha",
    [
        # fixup with same NVR, then version bump → advance to fixup
        ([_SPEC_V1, _SPEC_V1, _SPEC_V2], "bbb222"),
        # all same NVR → advance to last
        ([_SPEC_V1, _SPEC_V1, _SPEC_V1], "ccc333"),
        # immediate version bump → no advancement
        ([_SPEC_V1, _SPEC_V2], "aaa111"),
        # only build_ref, no further commits → no advancement
        ([_SPEC_V1], "aaa111"),
    ],
)
async def test_find_latest_same_nvr_ref(commit_specs, expected_hexsha):
    hexshas = ["aaa111", "bbb222", "ccc333", "ddd444"][: len(commit_specs)]
    commits = [_mock_spec_commit(h, s) for h, s in zip(hexshas, commit_specs, strict=True)]

    build_ref = hexshas[0]
    main_ref = "origin/rhel-10-main"

    repo = flexmock()
    repo.should_receive("is_ancestor").with_args(build_ref, main_ref).and_return(True)
    repo.should_receive("commit").with_args(build_ref).and_return(commits[0])
    repo.should_receive("iter_commits").with_args(
        f"{build_ref}..{main_ref}", ancestry_path=True, reverse=True
    ).and_return(commits[1:])

    result = await CreateZstreamBranchTool._find_latest_same_nvr_ref(repo, "bash", build_ref, "rhel-10-main")
    assert result == expected_hexsha


@pytest.mark.asyncio
async def test_find_latest_same_nvr_ref_not_ancestor():
    """Returns build_ref when it is not an ancestor of the source branch."""
    build_ref = "aaa111"
    repo = flexmock()
    repo.should_receive("is_ancestor").and_return(False)

    result = await CreateZstreamBranchTool._find_latest_same_nvr_ref(repo, "bash", build_ref, "rhel-10-main")
    assert result == build_ref


@pytest.mark.asyncio
async def test_find_latest_same_nvr_ref_missing_spec():
    """Returns build_ref when the spec file is missing at that revision."""

    build_ref = "aaa111"
    repo = flexmock()
    repo.should_receive("is_ancestor").and_return(True)
    repo.should_receive("commit").with_args(build_ref).and_return(
        flexmock(hexsha=build_ref, tree=flexmock(blobs=[]))
    )

    result = await CreateZstreamBranchTool._find_latest_same_nvr_ref(repo, "bash", build_ref, "rhel-10-main")
    assert result == build_ref


@pytest.mark.asyncio
async def test_create_zstream_branch_no_source_branch(monkeypatch):
    """When no source branch exists, the build ref is used directly."""
    package = "bash"
    branch = "rhel-10.0"
    user = "bot"
    ref = "123456abcdef"  # pragma: allowlist secret

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).and_return(True).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)

    async def mock_get_latest_z_pending_build(package, dist_git_branch):
        return EVR(version="1.0", release="1.el10"), ref

    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
        mock_get_latest_z_pending_build
    ).once()

    # No higher branches and no rhel-X-main
    gitcmd.should_receive("push").with_args("origin", f"{ref}:refs/heads/{branch}").once().and_return("")
    mock_repo = flexmock(
        git=gitcmd,
        remotes=flexmock(
            origin=flexmock(refs=[]),
        ),
    )
    mock_repo.should_receive("commit").with_args(ref).and_return(flexmock()).once()
    flexmock(git.Repo).should_receive("clone_from").and_return(mock_repo)

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    result = (await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})).result
    assert result.startswith("Successfully")


@pytest.mark.asyncio
async def test_create_zstream_branch_older_zstream(monkeypatch):
    """Z-stream branch creation works for all z-stream branches."""
    package = "bash"
    branch = "rhel-9.6.0"
    user = "bot"
    ref = "abc123def456"  # pragma: allowlist secret

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).and_return(True).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)

    async def mock_get_latest_z_pending_build(package, dist_git_branch):
        return EVR(version="1.0", release="1.el9"), ref

    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
        mock_get_latest_z_pending_build
    ).once()

    gitcmd.should_receive("push").with_args("origin", f"{ref}:refs/heads/{branch}").once().and_return("")
    mock_repo = flexmock(
        git=gitcmd,
        remotes=flexmock(
            origin=flexmock(refs=[]),
        ),
    )
    mock_repo.should_receive("commit").with_args(ref).and_return(flexmock()).once()
    flexmock(git.Repo).should_receive("clone_from").and_return(mock_repo)

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    result = (await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})).result
    assert result.startswith("Successfully")


@pytest.mark.asyncio
async def test_create_zstream_branch_falls_back_to_buildrequires(monkeypatch):
    package = "bash"
    branch = "rhel-10.0"
    ref = "abc123def456"  # pragma: allowlist secret

    async def init_kerberos_ticket():
        return "bot@EXAMPLE.COM"

    async def mock_get_latest_z_pending_build(package, dist_git_branch):
        raise NoBuildFoundError("no z-pending build")

    async def mock_get_latest_buildroot_build(package, dist_git_branch):
        assert dist_git_branch == branch
        return EVR(version="1.0", release="1.el10"), ref

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()
    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
        mock_get_latest_z_pending_build
    ).once()
    flexmock(distgit_tools).should_receive("get_latest_buildroot_build").replace_with(
        mock_get_latest_buildroot_build
    ).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).and_return(True).mock()
    gitcmd.should_receive("push").with_args("origin", f"{ref}:refs/heads/{branch}").once().and_return("")
    flexmock(git.cmd.Git).new_instances(gitcmd)
    mock_repo = flexmock(git=gitcmd, remotes=flexmock(origin=flexmock(refs=[])))
    mock_repo.should_receive("commit").with_args(ref).and_return(flexmock()).once()
    flexmock(git.Repo).should_receive("clone_from").and_return(mock_repo)
    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    result = (await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})).result

    assert result.startswith("Successfully")


@pytest.mark.asyncio
async def test_create_zstream_branch_errors_when_both_build_lookups_fail(monkeypatch):
    package = "bash"
    branch = "rhel-10.0"

    async def init_kerberos_ticket():
        return "bot@EXAMPLE.COM"

    async def fail(*args):
        raise NoBuildFoundError("no build found")

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()
    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(fail).once()
    flexmock(distgit_tools).should_receive("get_latest_buildroot_build").replace_with(fail).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)
    flexmock(git.Repo).should_receive("clone_from").and_return(
        flexmock(git=gitcmd, remotes=flexmock(origin=flexmock(refs=[])))
    )
    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    with pytest.raises(ToolError, match="Failed to find suitable source build"):
        await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})


@pytest.mark.asyncio
async def test_create_zstream_branch_does_not_fallback_on_lookup_error(monkeypatch):
    package = "bash"
    branch = "rhel-10.0"

    async def init_kerberos_ticket():
        return "bot@EXAMPLE.COM"

    async def fail(*args):
        raise RuntimeError("Brew unavailable")

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()
    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(fail).once()
    flexmock(distgit_tools).should_receive("get_latest_buildroot_build").never()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)
    flexmock(git.Repo).should_receive("clone_from").and_return(
        flexmock(git=gitcmd, remotes=flexmock(origin=flexmock(refs=[])))
    )
    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    with pytest.raises(ToolError, match="Failed to find suitable source build"):
        await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})


@pytest.mark.asyncio
async def test_create_zstream_branch_commit_not_in_clone(monkeypatch):
    """ToolError with clear message when the Brew build commit is missing from the dist-git clone."""
    package = "bash"
    branch = "rhel-10.0"
    user = "bot"
    ref = "deadbeef1234"  # pragma: allowlist secret

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).mock()
    flexmock(git.cmd.Git).new_instances(gitcmd)

    mock_repo = flexmock(
        git=gitcmd,
        remotes=flexmock(
            origin=flexmock(refs=[]).should_receive("push").times(0).mock(),
        ),
    )
    mock_repo.should_receive("commit").with_args(ref).and_raise(
        git.exc.GitCommandError(["git", "rev-parse"], status=128, stderr="bad object")
    ).once()
    flexmock(git.Repo).should_receive("clone_from").and_return(mock_repo)

    async def mock_get_latest_z_pending_build(package, dist_git_branch):
        return EVR(version="1.0", release="1.el10"), ref

    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
        mock_get_latest_z_pending_build
    ).once()

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    with pytest.raises(ToolError, match="Failed to push branch to dist-git"):
        await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})


@pytest.mark.asyncio
async def test_create_zstream_branch_push_hook_rejection(monkeypatch):
    """Push rejected by server hook — full stderr surfaces in ToolError."""
    package = "bash"
    branch = "rhel-10.0"
    user = "bot"
    ref = "123456abcdef"  # pragma: allowlist secret

    async def init_kerberos_ticket():
        return f"{user}@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).mock()
    gitcmd.should_receive("push").with_args("origin", f"{ref}:refs/heads/{branch}").once().and_raise(
        git.exc.GitCommandError(
            ["git", "push"],
            status=1,
            stderr="remote: error: hook declined to update refs/heads/rhel-10.0",
        )
    )
    flexmock(git.cmd.Git).new_instances(gitcmd)

    mock_repo = flexmock(
        git=gitcmd,
        remotes=flexmock(
            origin=flexmock(refs=[]),
        ),
    )
    mock_repo.should_receive("commit").with_args(ref).and_return(flexmock()).once()
    flexmock(git.Repo).should_receive("clone_from").and_return(mock_repo)

    async def mock_get_latest_z_pending_build(package, dist_git_branch):
        return EVR(version="1.0", release="1.el10"), ref

    flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
        mock_get_latest_z_pending_build
    ).once()

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    with pytest.raises(ToolError, match="Failed to push branch to dist-git"):
        await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})


def _policy_rejection_stderr(
    branch: str,
    issues: list[tuple[str, str]],
    *,
    targets: tuple[str, ...] = ("rhel-9.9", "rhel-9.9.z"),
) -> str:
    issue_lines = "\n".join(
        f"remote: ***     {key} (FixVersion is set to: {fix_version})" for key, fix_version in issues
    )
    targets_expr = " or ".join(f"((jiraField(fixVersions) == {target}))" for target in targets)
    return (
        "Cmd('git') failed due to: exit code(1)\n"
        f"  cmdline: git push origin deadbeef:refs/heads/{branch}\n"
        "  stderr: 'remote: *** Checking commit deadbeef\n"
        "remote: *** Resolves:\n"
        "remote: ***   Unapproved:\n"
        f"{issue_lines}\n"
        "remote: *** No approved issue IDs referenced in log message or changelog\n"
        "remote: *** Unapproved issue RHEL-1 referenced in log message or changelog\n"
        "remote: *** Helpful hints to debug this error:\n"
        "remote: *** Current checkin policy requires:\n"
        f"remote:     {targets_expr}\n"
        "remote: hooklet hooks/update.secondary.d/01-gitbzverify.py failed\n"
        "remote: hooks/update.secondary died\n"
        f"remote: error: hook declined to update refs/heads/{branch}\n"
        "To ssh://pkgs.devel.redhat.com/rpms/rsync\n"
        f" ! [remote rejected] deadbeef -> {branch} (hook declined)\n"
        "error: failed to push some refs to 'ssh://pkgs.devel.redhat.com/rpms/rsync''"
    )


_POLICY_REF = "635bfccafb904d15086ca55addc8d6aa23e336d5"  # pragma: allowlist secret


async def _run_policy_rejection(
    monkeypatch,
    *,
    branch: str = "rhel-9.9.0",
    package: str = "rsync",
    ref: str = _POLICY_REF,
    issues: list[tuple[str, str]] | None = None,
    targets: tuple[str, ...] = ("rhel-9.9", "rhel-9.9.z"),
    source_branch: str | None = "rhel-9-main",
    ref_in_source_branch: bool | None = False,
    fallback_to_buildroot: bool = False,
) -> ToolError:
    """Run create_zstream_branch into a policy rejection and return the error."""
    if issues is None:
        issues = [("RHEL-248836", "rhel-9.8.z")]
    stderr = _policy_rejection_stderr(branch, issues, targets=targets)

    async def init_kerberos_ticket():
        return "bot@EXAMPLE.COM"

    flexmock(distgit_tools).should_receive("init_kerberos_ticket").replace_with(init_kerberos_ticket).once()

    gitcmd = flexmock().should_receive("ls_remote").and_return(False).mock()
    gitcmd.should_receive("push").once().and_raise(
        git.exc.GitCommandError(["git", "push"], status=1, stderr=stderr)
    )
    flexmock(git.cmd.Git).new_instances(gitcmd)

    refs = [flexmock(name=f"origin/{source_branch}")] if source_branch else []
    mock_repo = flexmock(git=gitcmd, remotes=flexmock(origin=flexmock(refs=refs)))
    mock_repo.should_receive("commit").with_args(ref).and_return(flexmock()).once()
    if source_branch:
        if ref_in_source_branch is None:
            mock_repo.should_receive("is_ancestor").and_raise(
                git.exc.GitCommandError(["git", "merge-base"], status=128, stderr="bad object")
            ).once()
        else:
            mock_repo.should_receive("is_ancestor").with_args(ref, f"origin/{source_branch}").and_return(
                ref_in_source_branch
            ).once()
    flexmock(git.Repo).should_receive("clone_from").and_return(mock_repo)

    if fallback_to_buildroot:

        async def fail_z_pending(package, dist_git_branch):
            raise NoBuildFoundError("no z-pending build")

        async def buildroot_build(package, dist_git_branch):
            return EVR(version="3.2.7", release="1.el9"), ref

        flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
            fail_z_pending
        ).once()
        flexmock(distgit_tools).should_receive("get_latest_buildroot_build").replace_with(
            buildroot_build
        ).once()
    else:

        async def z_pending_build(package, dist_git_branch):
            return EVR(version="3.2.7", release="1.el9_8"), ref

        flexmock(distgit_tools).should_receive("get_latest_z_pending_build").replace_with(
            z_pending_build
        ).once()

    if source_branch and source_branch.endswith("-main"):

        async def same_nvr(repo, pkg, build_ref, source):
            return ref

        flexmock(CreateZstreamBranchTool).should_receive("_find_latest_same_nvr_ref").replace_with(
            same_nvr
        ).once()
    else:
        flexmock(CreateZstreamBranchTool).should_receive("_find_latest_same_nvr_ref").times(0)

    monkeypatch.setenv("GITLAB_TOKEN", "<TOKEN>")

    with pytest.raises(ToolError) as excinfo:
        await CreateZstreamBranchTool().run(input={"package": package, "branch": branch})
    return excinfo.value


def test_parse_checkin_policy_rejection():
    stderr = _policy_rejection_stderr(
        "rhel-9.9.0",
        [
            ("RHEL-248836", "rhel-9.8.z"),
            ("FOO-42", "rhel-9.8.z"),
            ("RHEL-248836", "rhel-9.8.z"),
        ],
    )
    rejection = _parse_checkin_policy_rejection(stderr)

    assert rejection is not None
    assert rejection.unapproved == (
        ("RHEL-248836", "rhel-9.8.z"),
        ("FOO-42", "rhel-9.8.z"),
    )
    assert rejection.target_fix_versions == ("rhel-9.9", "rhel-9.9.z")


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param("remote: error: hook declined to update refs/heads/rhel-10.0", id="other-hook"),
        pytest.param(
            "remote: hooklet hooks/update.secondary.d/01-gitbzverify.py failed: Jira request timed out\n"
            "remote: error: hook declined to update refs/heads/rhel-10.0",
            id="gitbzverify-crash",
        ),
        pytest.param(
            "remote: *** Current checkin policy requires:\nfatal: the remote end hung up unexpectedly",
            id="not-declined",
        ),
    ],
)
def test_parse_checkin_policy_rejection_ignores_unrelated_errors(stderr):
    assert _parse_checkin_policy_rejection(stderr) is None


def test_parse_checkin_policy_rejection_drops_markup_in_fix_versions():
    stderr = _policy_rejection_stderr(
        "rhel-9.9.0",
        [("RHEL-1", "rhel-9.8.z}} [click|https://attacker.example] {{x")],
        targets=("rhel-9.9}}[x|https://attacker.example]",),
    )
    rejection = _parse_checkin_policy_rejection(stderr)

    assert rejection is not None
    assert rejection.unapproved == ()
    assert rejection.target_fix_versions == ("rhel-9.9",)


@pytest.mark.asyncio
async def test_create_zstream_branch_policy_rejection_message(monkeypatch):
    """Policy rejections get Jira wiki markup instead of raw stderr."""
    error = await _run_policy_rejection(monkeypatch)
    message = error.message

    assert "Ymir could not create the missing Z-stream branch {{rhel-9.9.0}}" in message
    assert "for {{rsync}}" in message
    assert "{{635bfccafb90}}" in message
    assert "taken from the {{rhel-9.9.0-z-pending}} tag" in message
    assert "not part of {{rhel-9-main}}" in message
    assert "targeted at {{rhel-9.8.z}}" in message
    assert "RHEL-248836" in message
    assert "check-in policy only accepts issues with fixVersion {{rhel-9.9}}, {{rhel-9.9.z}}" in message
    assert "{{gitbzverify}}" in message
    assert "have not been evened out" in message
    assert "# Even out the RHEL Z-stream and CentOS Stream branches" in message
    assert "{{ymir_todo}}" in message
    # Jira wiki markup, not Markdown or raw git output.
    assert "`" not in message
    assert "Checking commit" not in message
    assert "hooklet" not in message
    assert "stderr:" not in message

    context = error.context["additional_context"]
    assert context["error_code"] == ZSTREAM_BRANCH_POLICY_REJECTED
    assert context["user_message"] == message
    assert context["ref_in_source_branch"] == "no"
    assert context["lookup_tag"] == "rhel-9.9.0-z-pending"
    assert context["unapproved_issue_count"] == "1"
    assert context["unapproved_fix_versions"] == "rhel-9.8.z"
    assert context["target_fix_versions"] == "rhel-9.9, rhel-9.9.z"
    assert "exception" not in context
    assert "remote: *** Checking commit" in context["raw_stderr"]
    assert len(context["raw_stderr"]) <= 4000


@pytest.mark.asyncio
async def test_create_zstream_branch_policy_rejection_buildroot_fallback(monkeypatch):
    error = await _run_policy_rejection(monkeypatch, fallback_to_buildroot=True)
    message = error.message

    assert "taken from the {{rhel-9.9.0-buildrequires}} tag" in message
    assert "z-pending" not in message
    assert error.context["additional_context"]["lookup_tag"] == "rhel-9.9.0-buildrequires"


@pytest.mark.parametrize(
    "overrides, expected, unexpected",
    [
        pytest.param(
            {"source_branch": "rhel-9.10.0"},
            [
                "not part of {{rhel-9.10.0}}",
                "Do not create {{rhel-9.9.0}} from {{rhel-9.10.0}}",
                "# Create {{rhel-9.9.0}} manually from an accepted base, not from {{rhel-9.10.0}}.",
            ],
            ["have not been evened out", "CentOS Stream"],
            id="higher-zstream-source",
        ),
        pytest.param(
            {"source_branch": "rhel-9.10.0", "ref_in_source_branch": None},
            [
                "could not verify whether the commit belongs to {{rhel-9.10.0}}",
                "# Create {{rhel-9.9.0}} manually from an accepted base, not from {{rhel-9.10.0}}.",
            ],
            ["CentOS Stream"],
            id="higher-zstream-ancestry-unknown",
        ),
        pytest.param(
            {"source_branch": None, "ref_in_source_branch": None},
            [
                "could not find a newer Z-stream or a CentOS Stream branch",
                "once an appropriate source branch exists",
            ],
            ["Even out the RHEL", "part of"],
            id="no-source-branch",
        ),
        pytest.param(
            {"ref_in_source_branch": None},
            [
                "could not verify whether the commit belongs to {{rhel-9-main}}",
                "# Check whether commit {{635bfccafb90}} is part of {{rhel-9-main}}. If it is not, even out",
            ],
            ["That commit is part of", "That commit is not part of", "# Even out the RHEL"],
            id="ancestry-unknown",
        ),
        pytest.param(
            {"ref_in_source_branch": True},
            [
                "is part of {{rhel-9-main}}",
                "Review whether the referenced fixes should target {{rhel-9.9.0}}",
            ],
            ["not part of", "have not been evened out", "Even out the RHEL"],
            id="ref-in-source",
        ),
        pytest.param(
            {"unapproved": (("RHEL-100", "rhel-9.8.z"), ("RHEL-101", "rhel-9.7.z"))},
            [
                "targeted at other streams ({{rhel-9.7.z}}, {{rhel-9.8.z}})",
                "RHEL-100 (rhel-9.8.z)",
                "RHEL-101 (rhel-9.7.z)",
            ],
            [],
            id="multiple-fix-versions",
        ),
        pytest.param(
            {"unapproved": ()},
            ["did not list the individual issues"],
            ["targeted at", ":\n\n"],
            id="empty-issue-list",
        ),
        pytest.param(
            {"unapproved": tuple((f"RHEL-{900001 + index}", "rhel-9.8.z") for index in range(19))},
            ["RHEL-900001", "RHEL-900005", "and 14 more"],
            ["RHEL-900006"],
            id="truncated-issue-list",
        ),
        pytest.param(
            {"target_fix_versions": ()},
            ["Dist-git's {{gitbzverify}} hook declined the push."],
            ["only accepts"],
            id="no-policy-targets",
        ),
        pytest.param(
            {"build_ref": "1111111111112222222222223333333333334444"},  # pragma: allowlist secret
            [
                "based on commit {{635bfccafb90}}, the latest commit with the same NVR as build commit "
                "{{111111111111}} from the {{rhel-9.9.0-z-pending}} tag",
            ],
            ["taken from the"],
            id="ref-advanced-past-build",
        ),
    ],
)
def test_format_checkin_policy_rejection(overrides, expected, unexpected):
    rejection_fields = {
        "unapproved": (("RHEL-248836", "rhel-9.8.z"),),
        "target_fix_versions": ("rhel-9.9", "rhel-9.9.z"),
    }
    format_args = {"build_ref": _POLICY_REF, "source_branch": "rhel-9-main", "ref_in_source_branch": False}
    for key, value in overrides.items():
        (rejection_fields if key in rejection_fields else format_args)[key] = value

    message = _format_checkin_policy_rejection(
        package="rsync",
        branch="rhel-9.9.0",
        ref=_POLICY_REF,
        lookup_tag="rhel-9.9.0-z-pending",
        rejection=CheckinPolicyRejection(**rejection_fields),
        **format_args,
    )

    for text in expected:
        assert text in message
    for text in unexpected:
        assert text not in message
