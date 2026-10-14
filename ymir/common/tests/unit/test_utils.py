import asyncio
import subprocess

import httpx
import koji
import pytest
from flexmock import flexmock
from specfile.utils import EVR, NEVR

import ymir.common.utils as _ymir_utils
from ymir.common.base_utils import KerberosError, extract_principal, init_kerberos_ticket
from ymir.common.utils import (
    NoBuildFoundError,
    _is_connection_error,
    check_package_built_with_fixed_dependency,
    get_latest_buildroot_build,
    get_latest_candidate_build,
    get_latest_z_pending_build,
    mcp_tools,
    parse_koji_build_source,
)


async def _coro(val):
    return val


async def _noop(*args, **kwargs):
    pass


class _AsyncContextManager:
    """Helper for mocking async context managers in tests."""

    def __init__(self, return_value):
        self.return_value = return_value

    async def __aenter__(self):
        return self.return_value

    async def __aexit__(self, *args):
        return None


class TestInitKerberosTicket:
    """Test cases for init_kerberos_ticket() function."""

    @pytest.mark.asyncio
    async def test_klist_fails_no_keytab_raises_error(self, monkeypatch):
        """Test that klist failure with no keytab raises KerberosError with klist details."""
        monkeypatch.delenv("KRB5CCNAME", raising=False)
        monkeypatch.delenv("KEYTAB_FILE", raising=False)

        mock_proc = flexmock(returncode=1)
        mock_proc.should_receive("communicate").and_return(_coro((b"error output", b"stderr output")))

        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(_coro(mock_proc))

        with pytest.raises(KerberosError, match="klist exited with 1"):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_valid_ticket_in_cache_returns_principal(self, monkeypatch):
        """Test that valid ticket in cache returns the principal."""
        klist_output = (
            b"Principal name                 Cache name\n"
            b"--------------                 ----------\n"
            b"user@EXAMPLE.COM         KCM:1000\n"
        )
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(_coro(mock_proc))

        result = await init_kerberos_ticket()
        assert result == "user@EXAMPLE.COM"

    @pytest.mark.asyncio
    async def test_expired_ticket_ignored(self, monkeypatch):
        """Test that expired tickets are ignored."""
        klist_output = (
            b"Principal name                 Cache name\n"
            b"--------------                 ----------\n"
            b"user@EXAMPLE.COM         FILE:.secrets/ccache/krb5cc (Expired)\n"
        )
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(_coro(mock_proc))

        with pytest.raises(
            KerberosError,
            match="No valid Kerberos ticket found and KEYTAB_FILE is not set",
        ):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_no_tickets_in_cache(self, monkeypatch):
        """Test behavior when klist returns no tickets."""
        klist_output = (
            b"Principal name                 Cache name\n--------------                 ----------\n"
        )
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(_coro(mock_proc))

        with pytest.raises(
            KerberosError,
            match="No valid Kerberos ticket found and KEYTAB_FILE is not set",
        ):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_keytab_principal_already_in_cache(self, monkeypatch):
        """Test that existing keytab principal in cache is used."""
        klist_output = (
            b"Principal name                 Cache name\n"
            b"--------------                 ----------\n"
            b"jotnar-bot@IPA.REDHAT.COM    KCM:1000\n"
        )
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        monkeypatch.setenv("KEYTAB_FILE", "/path/to/keytab")
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(_coro(mock_proc))

        from ymir.common import base_utils

        flexmock(base_utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
            _coro("jotnar-bot@IPA.REDHAT.COM")
        )

        result = await init_kerberos_ticket()
        assert result == "jotnar-bot@IPA.REDHAT.COM"

    @pytest.mark.asyncio
    async def test_keytab_kinit_success(self, monkeypatch):
        """Test successful kinit with keytab when principal not in cache."""
        klist_output = (
            b"Principal name                 Cache name\n--------------                 ----------\n"
        )
        mock_klist_proc = flexmock(returncode=0)
        mock_klist_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        mock_kinit_proc = flexmock(returncode=0)
        mock_kinit_proc.should_receive("communicate").and_return(_coro((b"error output", b"stderr output")))

        monkeypatch.setenv("KEYTAB_FILE", "/path/to/keytab")

        def mock_create_subprocess(*args, **kwargs):
            if args[0] == "klist":
                return _coro(mock_klist_proc)
            if args[0] == "kinit":
                return _coro(mock_kinit_proc)
            return None

        flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(mock_create_subprocess)

        from ymir.common import base_utils

        flexmock(base_utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
            _coro("jotnar-bot@IPA.REDHAT.COM")
        )

        result = await init_kerberos_ticket()
        assert result == "jotnar-bot@IPA.REDHAT.COM"

    @pytest.mark.asyncio
    async def test_keytab_kinit_failure(self, monkeypatch):
        """Test kinit failure with keytab raises error."""
        klist_output = (
            b"Principal name                 Cache name\n--------------                 ----------\n"
        )
        mock_klist_proc = flexmock(returncode=0)
        mock_klist_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        mock_kinit_proc = flexmock(returncode=1)
        mock_kinit_proc.should_receive("communicate").and_return(_coro((b"error output", b"stderr output")))

        monkeypatch.setenv("KEYTAB_FILE", "/path/to/keytab")

        def mock_create_subprocess(*args, **kwargs):
            if args[0] == "klist":
                return _coro(mock_klist_proc)
            if args[0] == "kinit":
                return _coro(mock_kinit_proc)
            return None

        flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(mock_create_subprocess)

        from ymir.common import base_utils

        flexmock(base_utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
            _coro("jotnar-bot@IPA.REDHAT.COM")
        )

        with pytest.raises(KerberosError, match="kinit command failed"):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_keytab_extract_principal_failure(self, monkeypatch):
        """Test extract_principal failure raises error."""
        monkeypatch.setenv("KEYTAB_FILE", "/path/to/keytab")

        from ymir.common import base_utils

        flexmock(base_utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
            _coro(None)
        )

        with pytest.raises(KerberosError, match="Failed to extract principal from keytab file"):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_no_krb5ccname_finds_keyring_ticket(self, monkeypatch):
        """Test that tickets are found via system default cache (e.g. KEYRING)
        when KRB5CCNAME is not set."""
        klist_output = (
            b"Principal name                 Cache name\n"
            b"--------------                 ----------\n"
            b"user@EXAMPLE.COM         KEYRING:persistent:1000\n"
        )
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        monkeypatch.delenv("KRB5CCNAME", raising=False)
        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(_coro(mock_proc))

        result = await init_kerberos_ticket()
        assert result == "user@EXAMPLE.COM"

    @pytest.mark.asyncio
    async def test_multiple_valid_principals_returns_first(self, monkeypatch):
        """Test that first valid principal is returned when multiple exist."""
        klist_output = (
            b"Principal name                 Cache name\n"
            b"--------------                 ----------\n"
            b"user1@EXAMPLE.COM         KCM:1000\n"
            b"user2@EXAMPLE.COM         KCM:1001\n"
        )
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(_coro(mock_proc))

        result = await init_kerberos_ticket()
        assert result == "user1@EXAMPLE.COM"

    @pytest.mark.asyncio
    async def test_mixed_valid_and_expired_principals(self, monkeypatch):
        """Test that expired principals are ignored and valid ones are used."""
        klist_output = (
            b"Principal name                 Cache name\n"
            b"--------------                 ----------\n"
            b"expired@EXAMPLE.COM      FILE:.secrets/ccache/krb5cc (Expired)\n"
            b"valid@EXAMPLE.COM        KCM:1000\n"
        )
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(_coro(mock_proc))

        result = await init_kerberos_ticket()
        assert result == "valid@EXAMPLE.COM"


class TestExtractPrincipal:
    """Test cases for extract_principal() helper function."""

    @pytest.mark.asyncio
    async def test_extract_principal_success(self):
        """Test successful principal extraction from keytab."""
        klist_output = (
            b"Keytab name: FILE:openshift/jotnar-bot.keytab\n"
            b"KVNO Principal\n"
            b"---- --------------------------------------------------------------------------\n"
            b"   2 jotnar-bot@IPA.REDHAT.COM (aes256-cts-hmac-sha1-96)  "
            b"(0xabcdef0000000000000000000000000000000000000000000000000000000000)\n"
            b"   2 jotnar-bot@IPA.REDHAT.COM (aes128-cts-hmac-sha1-96)  "
            b"(0xabcdef000000000000000000000000000)\n"
        )
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist",
            "-k",
            "-K",
            "-e",
            "/path/to/keytab",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).and_return(_coro(mock_proc))

        result = await extract_principal("/path/to/keytab")
        assert result == "jotnar-bot@IPA.REDHAT.COM"

    @pytest.mark.asyncio
    async def test_extract_principal_klist_failure(self):
        """Test extract_principal when klist command fails."""
        mock_proc = flexmock(returncode=1)
        mock_proc.should_receive("communicate").and_return(_coro((b"error", b"stderr")))

        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist",
            "-k",
            "-K",
            "-e",
            "/path/to/keytab",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).and_return(_coro(mock_proc))

        with pytest.raises(KerberosError, match="klist command failed"):
            await extract_principal("/path/to/keytab")

    @pytest.mark.asyncio
    async def test_extract_principal_no_valid_key(self):
        """Test extract_principal when no valid key found in output."""
        klist_output = (
            b"Keytab name: FILE:openshift/jotnar-bot.keytab\n"
            b"KVNO Principal\n"
            b"---- --------------------------------------------------------------------------\n"
        )
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist",
            "-k",
            "-K",
            "-e",
            "/path/to/keytab",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).and_return(_coro(mock_proc))

        with pytest.raises(KerberosError, match="No valid key found in the keytab file"):
            await extract_principal("/path/to/keytab")


# ============================================================================
# _is_connection_error
# ============================================================================


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectError("refused"), True),
        (ConnectionError("reset"), True),
        (OSError("network unreachable"), True),
        (ValueError("bad value"), False),
        (RuntimeError("oops"), False),
        (ExceptionGroup("task group", [httpx.ConnectError("refused")]), True),
        (ExceptionGroup("task group", [ValueError("bad value")]), False),
        (ExceptionGroup("outer", [ExceptionGroup("inner", [httpx.ConnectError("refused")])]), True),
    ],
)
def test_is_connection_error(exc, expected):
    assert _is_connection_error(exc) == expected


# ============================================================================
# mcp_tools retry logic
# ============================================================================

FAKE_URL = "http://mcp-gateway:8000/sse"
FAKE_TOOLS = [flexmock()]


class _SSEContextManager:
    def __init__(self, exc=None, wrap_body_error=False):
        self._exc = exc
        self._wrap_body_error = wrap_body_error

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return flexmock(), flexmock()

    async def __aexit__(self, _exc_type, exc, _traceback):
        if exc and self._wrap_body_error:
            raise ExceptionGroup("SSE task group", [exc])
        return False


def make_sse_cm(exc=None, wrap_body_error=False):
    """Async context manager mock for sse_client. Raises exc on entry if given."""
    return _SSEContextManager(exc, wrap_body_error)


class _SessionContextManager:
    async def __aenter__(self):
        session = flexmock()
        session.should_receive("initialize").and_return(_coro(None))
        return session

    async def __aexit__(self, *args):
        return False


def make_session_cm():
    """Async context manager mock for ClientSession, yielding an async session."""
    return _SessionContextManager()


@pytest.mark.asyncio
async def test_mcp_tools_success_on_first_attempt():
    """Connected immediately; sleep is never called."""
    flexmock(_ymir_utils).should_receive("sse_client").once().and_return(make_sse_cm())
    flexmock(_ymir_utils).should_receive("ClientSession").and_return(make_session_cm())
    flexmock(_ymir_utils.MCPTool).should_receive("from_session").and_return(_coro(FAKE_TOOLS))
    flexmock(asyncio).should_receive("sleep").never()

    async with mcp_tools(FAKE_URL) as tools:
        assert tools == FAKE_TOOLS


@pytest.mark.asyncio
async def test_mcp_tools_retries_once_then_succeeds():
    """First attempt raises ConnectError; second attempt succeeds."""
    conn_err = httpx.ConnectError("refused")
    flexmock(_ymir_utils).should_receive("sse_client").twice().and_return(
        make_sse_cm(exc=conn_err)
    ).and_return(make_sse_cm())
    flexmock(_ymir_utils).should_receive("ClientSession").and_return(make_session_cm())
    flexmock(_ymir_utils.MCPTool).should_receive("from_session").and_return(_coro(FAKE_TOOLS))
    flexmock(asyncio).should_receive("sleep").once().with_args(3.0).replace_with(_noop)

    async with mcp_tools(FAKE_URL, retry_delay=3.0) as tools:
        assert tools == FAKE_TOOLS


@pytest.mark.asyncio
async def test_mcp_tools_exhausts_retries_and_raises():
    """All attempts fail; ConnectError propagates after max_retries exhausted."""
    conn_err = httpx.ConnectError("refused")
    flexmock(_ymir_utils).should_receive("sse_client").times(3).and_return(make_sse_cm(exc=conn_err))
    flexmock(asyncio).should_receive("sleep").times(2).with_args(2.0).replace_with(_noop)

    with pytest.raises(httpx.ConnectError):
        async with mcp_tools(FAKE_URL, max_retries=3, retry_delay=2.0):
            pass


@pytest.mark.asyncio
async def test_mcp_tools_non_connection_error_raises_immediately():
    """A non-connection error on first attempt is not retried."""
    flexmock(_ymir_utils).should_receive("sse_client").once().and_return(
        make_sse_cm(exc=ValueError("unexpected"))
    )
    flexmock(asyncio).should_receive("sleep").never()

    with pytest.raises(ValueError):
        async with mcp_tools(FAKE_URL, max_retries=5):
            pass


@pytest.mark.asyncio
async def test_mcp_tools_preserves_exception_raised_by_caller():
    error = ValueError("caller failed")
    flexmock(_ymir_utils).should_receive("sse_client").once().and_return(make_sse_cm(wrap_body_error=True))
    flexmock(_ymir_utils).should_receive("ClientSession").and_return(make_session_cm())
    flexmock(_ymir_utils.MCPTool).should_receive("from_session").and_return(_coro(FAKE_TOOLS))

    with pytest.raises(ValueError) as exc_info:
        async with mcp_tools(FAKE_URL):
            raise error

    assert exc_info.value is error


# ============================================================================
# get_latest_candidate_build
# ============================================================================


def test_parse_koji_build_source():
    assert parse_koji_build_source({"source": "git+https://gitlab.com/redhat/rhel/rpms/bash#abc123"}) == (
        "git+https://gitlab.com/redhat/rhel/rpms/bash",
        "abc123",
    )


@pytest.mark.parametrize(
    "source",
    [None, "", "git+https://gitlab.com/redhat/rhel/rpms/bash", "#abc123"],
)
def test_parse_koji_build_source_rejects_invalid_metadata(source):
    with pytest.raises(ValueError, match="source"):
        parse_koji_build_source({"source": source})


def _mock_koji_session(list_tagged_results, get_build_result):
    flexmock(koji).should_receive("ClientSession").and_return(
        flexmock(
            listTagged=lambda **kw: list_tagged_results.get(kw["tag"], []),
            getBuild=lambda *a, **kw: get_build_result,
        ),
    )


@pytest.mark.asyncio
async def test_get_latest_candidate_build_picks_higher_evr():
    _mock_koji_session(
        {
            "rhel-9.6.0-candidate": [
                {"build_id": 1, "epoch": 0, "version": "1.0", "release": "1.el9"},
            ],
            "rhel-9.6.0-z-candidate": [
                {"build_id": 2, "epoch": 0, "version": "1.0", "release": "2.el9"},
            ],
        },
        {"source": "git+https://pkgs.example.com/rpms/bash#abc123"},
    )
    evr, ref = await get_latest_candidate_build("bash", "rhel-9.6.0")
    assert evr == EVR(epoch=0, version="1.0", release="2.el9")
    assert ref == "abc123"


@pytest.mark.asyncio
async def test_get_latest_candidate_build_only_candidate():
    _mock_koji_session(
        {
            "rhel-9.6.0-candidate": [
                {"build_id": 1, "epoch": 0, "version": "1.0", "release": "1.el9"},
            ],
            "rhel-9.6.0-z-candidate": [],
        },
        {"source": "git+https://pkgs.example.com/rpms/bash#def456"},
    )
    evr, ref = await get_latest_candidate_build("bash", "rhel-9.6.0")
    assert evr == EVR(epoch=0, version="1.0", release="1.el9")
    assert ref == "def456"


@pytest.mark.asyncio
async def test_get_latest_candidate_build_no_builds():
    _mock_koji_session(
        {"rhel-9.6.0-candidate": [], "rhel-9.6.0-z-candidate": []},
        None,
    )
    with pytest.raises(NoBuildFoundError, match="no builds"):
        await get_latest_candidate_build("bash", "rhel-9.6.0")


# ============================================================================
# get_latest_z_pending_build
# ============================================================================


@pytest.mark.asyncio
async def test_get_latest_z_pending_build():
    _mock_koji_session(
        {
            "rhel-9.6.0-z-pending": [
                {"build_id": 1, "epoch": 0, "version": "1.0", "release": "1.el9"},
            ],
        },
        {"source": "git+https://pkgs.example.com/rpms/bash#abc123"},
    )
    evr, ref = await get_latest_z_pending_build("bash", "rhel-9.6.0")
    assert evr == EVR(epoch=0, version="1.0", release="1.el9")
    assert ref == "abc123"


@pytest.mark.asyncio
async def test_get_latest_z_pending_build_no_builds():
    _mock_koji_session(
        {"rhel-9.6.0-z-pending": []},
        None,
    )
    with pytest.raises(NoBuildFoundError, match="no builds"):
        await get_latest_z_pending_build("bash", "rhel-9.6.0")


@pytest.mark.asyncio
async def test_get_latest_buildroot_build():
    _mock_koji_session(
        {
            "rhel-9.6.0-buildrequires": [
                {"build_id": 1, "epoch": 0, "version": "1.0", "release": "1.el9"},
            ],
        },
        {"source": "git+https://pkgs.example.com/rpms/bash#abc123"},
    )

    evr, ref = await get_latest_buildroot_build("bash", "rhel-9.6.0")

    assert evr == EVR(epoch=0, version="1.0", release="1.el9")
    assert ref == "abc123"


# ============================================================================
# check_package_built_with_fixed_dependency
# ============================================================================


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_already_fixed():
    """Test when package was built with fixed or newer dependency."""
    mock_tool = flexmock()

    # Mock Jira search result with multiple builds
    jira_search_result = [
        {
            "key": "RHEL-242375",
            "fields": {
                "customfield_10578": "go-fdo-client-1.0.0-4.el10_2.7",
            },
        },
        {
            "key": "RHEL-242374",
            "fields": {
                "customfield_10578": "go-fdo-client-1.0.0-3.el10_2",
            },
        },
    ]

    # Mock installed_pkgs.log content
    installed_pkgs_log_content = b"""golang-1.26.4-1.el10_2.x86_64 1 2 abc installed
other-package-1.0-1.el10_2.x86_64 1 2 abc installed
"""

    # Mock Koji build info for candidate selection (EVR comparison)
    candidate1_build = {
        "name": "go-fdo-client",
        "build_id": 99910,
        "epoch": None,
        "version": "1.0.0",
        "release": "4.el10_2.7",
    }
    candidate2_build = {
        "name": "go-fdo-client",
        "build_id": 99911,
        "epoch": None,
        "version": "1.0.0",
        "release": "3.el10_2",
    }
    fixed_build = {
        "name": "golang",
        "epoch": None,
        "version": "1.26.4",
        "release": "1.el10_2",
        "build_id": 123456,
    }
    used_build = {"name": "golang", "epoch": None, "version": "1.26.4", "release": "1.el10_2"}

    # Mock run_tool to return fresh coroutines each call (called twice: closed + active)
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock Koji listRPMs for architecture lookup (buildID-aware to handle both packages)
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [{"arch": "x86_64", "name": "go-fdo-client", "nvr": "go-fdo-client-1.0.0-4.el10_2.7"}]
            if buildID == 99910
            else [{"arch": "x86_64", "name": "golang", "nvr": "golang-1.26.4-1.el10_2"}]
        )
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    # Mock httpx client for installed_pkgs.log fetch
    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()

    # Create a proper async context manager mock
    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    # Mock Koji getBuild with argument-aware mapping
    nvr_to_build = {
        "go-fdo-client-1.0.0-4.el10_2.7": candidate1_build,
        "go-fdo-client-1.0.0-3.el10_2": candidate2_build,
        "golang-1.26.4-1.el10_2": fixed_build,
    }

    def mock_get_koji_build(url, nvr):
        # Return from map or used_build as fallback
        return nvr_to_build.get(nvr, used_build)

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_koji_build)

    already_fixed, issue_key, _nvr, _reason = await check_package_built_with_fixed_dependency(
        package="go-fdo-client",
        fix_version="rhel-10.2.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.26.4-1.el10_2"),
        available_tools=[mock_tool],
    )

    assert already_fixed is True
    assert issue_key == "RHEL-242375"
    assert _nvr == "go-fdo-client-1.0.0-4.el10_2.7"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_subpackage_match_needs_verification():
    """Test when dependency is matched via subpackage — needs manual verification."""
    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-242375",
            "fields": {
                "customfield_10578": "go-fdo-client-1.0.0-4.el10_2.7",
            },
        },
    ]

    # installed_pkgs.log has golang-bin (subpackage) but NOT golang (source component)
    installed_pkgs_log_content = b"""golang-bin-1.26.4-1.el10_2.x86_64 1 2 abc installed
other-package-1.0-1.el10_2.x86_64 1 2 abc installed
"""

    candidate1_build = {
        "name": "go-fdo-client",
        "build_id": 99910,
        "epoch": None,
        "version": "1.0.0",
        "release": "4.el10_2.7",
    }
    fixed_build = {
        "name": "golang",
        "epoch": None,
        "version": "1.26.4",
        "release": "1.el10_2",
        "build_id": 123456,
    }
    used_build = {"name": "golang", "epoch": None, "version": "1.26.4", "release": "1.el10_2"}

    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # listRPMs: package build returns arch info, fixed dep returns golang + golang-bin subpackages
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [{"arch": "x86_64", "name": "go-fdo-client", "nvr": "go-fdo-client-1.0.0-4.el10_2.7"}]
            if buildID == 99910
            else [
                {"arch": "x86_64", "name": "golang", "nvr": "golang-1.26.4-1.el10_2"},
                {"arch": "x86_64", "name": "golang-bin", "nvr": "golang-bin-1.26.4-1.el10_2"},
            ]
        )
    )
    # getRPM for subpackage resolution (golang-bin -> source RPM golang)
    mock_koji_session.should_receive("getRPM").with_args(
        {"name": "golang-bin", "version": "1.26.4", "release": "1.el10_2", "arch": "x86_64"}
    ).and_return({"name": "golang-bin", "build_id": 123456, "epoch": None})
    mock_koji_session.should_receive("getBuild").with_args(123456).and_return(
        {"name": "golang", "version": "1.26.4", "release": "1.el10_2", "epoch": None}
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()
    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    nvr_to_build = {
        "go-fdo-client-1.0.0-4.el10_2.7": candidate1_build,
        "golang-1.26.4-1.el10_2": fixed_build,
    }

    def mock_get_koji_build(url, nvr):
        return nvr_to_build.get(nvr, used_build)

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_koji_build)

    already_fixed, issue_key, _nvr, reason = await check_package_built_with_fixed_dependency(
        package="go-fdo-client",
        fix_version="rhel-10.2.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.26.4-1.el10_2"),
        available_tools=[mock_tool],
    )

    assert already_fixed is None
    assert issue_key == "RHEL-242375"
    assert _nvr == "go-fdo-client-1.0.0-4.el10_2.7"
    assert reason == "subpackage_match_needs_verification:golang-bin"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_direct_component_match_with_subpackages():
    """Test when component name is installed but subpackages exist — needs verification."""
    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-242375",
            "fields": {
                "customfield_10578": "go-fdo-client-1.0.0-4.el10_2.7",
            },
        },
    ]

    # installed_pkgs.log has golang (source component) only, NOT golang-bin (subpackage).
    # Even though golang produces subpackages, only the component itself is installed here,
    # so this is a direct match and should confirm already fixed.
    installed_pkgs_log_content = b"""golang-1.26.4-1.el10_2.x86_64 1 2 abc installed
other-package-1.0-1.el10_2.x86_64 1 2 abc installed
"""

    candidate1_build = {
        "name": "go-fdo-client",
        "build_id": 99910,
        "epoch": None,
        "version": "1.0.0",
        "release": "4.el10_2.7",
    }
    fixed_build = {
        "name": "golang",
        "epoch": None,
        "version": "1.26.4",
        "release": "1.el10_2",
        "build_id": 123456,
    }
    used_build = {"name": "golang", "epoch": None, "version": "1.26.4", "release": "1.el10_2"}

    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # listRPMs: package build returns arch info, fixed dep returns golang + golang-bin
    # (component produces subpackages, but only the component itself is installed)
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [{"arch": "x86_64", "name": "go-fdo-client", "nvr": "go-fdo-client-1.0.0-4.el10_2.7"}]
            if buildID == 99910
            else [
                {"arch": "x86_64", "name": "golang", "nvr": "golang-1.26.4-1.el10_2"},
                {"arch": "x86_64", "name": "golang-bin", "nvr": "golang-bin-1.26.4-1.el10_2"},
            ]
        )
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()
    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    nvr_to_build = {
        "go-fdo-client-1.0.0-4.el10_2.7": candidate1_build,
        "golang-1.26.4-1.el10_2": fixed_build,
    }

    def mock_get_koji_build(url, nvr):
        return nvr_to_build.get(nvr, used_build)

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_koji_build)

    already_fixed, issue_key, _nvr, reason = await check_package_built_with_fixed_dependency(
        package="go-fdo-client",
        fix_version="rhel-10.2.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.26.4-1.el10_2"),
        available_tools=[mock_tool],
    )

    assert already_fixed is None
    assert issue_key == "RHEL-242375"
    assert _nvr == "go-fdo-client-1.0.0-4.el10_2.7"
    assert reason == "subpackage_match_needs_verification:golang"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_real_glibc_with_subpackages():
    """Test with real-world glibc data — component has subpackages, needs verification."""
    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-242375",
            "fields": {
                "customfield_10578": "some-app-1.0.0-1.el8",
            },
        },
    ]

    # Real installed_pkgs.log content from Brew build with glibc subpackages
    installed_pkgs_log_content = b"\n".join(
        [
            b"glibc-all-langpacks-2.28-248.el8.ppc64le 1704283999 436421312 fbb523 installed",
            b"glibc-common-2.28-248.el8.ppc64le 1704283999 8436969 b26881 installed",
            b"glibc-gconv-extra-2.28-248.el8.ppc64le 1704283999 19403790 d588bb installed",
            b"glibc-2.28-248.el8.ppc64le 1704283999 11533664 d3adce installed",
            b"glibc-headers-2.28-248.el8.ppc64le 1704283999 2017478 218074 installed",
            b"glibc-devel-2.28-248.el8.ppc64le 1704283999 1704692 c9c55b installed",
            b"",
        ]
    )

    candidate_build = {
        "name": "some-app",
        "build_id": 99910,
        "epoch": None,
        "version": "1.0.0",
        "release": "1.el8",
    }
    fixed_build = {
        "name": "glibc",
        "epoch": None,
        "version": "2.28",
        "release": "248.el8",
        "build_id": 123456,
    }
    used_build = {"name": "glibc", "epoch": None, "version": "2.28", "release": "248.el8"}

    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # listRPMs: package build returns arch info, fixed dep returns glibc and all subpackages
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [{"arch": "ppc64le", "name": "some-app", "nvr": "some-app-1.0.0-1.el8"}]
            if buildID == 99910
            else [
                {"arch": "ppc64le", "name": "glibc", "nvr": "glibc-2.28-248.el8"},
                {"arch": "ppc64le", "name": "glibc-all-langpacks", "nvr": "glibc-all-langpacks-2.28-248.el8"},
                {"arch": "ppc64le", "name": "glibc-common", "nvr": "glibc-common-2.28-248.el8"},
                {"arch": "ppc64le", "name": "glibc-gconv-extra", "nvr": "glibc-gconv-extra-2.28-248.el8"},
                {"arch": "ppc64le", "name": "glibc-headers", "nvr": "glibc-headers-2.28-248.el8"},
                {"arch": "ppc64le", "name": "glibc-devel", "nvr": "glibc-devel-2.28-248.el8"},
            ]
        )
    )
    # getRPM: resolve binary RPMs to their source RPM info
    # getRPM receives a dict like {"name": "pkg", "version": "ver", "release": "rel", "arch": "arch"}
    mock_koji_session.should_receive("getRPM").replace_with(
        lambda rpm_dict: (
            {
                "build_id": 123456,
                "name": rpm_dict["name"],
            }
            if rpm_dict["name"].startswith("glibc")
            else None
        )
    )
    mock_koji_session.should_receive("getBuild").replace_with(
        lambda buildID: fixed_build if buildID == 123456 else None
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()
    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    nvr_to_build = {
        "some-app-1.0.0-1.el8": candidate_build,
        "glibc-2.28-248.el8": fixed_build,
    }

    def mock_get_koji_build(url, nvr):
        return nvr_to_build.get(nvr, used_build)

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_koji_build)

    already_fixed, issue_key, _nvr, reason = await check_package_built_with_fixed_dependency(
        package="some-app",
        fix_version="rhel-8.0.z",
        dep_component="glibc",
        fixed_dep_nvr=NEVR.from_string("glibc-2.28-248.el8"),
        available_tools=[mock_tool],
    )

    assert already_fixed is None
    assert issue_key == "RHEL-242375"
    assert _nvr == "some-app-1.0.0-1.el8"
    # First matching subpackage is enough to request verification
    assert reason == "subpackage_match_needs_verification:glibc-all-langpacks"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_needs_rebuild():
    """Test when package was built with older dependency."""
    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-242375",
            "fields": {
                "customfield_10578": "go-fdo-client-1.0.0-4.el10_2.7",
                "updated": "2026-06-20T10:00:00.000+0000",
            },
        }
    ]

    # installed_pkgs.log shows older golang version was used
    installed_pkgs_log_content = b"""golang-1.26.3-1.el10_2.x86_64 1 2 abc installed
other-package-1.0-1.el10_2.x86_64 1 2 abc installed
"""

    candidate_build = {
        "name": "go-fdo-client",
        "build_id": 99901,
        "epoch": None,
        "version": "1.0.0",
        "release": "4.el10_2.7",
    }
    fixed_build = {
        "name": "golang",
        "epoch": None,
        "version": "1.26.4",
        "release": "1.el10_2",
        "build_id": 123456,
    }
    used_build = {"name": "golang", "epoch": None, "version": "1.26.3", "release": "1.el10_2"}

    # Mock run_tool to return fresh coroutines each call (called twice: closed + active)
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock Koji listRPMs for architecture lookup and subpackage lookup
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [{"arch": "x86_64", "name": "go-fdo-client", "nvr": "go-fdo-client-1.0.0-4.el10_2.7"}]
            if buildID == 99901
            else [{"arch": "x86_64", "name": "golang", "nvr": "golang-1.26.4-1.el10_2"}]
        )
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    # Mock _get_koji_build with argument-aware function
    def mock_get_build(url, nvr):
        if nvr == "go-fdo-client-1.0.0-4.el10_2.7":
            return candidate_build
        if nvr == "golang-1.26.4-1.el10_2":
            return fixed_build
        if nvr == "golang-1.26.3-1.el10_2":
            return used_build
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, issue_key, _nvr, _reason = await check_package_built_with_fixed_dependency(
        package="go-fdo-client",
        fix_version="rhel-10.2.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.26.4-1.el10_2"),
        available_tools=[mock_tool],
    )

    assert already_fixed is False
    assert issue_key == "RHEL-242375"
    assert _nvr == "go-fdo-client-1.0.0-4.el10_2.7"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_no_build_found():
    """Test when no completed build exists for the package."""
    mock_tool = flexmock()

    async def mock_run_tool(tool, **kwargs):
        return []

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(mock_run_tool)

    already_fixed, issue_key, _nvr, _reason = await check_package_built_with_fixed_dependency(
        package="go-fdo-client",
        fix_version="rhel-10.2.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.26.4-1.el10_2"),
        available_tools=[mock_tool],
    )

    assert already_fixed is False
    assert issue_key is None
    assert _nvr is None


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_no_installed_pkgs_log_built_after_fix():
    """A later build without installed_pkgs.log still needs manual verification."""
    from datetime import datetime

    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-228612",
            "fields": {
                "customfield_10578": "git-lfs-3.4.1-13.el8_10",
            },
        }
    ]

    candidate_build = {
        "name": "git-lfs",
        "build_id": 12345,
        "epoch": None,
        "version": "3.4.1",
        "release": "13.el8_10",
        "completion_time": datetime(2026, 8, 25, 10, 0, 0),  # After golang fix
        "completion_ts": 1756162800.0,  # Recent build
    }
    fixed_build = {
        "name": "golang",
        "epoch": None,
        "version": "1.26.7",
        "release": "1.el10",
        "completion_time": datetime(2026, 8, 24, 15, 0, 0),
    }

    # Mock run_tool to return fresh coroutines each call (called twice: closed + active)
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock koji.ClientSession.listRPMs to return noarch-only RPMs
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").with_args(buildID=12345).and_return(
        [{"arch": "noarch", "name": "git-lfs", "nvr": "git-lfs-3.4.1-13.el8_10"}]
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    # Mock installed_pkgs.log fetch to return 404 (no log available)
    mock_head_response = flexmock(status_code=404)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    # Mock _get_koji_build for both arch lookup and timestamp comparison
    def mock_get_build(url, nvr):
        if nvr == "git-lfs-3.4.1-13.el8_10":
            return candidate_build
        if nvr == "golang-1.26.7-1.el10":
            return fixed_build
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, issue_key, _nvr, reason = await check_package_built_with_fixed_dependency(
        package="git-lfs",
        fix_version="rhel-8.10.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.26.7-1.el10"),
        available_tools=[mock_tool],
    )

    # Should return None (needs manual verification) since built after fix but no installed_pkgs.log
    assert already_fixed is None
    assert reason == "built_after_fix_no_installed_pkgs_log"
    assert issue_key == "RHEL-228612"
    assert _nvr == "git-lfs-3.4.1-13.el8_10"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_no_installed_pkgs_log_built_before_fix():
    """Test when installed_pkgs.log unavailable and package built before fix - returns False for rebuild."""
    from datetime import datetime

    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-228612",
            "fields": {
                "customfield_10578": "git-lfs-3.4.1-12.el8_10",
            },
        }
    ]

    candidate_build = {
        "name": "git-lfs",
        "build_id": 12346,
        "epoch": None,
        "version": "3.4.1",
        "release": "12.el8_10",
        "completion_time": datetime(2026, 7, 9, 13, 0, 0),  # Before golang fix
        "completion_ts": 1752058800.0,  # Recent build
    }
    fixed_build = {
        "name": "golang",
        "epoch": None,
        "version": "1.26.7",
        "release": "1.el10",
        "completion_time": datetime(2026, 8, 24, 15, 0, 0),
    }

    # Mock run_tool to return fresh coroutines each call (called twice: closed + active)
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock koji.ClientSession.listRPMs to return noarch-only RPMs
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").with_args(buildID=12346).and_return(
        [{"arch": "noarch", "name": "git-lfs", "nvr": "git-lfs-3.4.1-12.el8_10"}]
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    # Mock installed_pkgs.log fetch to return 404
    mock_head_response = flexmock(status_code=404)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    # Mock _get_koji_build for both arch lookup and timestamp comparison
    def mock_get_build(url, nvr):
        if nvr == "git-lfs-3.4.1-12.el8_10":
            return candidate_build
        if nvr == "golang-1.26.7-1.el10":
            return fixed_build
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, issue_key, _nvr, _reason = await check_package_built_with_fixed_dependency(
        package="git-lfs",
        fix_version="rhel-8.10.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.26.7-1.el10"),
        available_tools=[mock_tool],
    )

    # Should return False (needs rebuild) since built before fix
    assert already_fixed is False
    assert issue_key == "RHEL-228612"
    assert _nvr == "git-lfs-3.4.1-12.el8_10"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_gzipped_log():
    """Test handling of gzipped installed_pkgs.log files."""
    import gzip

    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-242375",
            "fields": {
                "customfield_10578": "go-fdo-client-1.0.0-4.el10_2.7",
            },
        }
    ]

    # Create gzipped content
    log_content = b"golang-1.26.4-1.el10_2.x86_64 1 2 abc installed\n"
    gzipped_content = gzip.compress(log_content)

    candidate_build = {
        "name": "go-fdo-client",
        "build_id": 99902,
        "epoch": None,
        "version": "1.0.0",
        "release": "4.el10_2.7",
    }
    fixed_build = {
        "name": "golang",
        "epoch": None,
        "version": "1.26.4",
        "release": "1.el10_2",
        "build_id": 1234,
    }

    # Mock run_tool to return fresh coroutines each call (called twice: closed + active)
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock Koji listRPMs for architecture lookup and _get_known_package_names
    mock_session = flexmock()
    mock_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [{"arch": "x86_64", "name": "go-fdo-client", "nvr": "go-fdo-client-1.0.0-4.el10_2.7"}]
            if buildID == 99902
            else [{"arch": "x86_64", "name": "golang", "nvr": "golang-1.26.4-1.el10_2"}]
        )
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_session)

    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=gzipped_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    # Mock _get_koji_build with argument-aware function
    def mock_get_build(url, nvr):
        if nvr == "go-fdo-client-1.0.0-4.el10_2.7":
            return candidate_build
        if nvr == "golang-1.26.4-1.el10_2":
            # Return fixed_build for all calls to this NVR
            return fixed_build
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, issue_key, _nvr, _reason = await check_package_built_with_fixed_dependency(
        package="go-fdo-client",
        fix_version="rhel-10.2.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.26.4-1.el10_2"),
        available_tools=[mock_tool],
    )

    assert already_fixed is True
    assert issue_key == "RHEL-242375"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_dotted_package_name():
    """Test handling of package names containing periods (e.g., python3.11)."""
    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-123456",
            "fields": {
                "customfield_10578": "some-app-1.0.0-1.el10_2",
            },
        }
    ]

    # installed_pkgs.log with dotted package name
    installed_pkgs_log_content = b"""python3.11-3.11.9-1.el10_2.x86_64 1 2 abc installed
other-package-1.0-1.el10_2.x86_64 1 2 abc installed
"""

    candidate_build = {
        "name": "some-app",
        "build_id": 99903,
        "epoch": None,
        "version": "1.0.0",
        "release": "1.el10_2",
    }
    fixed_build = {
        "name": "python3.11",
        "epoch": None,
        "version": "3.11.9",
        "release": "1.el10_2",
        "build_id": 123456,
    }
    # Mock run_tool to return fresh coroutines each call (called twice: closed + active)
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock Koji listRPMs for candidate build architecture lookup (buildID-aware)
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [{"arch": "x86_64", "name": "some-app", "nvr": "some-app-1.0.0-1.el10_2"}]
            if buildID == 99903
            else [{"arch": "x86_64", "name": "python3.11", "nvr": "python3.11-3.11.9-1.el10_2"}]
        )
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    # Mock _get_koji_build with argument-aware function
    def mock_get_build(url, nvr):
        if nvr == "some-app-1.0.0-1.el10_2":
            return candidate_build
        if nvr == "python3.11-3.11.9-1.el10_2":
            return fixed_build
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, issue_key, _nvr, _reason = await check_package_built_with_fixed_dependency(
        package="some-app",
        fix_version="rhel-10.2.z",
        dep_component="python3.11",
        fixed_dep_nvr=NEVR.from_string("python3.11-3.11.9-1.el10_2"),
        available_tools=[mock_tool],
    )

    assert already_fixed is True
    assert issue_key == "RHEL-123456"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_epoch_from_log():
    """Test that epoch from installed_pkgs.log is used for NEVR comparison."""
    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-123456",
            "fields": {
                "customfield_10578": "some-app-1.0.0-1.el10_2",
            },
        }
    ]

    # installed_pkgs.log WITH epoch 1 (same as Brew's epoch for the fixed build)
    installed_pkgs_log_content = b"""python-libs-1:3.11.9-1.el10_2.x86_64 1 2 abc installed
other-package-1.0-1.el10_2.x86_64 1 2 abc installed
"""

    candidate_build = {
        "name": "some-app",
        "build_id": 99904,
        "epoch": None,
        "version": "1.0.0",
        "release": "1.el10_2",
    }
    # Fixed build has epoch 1
    fixed_build = {
        "name": "python-libs",
        "epoch": 1,
        "version": "3.11.9",
        "release": "1.el10_2",
        "build_id": 123456,
    }
    # Mock run_tool to return fresh coroutines each call (called twice: closed + active)
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock Koji listRPMs for candidate build architecture lookup (buildID-aware)
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [{"arch": "x86_64", "name": "some-app", "nvr": "some-app-1.0.0-1.el10_2"}]
            if buildID == 99904
            else [{"arch": "x86_64", "name": "python-libs", "nvr": "python-libs-3.11.9-1.el10_2"}]
        )
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    # Mock _get_koji_build with argument-aware function
    def mock_get_build(url, nvr):
        if nvr == "some-app-1.0.0-1.el10_2":
            return candidate_build
        if nvr == "python-libs-3.11.9-1.el10_2":
            return fixed_build
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, issue_key, _nvr, _reason = await check_package_built_with_fixed_dependency(
        package="some-app",
        fix_version="rhel-10.2.z",
        dep_component="python-libs",
        fixed_dep_nvr=NEVR.from_string("python-libs-3.11.9-1.el10_2"),
        available_tools=[mock_tool],
    )

    # Should be True because epoch from log (1) matches Brew's epoch (1)
    assert already_fixed is True
    assert issue_key == "RHEL-123456"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_single_binary_with_debuginfo():
    """Test single-binary component with debuginfo/debugsource correctly resolves to already_fixed=True.

    A component like bash that produces bash, bash-debuginfo, bash-debugsource should be treated
    as having no subpackages (debuginfo/debugsource are filtered out by _get_known_package_names).
    """

    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-123456",
            "fields": {
                "customfield_10578": "build-tool-1.0.0-1.el10",
            },
        }
    ]

    # installed_pkgs.log with bash (the main binary)
    installed_pkgs_log_content = b"""bash-5.1.16-1.el10.x86_64 1 2 abc installed
other-package-1.0-1.el10.x86_64 1 2 abc installed
"""

    candidate_build = {
        "name": "build-tool",
        "build_id": 99905,
        "epoch": None,
        "version": "1.0.0",
        "release": "1.el10",
    }
    fixed_build = {
        "name": "bash",
        "epoch": None,
        "version": "5.1.16",
        "release": "1.el10",
        "build_id": 123456,
    }

    # Mock Jira search
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock Koji listRPMs (for architecture detection)
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [{"arch": "x86_64", "name": "build-tool", "nvr": "build-tool-1.0.0-1.el10"}]
            if buildID == 99905
            else [{"arch": "x86_64", "name": "bash", "nvr": "bash-5.1.16-1.el10"}]
        )
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    # Mock HTTP client for installed_pkgs.log fetch
    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()
    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    # Mock _get_koji_build
    def mock_get_build(url, nvr):
        if nvr == "build-tool-1.0.0-1.el10":
            return candidate_build
        if nvr == "bash-5.1.16-1.el10":
            return fixed_build
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    # Mock _get_known_package_names to return only "bash" (filtered list without debuginfo/debugsource)
    from specfile.utils import NEVR

    fixed_nevr = NEVR(name="bash", epoch=0, version="5.1.16", release="1.el10")
    flexmock(_ymir_utils).should_receive("_get_known_package_names").and_return(
        _coro((["bash"], 1, fixed_nevr, "bash"))
    )

    already_fixed, issue_key, nvr, reason = await check_package_built_with_fixed_dependency(
        package="build-tool",
        fix_version="rhel-10.0.z",
        dep_component="bash",
        fixed_dep_nvr=NEVR.from_string("bash-5.1.16-1.el10"),
        available_tools=[mock_tool],
    )

    assert already_fixed is True
    assert issue_key == "RHEL-123456"
    assert nvr == "build-tool-1.0.0-1.el10"
    assert reason is None


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_single_binary_different_name():
    """Test single-binary component whose binary name differs from source component.

    When a source component (e.g., python-foobar) produces only one binary package
    (e.g., python3-foobar) and that binary is installed, the dependency is confirmed
    fixed — there's no ambiguity about which binary carries the fix.
    """
    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-300001",
            "fields": {
                "customfield_10578": "my-app-2.0.0-1.el10",
            },
        }
    ]

    # installed_pkgs.log has python3-foobar (the only real binary from python-foobar)
    installed_pkgs_log_content = b"""python3-foobar-1.5.0-3.el10.x86_64 1 2 abc installed
other-package-1.0-1.el10.x86_64 1 2 abc installed
"""

    candidate_build = {
        "name": "my-app",
        "build_id": 99920,
        "epoch": None,
        "version": "2.0.0",
        "release": "1.el10",
    }
    fixed_build = {
        "name": "python-foobar",
        "epoch": None,
        "version": "1.5.0",
        "release": "3.el10",
        "build_id": 200001,
    }
    used_build = {
        "name": "python-foobar",
        "epoch": None,
        "version": "1.5.0",
        "release": "3.el10",
    }

    # Mock Jira search
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # listRPMs: package build returns arch info;
    # fixed dep returns only python3-foobar (single binary, name differs from source component)
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [{"arch": "x86_64", "name": "my-app", "nvr": "my-app-2.0.0-1.el10"}]
            if buildID == 99920
            else [
                {"arch": "x86_64", "name": "python3-foobar", "nvr": "python3-foobar-1.5.0-3.el10"},
            ]
        )
    )
    # getRPM for subpackage resolution (python3-foobar -> source RPM python-foobar)
    mock_koji_session.should_receive("getRPM").with_args(
        {"name": "python3-foobar", "version": "1.5.0", "release": "3.el10", "arch": "x86_64"}
    ).and_return({"name": "python3-foobar", "build_id": 200001, "epoch": None})
    mock_koji_session.should_receive("getBuild").with_args(200001).and_return(
        {"name": "python-foobar", "version": "1.5.0", "release": "3.el10", "epoch": None}
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    # Mock HTTP client for installed_pkgs.log fetch
    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()
    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    nvr_to_build = {
        "my-app-2.0.0-1.el10": candidate_build,
        "python-foobar-1.5.0-3.el10": fixed_build,
    }

    def mock_get_koji_build(url, nvr):
        return nvr_to_build.get(nvr, used_build)

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_koji_build)

    already_fixed, issue_key, nvr, reason = await check_package_built_with_fixed_dependency(
        package="my-app",
        fix_version="rhel-10.0.z",
        dep_component="python-foobar",
        fixed_dep_nvr=NEVR.from_string("python-foobar-1.5.0-3.el10"),
        available_tools=[mock_tool],
    )

    assert already_fixed is True
    assert issue_key == "RHEL-300001"
    assert nvr == "my-app-2.0.0-1.el10"
    assert reason is None


# Tests for individual helper functions


@pytest.mark.asyncio
async def test_find_completed_builds_jira_success():
    """Test _find_completed_builds_jira returns closed and active candidates."""
    from ymir.common.utils import _find_completed_builds_jira

    mock_tool = flexmock()
    closed_result = [
        {
            "key": "RHEL-123",
            "fields": {
                "customfield_10578": "pkg-1.0-1.el10",
            },
        },
        {
            "key": "RHEL-456",
            "fields": {
                "customfield_10578": "pkg-2.0-1.el10",
            },
        },
    ]
    active_result = [
        {
            "key": "RHEL-789",
            "fields": {
                "customfield_10578": "pkg-3.0-1.el10",
                "status": {"name": "In Progress"},
            },
        },
    ]

    # Mock both queries
    closed_jql = (
        'project = RHEL AND component = "pkg" AND '
        'fixVersion in ("rhel-10.2", "rhel-10.2.z") AND '
        "status in (Closed, Done) AND "
        'resolution in ("Done", "Done-Errata") AND '
        "customfield_10578 IS NOT EMPTY"
    )
    active_jql = (
        'project = RHEL AND component = "pkg" AND '
        'fixVersion in ("rhel-10.2", "rhel-10.2.z") AND '
        "status not in (Closed, Done) AND "
        "customfield_10578 IS NOT EMPTY"
    )

    async def mock_run_tool(tool, **kwargs):
        if kwargs.get("jql") == closed_jql:
            return closed_result
        if kwargs.get("jql") == active_jql:
            return active_result
        raise ValueError(f"Unexpected JQL: {kwargs.get('jql')}")

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(mock_run_tool)

    closed, active = await _find_completed_builds_jira("pkg", "rhel-10.2.z", [mock_tool])

    assert len(closed) == 2
    assert closed[0] == ("RHEL-123", "pkg-1.0-1.el10")
    assert closed[1] == ("RHEL-456", "pkg-2.0-1.el10")
    assert len(active) == 1
    assert active[0] == ("RHEL-789", "pkg-3.0-1.el10")


@pytest.mark.asyncio
async def test_find_completed_builds_jira_no_results():
    """Test _find_completed_builds_jira returns empty lists when no results."""
    from ymir.common.utils import _find_completed_builds_jira

    mock_tool = flexmock()

    async def mock_run_tool(tool, **kwargs):
        return []

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(mock_run_tool)

    closed, active = await _find_completed_builds_jira("pkg", "rhel-10.2.z", [mock_tool])

    assert closed == []
    assert active == []


@pytest.mark.asyncio
async def test_find_completed_builds_jira_reads_all_closed_pages():
    from ymir.common.utils import _find_completed_builds_jira

    queries = []

    async def search(_tool, **kwargs):
        assert kwargs["fetch_all"] is True
        queries.append(kwargs["jql"])
        if "status not in" in kwargs["jql"]:
            return []
        return [
            {
                "key": f"RHEL-{index}",
                "fields": {
                    "customfield_10578": f"pkg-1-{index}.el10",
                },
            }
            for index in range(51)
        ]

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(search)

    closed, active = await _find_completed_builds_jira("pkg", "rhel-10.2.z", [flexmock()])

    assert len(closed) == 51
    assert closed[-1] == ("RHEL-50", "pkg-1-50.el10")
    assert active == []
    assert len(queries) == 2


@pytest.mark.asyncio
async def test_find_completed_builds_jira_missing_nvr():
    """Test _find_completed_builds_jira filters out issues without NVR."""
    from ymir.common.utils import _find_completed_builds_jira

    mock_tool = flexmock()
    closed_result = [
        {
            "key": "RHEL-123",
            "fields": {
                "customfield_10578": "pkg-1.0-1.el10",
            },
        },
        {"key": "RHEL-456", "fields": {"customfield_10578": None}},  # Missing NVR
        {"key": "RHEL-789", "fields": {}},  # Missing field entirely
    ]

    flexmock(_ymir_utils).should_receive("run_tool").and_return(_coro(closed_result)).and_return(_coro([]))

    closed, active = await _find_completed_builds_jira("pkg", "rhel-10.2.z", [mock_tool])

    assert len(closed) == 1
    assert closed[0] == ("RHEL-123", "pkg-1.0-1.el10")
    assert active == []


@pytest.mark.asyncio
async def test_find_completed_builds_jira_whitespace_and_invalid_types():
    """Test _find_completed_builds_jira handles whitespace and invalid field types."""
    from ymir.common.utils import _find_completed_builds_jira

    mock_tool = flexmock()
    closed_result = [
        {
            "key": "RHEL-123",
            "fields": {
                "customfield_10578": "pkg-1.0-1.el10",
            },
        },  # Valid
        {
            "key": "RHEL-456",
            "fields": {
                "customfield_10578": "  pkg-2.0-1.el10  ",
            },
        },  # Whitespace
        {"key": "RHEL-789", "fields": {"customfield_10578": "   "}},  # Only whitespace
        {"key": "RHEL-999", "fields": {"customfield_10578": 12345}},  # Non-string (int)
        {"key": "RHEL-888", "fields": {"customfield_10578": ["pkg-3.0-1.el10"]}},  # Non-string (list)
    ]

    flexmock(_ymir_utils).should_receive("run_tool").and_return(_coro(closed_result)).and_return(_coro([]))

    closed, active = await _find_completed_builds_jira("pkg", "rhel-10.2.z", [mock_tool])

    # Should only get the valid ones, with whitespace stripped
    assert len(closed) == 2
    assert closed[0] == ("RHEL-123", "pkg-1.0-1.el10")
    assert closed[1] == ("RHEL-456", "pkg-2.0-1.el10")  # Whitespace stripped
    assert active == []


@pytest.mark.asyncio
async def test_find_completed_builds_jira_includes_both_statuses():
    """Test _find_completed_builds_jira queries for both closed and active builds."""
    from ymir.common.utils import _find_completed_builds_jira

    mock_tool = flexmock()
    captured_jqls = []

    def capture_jql(*args, **kwargs):
        jql = kwargs.get("jql")
        if jql:
            captured_jqls.append(jql)
        return _coro([])

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(capture_jql)

    await _find_completed_builds_jira("pkg", "rhel-10.2.z", [mock_tool])

    assert len(captured_jqls) == 2
    # First query should be for closed builds
    assert "status in (Closed, Done)" in captured_jqls[0]
    assert 'resolution in ("Done", "Done-Errata")' in captured_jqls[0]
    # Second query should be for active builds
    assert "status not in (Closed, Done)" in captured_jqls[1]


@pytest.mark.asyncio
async def test_find_completed_builds_jira_escapes_package_name():
    """Test _find_completed_builds_jira escapes special characters in package name."""
    from ymir.common.utils import _find_completed_builds_jira

    mock_tool = flexmock()
    captured_jqls = []

    def capture_jql(*args, **kwargs):
        jql = kwargs.get("jql")
        if jql:
            captured_jqls.append(jql)
        return _coro([])

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(capture_jql)

    # Package name with quote and backslash that could break JQL
    malicious_package = 'pkg" OR project = "OTHER'
    await _find_completed_builds_jira(malicious_package, "rhel-10.2.z", [mock_tool])

    # Both queries should escape the package name
    for jql in captured_jqls:
        # The quote should be escaped, preventing JQL injection
        assert 'component = "pkg\\" OR project = \\"OTHER"' in jql
        # Should NOT contain unescaped injection attempt
        assert 'component = "pkg" OR project = "OTHER"' not in jql


@pytest.mark.asyncio
async def test_find_completed_builds_jira_active_query_fails():
    """Test _find_completed_builds_jira returns None for active when query fails."""
    from ymir.common.utils import _find_completed_builds_jira

    mock_tool = flexmock()
    closed_result = [
        {
            "key": "RHEL-123",
            "fields": {
                "customfield_10578": "pkg-1.0-1.el10",
            },
        }
    ]

    call_count = {"count": 0}

    async def mock_run_tool(tool, **kwargs):
        call_count["count"] += 1
        if call_count["count"] == 1:
            # First call (closed query) succeeds
            return closed_result
        # Second call (active query) fails
        raise Exception("Jira connection timeout")

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(mock_run_tool)

    closed, active = await _find_completed_builds_jira("pkg", "rhel-10.2.z", [mock_tool])

    assert closed == [("RHEL-123", "pkg-1.0-1.el10")]
    assert active is None  # Query failed


@pytest.mark.asyncio
async def test_find_completed_builds_jira_active_query_invalid_response():
    """Test _find_completed_builds_jira returns None when active query returns non-list."""
    from ymir.common.utils import _find_completed_builds_jira

    mock_tool = flexmock()
    closed_result = [
        {
            "key": "RHEL-123",
            "fields": {
                "customfield_10578": "pkg-1.0-1.el10",
            },
        }
    ]

    call_count = {"count": 0}

    async def mock_run_tool(tool, **kwargs):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return closed_result
        return {"error": "invalid response"}  # Not a list

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(mock_run_tool)

    closed, active = await _find_completed_builds_jira("pkg", "rhel-10.2.z", [mock_tool])

    assert closed == [("RHEL-123", "pkg-1.0-1.el10")]
    assert active is None


@pytest.mark.asyncio
async def test_find_completed_builds_jira_both_queries_fail():
    """Test _find_completed_builds_jira raises when closed query returns invalid response."""
    from ymir.common.utils import TransientInfrastructureError, _find_completed_builds_jira

    mock_tool = flexmock()

    async def mock_run_tool(tool, **kwargs):
        return None  # Invalid response

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(mock_run_tool)

    with pytest.raises(TransientInfrastructureError, match="Invalid response from closed-build"):
        await _find_completed_builds_jira("pkg", "rhel-10.2.z", [mock_tool])


@pytest.mark.asyncio
async def test_select_highest_evr_build_success():
    """Test _select_highest_evr_build selects highest EVR."""
    from ymir.common.utils import _select_highest_evr_build

    candidates = [
        ("RHEL-123", "pkg-1.0-1.el10"),
        ("RHEL-456", "pkg-2.0-1.el10"),
        ("RHEL-789", "pkg-1.5-2.el10"),
    ]

    # Use argument-aware mock to avoid nondeterministic ordering
    nvr_to_build = {
        "pkg-1.0-1.el10": {"name": "pkg", "epoch": None, "version": "1.0", "release": "1.el10"},
        "pkg-2.0-1.el10": {"name": "pkg", "epoch": None, "version": "2.0", "release": "1.el10"},
        "pkg-1.5-2.el10": {"name": "pkg", "epoch": None, "version": "1.5", "release": "2.el10"},
    }

    def mock_get_build(url, nvr):
        return nvr_to_build.get(nvr)

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    result = await _select_highest_evr_build(candidates, "pkg")

    assert result is not None
    nevr, issue_key = result
    assert nevr.name == "pkg"
    assert nevr.version == "2.0"
    assert nevr.release == "1.el10"
    assert issue_key == "RHEL-456"


@pytest.mark.asyncio
async def test_select_highest_evr_build_empty():
    """Test _select_highest_evr_build returns None for empty list."""
    from ymir.common.utils import _select_highest_evr_build

    result = await _select_highest_evr_build([], "pkg")
    assert result is None


@pytest.mark.asyncio
async def test_select_highest_evr_build_with_epoch():
    """Test _select_highest_evr_build respects epoch."""
    from ymir.common.utils import _select_highest_evr_build

    candidates = [
        ("RHEL-123", "pkg-9.0-1.el10"),  # No epoch, higher version
        ("RHEL-456", "pkg-1.0-1.el10"),  # Epoch 2, lower version but should win
    ]

    nvr_to_build = {
        "pkg-9.0-1.el10": {"name": "pkg", "epoch": None, "version": "9.0", "release": "1.el10"},
        "pkg-1.0-1.el10": {"name": "pkg", "epoch": 2, "version": "1.0", "release": "1.el10"},
    }

    def mock_get_build(url, nvr):
        return nvr_to_build.get(nvr)

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    result = await _select_highest_evr_build(candidates, "pkg")

    nevr, issue_key = result
    assert nevr.name == "pkg"
    assert nevr.epoch == 2
    assert nevr.version == "1.0"
    assert nevr.release == "1.el10"
    assert issue_key == "RHEL-456"


@pytest.mark.asyncio
async def test_select_highest_evr_build_koji_failures():
    """Test _select_highest_evr_build raises when any Brew lookup fails."""
    from ymir.common.utils import TransientInfrastructureError, _select_highest_evr_build

    candidates = [
        ("RHEL-123", "pkg-1.0-1.el10"),
        ("RHEL-456", "pkg-2.0-1.el10"),
    ]

    nvr_to_build = {
        "pkg-1.0-1.el10": {"name": "pkg", "epoch": None, "version": "1.0", "release": "1.el10"},
        "pkg-2.0-1.el10": Exception("Koji error"),
    }

    def mock_get_build(url, nvr):
        result = nvr_to_build.get(nvr)
        if isinstance(result, Exception):
            raise result
        return result

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    with pytest.raises(TransientInfrastructureError, match="Brew lookup failed for 1 candidate"):
        await _select_highest_evr_build(candidates, "pkg")


@pytest.mark.asyncio
async def test_select_highest_evr_build_wrong_package():
    """Test _select_highest_evr_build rejects builds for wrong package."""
    from ymir.common.utils import _select_highest_evr_build

    candidates = [
        ("RHEL-123", "pkg-1.0-1.el10"),
        ("RHEL-456", "wrong-pkg-2.0-1.el10"),  # Wrong package name in NVR
    ]

    nvr_to_build = {
        "pkg-1.0-1.el10": {"name": "pkg", "epoch": None, "version": "1.0", "release": "1.el10"},
        "wrong-pkg-2.0-1.el10": {"name": "wrong-pkg", "epoch": None, "version": "2.0", "release": "1.el10"},
    }

    def mock_get_build(url, nvr):
        return nvr_to_build.get(nvr)

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    result = await _select_highest_evr_build(candidates, "pkg")

    # Should only return the correct package build, ignoring the wrong-pkg build
    nevr, issue_key = result
    assert nevr.name == "pkg"
    assert nevr.version == "1.0"
    assert nevr.release == "1.el10"
    assert issue_key == "RHEL-123"


@pytest.mark.asyncio
async def test_fetch_installed_pkgs_log_success():
    """Test _fetch_installed_pkgs_log returns list of architecture logs."""
    from specfile.utils import NEVR

    from ymir.common.utils import _fetch_installed_pkgs_log

    installed_pkgs_log_content = b"golang-1.22.7-1.el10.x86_64 1 2 abc installed"

    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    built_archs = {"x86_64", "aarch64"}
    package_nevr = NEVR(name="golang", epoch=None, version="1.22.7", release="1.el10")
    logs = await _fetch_installed_pkgs_log(package_nevr, built_archs)

    assert len(logs) == 2
    assert {url for url, _ in logs} == {
        "https://brewweb.engineering.redhat.com/brew/packages/"
        f"golang/1.22.7/1.el10/data/logs/{arch}/installed_pkgs.log"
        for arch in built_archs
    }
    assert any("golang-1.22.7-1.el10.x86_64" in content for _, content in logs)


@pytest.mark.asyncio
async def test_fetch_installed_pkgs_log_gzipped():
    """Test _fetch_installed_pkgs_log handles gzipped logs."""
    import gzip

    from specfile.utils import NEVR

    from ymir.common.utils import _fetch_installed_pkgs_log

    original_content = b"golang-1.22.7-1.el10.x86_64 1 2 abc installed"
    gzipped_content = gzip.compress(original_content)

    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=gzipped_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    built_archs = {"x86_64"}
    package_nevr = NEVR(name="golang", epoch=None, version="1.22.7", release="1.el10")
    logs = await _fetch_installed_pkgs_log(package_nevr, built_archs)

    assert len(logs) == 1
    assert any("golang-1.22.7-1.el10.x86_64" in content for _, content in logs)


@pytest.mark.asyncio
async def test_fetch_installed_pkgs_log_invalid_encoding_retries():
    from specfile.utils import NEVR

    from ymir.common.utils import TransientInfrastructureError, _fetch_installed_pkgs_log

    client = flexmock()
    client.should_receive("head").and_return(_coro(flexmock(status_code=200)))
    client.should_receive("get").and_return(_coro(flexmock(status_code=200, content=b"\xff")))
    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(client))

    package_nevr = NEVR(name="pkg", epoch=None, version="1.0", release="1.el10")
    with pytest.raises(TransientInfrastructureError, match="Infrastructure error"):
        await _fetch_installed_pkgs_log(package_nevr, {"x86_64"})


@pytest.mark.asyncio
async def test_fetch_installed_pkgs_log_invalid_nvr():
    """Test _fetch_installed_pkgs_log raises exception for NVR with no available logs."""
    from specfile.utils import NEVR

    from ymir.common.utils import TransientInfrastructureError, _fetch_installed_pkgs_log

    mock_head_response = flexmock(status_code=404)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    built_archs = {"x86_64"}
    package_nevr = NEVR(name="invalid", epoch=None, version="n", release="vr")
    with pytest.raises(TransientInfrastructureError, match=r"No installed_pkgs\.log available"):
        await _fetch_installed_pkgs_log(package_nevr, built_archs)


@pytest.mark.asyncio
async def test_fetch_installed_pkgs_log_not_found():
    """Test _fetch_installed_pkgs_log raises exception when logs not found."""
    from specfile.utils import NEVR

    from ymir.common.utils import TransientInfrastructureError, _fetch_installed_pkgs_log

    mock_head_response = flexmock(status_code=404)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    built_archs = {"x86_64"}
    package_nevr = NEVR(name="pkg", epoch=None, version="1.0", release="1.el10")
    with pytest.raises(TransientInfrastructureError, match=r"No installed_pkgs\.log available"):
        await _fetch_installed_pkgs_log(package_nevr, built_archs)


@pytest.mark.asyncio
async def test_fetch_installed_pkgs_log_server_error():
    """Test _fetch_installed_pkgs_log raises TransientInfrastructureError on 5xx."""
    from specfile.utils import NEVR

    from ymir.common.utils import TransientInfrastructureError, _fetch_installed_pkgs_log

    mock_head_response = flexmock(status_code=503)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    built_archs = {"x86_64"}
    package_nevr = NEVR(name="pkg", epoch=None, version="1.0", release="1.el10")
    with pytest.raises(TransientInfrastructureError, match="Infrastructure error"):
        await _fetch_installed_pkgs_log(package_nevr, built_archs)


@pytest.mark.asyncio
async def test_get_known_package_names_success():
    """Test _get_known_package_names returns binary RPM names and validated NEVR."""
    from ymir.common.utils import _get_known_package_names

    fixed_build = {
        "name": "golang",
        "build_id": 123456,
        "epoch": None,
        "version": "1.22.7",
        "release": "1.el10",
    }

    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").with_args(buildID=123456).and_return(
        [{"name": "golang"}, {"name": "golang-bin"}, {"name": "golang-devel"}]
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(fixed_build)

    result = await _get_known_package_names(NEVR.from_string("golang-1.22.7-1.el10"))

    assert result is not None
    names, real_binary_count, fixed_dep_nevr, build_name = result
    assert "golang" in names
    assert "golang-bin" in names
    assert "golang-devel" in names
    assert real_binary_count == 3
    assert fixed_dep_nevr.name == "golang"
    assert fixed_dep_nevr.version == "1.22.7"
    assert fixed_dep_nevr.release == "1.el10"
    assert fixed_dep_nevr.epoch == 0
    assert build_name == "golang"


@pytest.mark.asyncio
async def test_get_known_package_names_component_not_in_list():
    """Test _get_known_package_names adds component name if missing."""
    from ymir.common.utils import _get_known_package_names

    fixed_build = {
        "name": "golang",
        "build_id": 123456,
        "epoch": None,
        "version": "1.22.7",
        "release": "1.el10",
    }

    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").with_args(buildID=123456).and_return(
        [{"name": "golang-bin"}, {"name": "golang-devel"}]  # Missing "golang" itself
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(fixed_build)

    result = await _get_known_package_names(NEVR.from_string("golang-1.22.7-1.el10"))

    assert result is not None
    names, real_binary_count, fixed_dep_nevr, build_name = result
    assert names[0] == "golang"  # Should be first (synthetically inserted)
    assert "golang-bin" in names
    assert "golang-devel" in names
    assert real_binary_count == 2  # Only 2 real binaries (golang-bin, golang-devel)
    assert fixed_dep_nevr.name == "golang"
    assert build_name == "golang"


@pytest.mark.asyncio
async def test_get_known_package_names_koji_failure():
    """Test _get_known_package_names returns None when build not found."""
    from ymir.common.utils import _get_known_package_names

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(None)

    result = await _get_known_package_names(NEVR.from_string("golang-1.22.7-1.el10"))

    assert result is None


@pytest.mark.asyncio
async def test_get_known_package_names_name_mismatch():
    """Test _get_known_package_names returns actual build_name for caller to validate."""
    from ymir.common.utils import _get_known_package_names

    wrong_build = {
        "name": "python",
        "build_id": 123,
        "epoch": None,
        "version": "1.22.7",
        "release": "1.el10",
    }
    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(wrong_build)

    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").with_args(buildID=123).and_return(
        [{"name": "python-libs"}, {"name": "python-devel"}]
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    result = await _get_known_package_names(NEVR.from_string("golang-1.22.7-1.el10"))

    assert result is not None
    _names, _real_binary_count, _fixed_dep_nevr, build_name = result
    assert build_name == "python"


@pytest.mark.asyncio
async def test_get_known_package_names_missing_build_id():
    """Test _get_known_package_names returns None when build has no build_id."""
    from ymir.common.utils import _get_known_package_names

    build_no_id = {"name": "golang"}
    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(build_no_id)

    result = await _get_known_package_names(NEVR.from_string("golang-1.22.7-1.el10"))

    assert result is None


@pytest.mark.asyncio
async def test_get_known_package_names_list_rpms_exception():
    """Test _get_known_package_names returns None when listRPMs raises exception."""
    from ymir.common.utils import _get_known_package_names

    valid_build = {
        "name": "golang",
        "build_id": 123456,
        "epoch": None,
        "version": "1.22.7",
        "release": "1.el10",
    }
    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(valid_build)

    mock_session = flexmock()
    mock_session.should_receive("listRPMs").and_raise(Exception("Koji unavailable"))
    flexmock(koji).should_receive("ClientSession").and_return(mock_session)

    result = await _get_known_package_names(NEVR.from_string("golang-1.22.7-1.el10"))

    assert result is None


@pytest.mark.asyncio
async def test_get_known_package_names_invalid_rpms_response():
    """Test _get_known_package_names returns None when listRPMs returns non-list."""
    from ymir.common.utils import _get_known_package_names

    valid_build = {
        "name": "golang",
        "build_id": 123456,
        "epoch": None,
        "version": "1.22.7",
        "release": "1.el10",
    }
    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(valid_build)

    mock_session = flexmock()
    mock_session.should_receive("listRPMs").and_return("not a list")
    flexmock(koji).should_receive("ClientSession").and_return(mock_session)

    result = await _get_known_package_names(NEVR.from_string("golang-1.22.7-1.el10"))

    assert result is None


@pytest.mark.asyncio
async def test_get_known_package_names_excludes_src_from_count():
    """Test _get_known_package_names includes src RPM in search but excludes from count."""
    from ymir.common.utils import _get_known_package_names

    fixed_build = {
        "name": "golang",
        "build_id": 123456,
        "epoch": None,
        "version": "1.22.7",
        "release": "1.el10",
    }

    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").with_args(buildID=123456).and_return(
        [
            {"name": "golang", "arch": "src"},  # Source RPM
            {"name": "golang", "arch": "x86_64"},
            {"name": "golang-bin", "arch": "x86_64"},
            {"name": "golang-devel", "arch": "x86_64"},
        ]
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(fixed_build)

    result = await _get_known_package_names(NEVR.from_string("golang-1.22.7-1.el10"))

    assert result is not None
    names, real_binary_count, fixed_dep_nevr, build_name = result
    # All non-debug packages should be in search list (including src)
    assert "golang" in names
    assert "golang-bin" in names
    assert "golang-devel" in names
    # But only installable binaries should be counted (exclude src)
    assert real_binary_count == 3  # golang, golang-bin, golang-devel (not the src)
    assert fixed_dep_nevr.name == "golang"
    assert build_name == "golang"


@pytest.mark.asyncio
async def test_parse_dependency_from_installed_pkgs_log_success():
    """Test _parse_dependency_from_installed_pkgs_log finds dependency."""
    from ymir.common.utils import _parse_dependency_from_installed_pkgs_log

    installed_pkgs_log = """golang-1.22.7-1.el10.x86_64 1 2 abc installed
other-package-1.0-1.el10.x86_64 1 2 abc installed
"""

    nevr, matched = await _parse_dependency_from_installed_pkgs_log(installed_pkgs_log, "golang", ["golang"])

    assert nevr is not None
    assert nevr.name == "golang"
    assert nevr.version == "1.22.7"
    assert nevr.release == "1.el10"
    assert nevr.epoch == 0
    assert matched == ["golang"]


@pytest.mark.asyncio
async def test_parse_dependency_from_installed_pkgs_log_with_epoch():
    """Test _parse_dependency_from_installed_pkgs_log parses epoch."""
    from ymir.common.utils import _parse_dependency_from_installed_pkgs_log

    installed_pkgs_log = "vim-2:9.0.1-1.el10.x86_64 1 2 abc installed\n"

    nevr, matched = await _parse_dependency_from_installed_pkgs_log(installed_pkgs_log, "vim", ["vim"])

    assert nevr is not None
    assert nevr.name == "vim"
    assert nevr.version == "9.0.1"
    assert nevr.release == "1.el10"
    assert nevr.epoch == 2
    assert matched == ["vim"]


@pytest.mark.asyncio
async def test_parse_dependency_from_installed_pkgs_log_subpackage():
    """Test _parse_dependency_from_installed_pkgs_log finds subpackage."""
    from ymir.common.utils import _parse_dependency_from_installed_pkgs_log

    installed_pkgs_log = "golang-bin-1.22.7-1.el10.x86_64 1 2 abc installed\n"

    nevr, matched = await _parse_dependency_from_installed_pkgs_log(
        installed_pkgs_log, "golang", ["golang", "golang-bin"]
    )

    assert nevr is not None
    assert nevr.name == "golang"
    assert nevr.version == "1.22.7"
    assert nevr.release == "1.el10"
    assert nevr.epoch == 0
    assert matched == ["golang-bin"]


@pytest.mark.asyncio
async def test_parse_dependency_from_installed_pkgs_log_nonnumeric_version():
    """Test _parse_dependency_from_installed_pkgs_log handles versions starting with non-digit."""
    from ymir.common.utils import _parse_dependency_from_installed_pkgs_log

    # Version starts with 'v' (valid in RPM)
    installed_pkgs_log = "myapp-v1.2.3-1.el10.x86_64 1 2 abc installed\n"

    nevr, matched = await _parse_dependency_from_installed_pkgs_log(installed_pkgs_log, "myapp", ["myapp"])

    assert nevr is not None
    assert nevr.name == "myapp"
    assert nevr.version == "v1.2.3"
    assert nevr.release == "1.el10"
    assert nevr.epoch == 0
    assert matched == ["myapp"]


@pytest.mark.asyncio
async def test_parse_dependency_from_installed_pkgs_log_dotted_name():
    """Test _parse_dependency_from_installed_pkgs_log handles package names with dots."""
    from ymir.common.utils import _parse_dependency_from_installed_pkgs_log

    installed_pkgs_log = "python3.11-3.11.9-1.el10.x86_64 1 2 abc installed\n"

    nevr, matched = await _parse_dependency_from_installed_pkgs_log(
        installed_pkgs_log, "python3.11", ["python3.11"]
    )

    assert nevr is not None
    assert nevr.name == "python3.11"
    assert nevr.version == "3.11.9"
    assert nevr.release == "1.el10"
    assert nevr.epoch == 0
    assert matched == ["python3.11"]


@pytest.mark.asyncio
async def test_parse_dependency_from_installed_pkgs_log_not_found():
    """Test _parse_dependency_from_installed_pkgs_log returns None when not found."""
    from ymir.common.utils import _parse_dependency_from_installed_pkgs_log

    installed_pkgs_log = "other-package-1.0-1.el10.x86_64 1 2 abc installed\n"

    nevr, matched = await _parse_dependency_from_installed_pkgs_log(installed_pkgs_log, "golang", ["golang"])

    assert nevr is None
    assert matched is None


@pytest.mark.asyncio
async def test_installed_pkgs_sample_epoch_and_subpackage():
    """Brew's installed_pkgs.log puts the epoch after the RPM name."""
    from specfile.utils import NEVR

    from ymir.common.utils import _parse_dependency_from_installed_pkgs_log

    log = (
        "gmp-1:6.1.2-10.el8.ppc64le 1560501960 1543484 "
        "7466cd0929fb6ee3cbf6d8b8dfbfefef installed\n"
        "libjpeg-turbo-devel-1.5.3-12.el8.ppc64le 1626337712 341202 "
        "12deaf38928bd089ec1826234c54c2b4 installed\n"
    )

    nevr, matched = await _parse_dependency_from_installed_pkgs_log(log, "gmp", ["gmp"])
    assert nevr == NEVR(name="gmp", epoch=1, version="6.1.2", release="10.el8")
    assert matched == ["gmp"]

    nevr2, matched2 = await _parse_dependency_from_installed_pkgs_log(
        log, "libjpeg-turbo", ["libjpeg-turbo", "libjpeg-turbo-devel"]
    )
    assert nevr2 == NEVR(name="libjpeg-turbo", epoch=0, version="1.5.3", release="12.el8")
    assert matched2 == ["libjpeg-turbo-devel"]


@pytest.mark.asyncio
async def test_resolve_binary_rpm_to_source_build_success():
    """Test _resolve_binary_rpm_to_source_build uses getRPM to resolve binary to source."""
    from ymir.common.utils import _resolve_binary_rpm_to_source_build

    mock_session = flexmock()
    mock_session.should_receive("getRPM").with_args(
        {"name": "perl-Errno", "version": "1.37", "release": "2.el10", "arch": "x86_64"}
    ).and_return({"name": "perl-Errno", "build_id": 555, "epoch": None})
    mock_session.should_receive("getBuild").with_args(555).and_return(
        {"name": "perl", "version": "5.40.0", "release": "2.el10", "epoch": 4}
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_session)

    result = await _resolve_binary_rpm_to_source_build(
        "https://brewhub.example.com", "perl-Errno-1.37-2.el10.x86_64", "perl-Errno", "perl"
    )

    assert result is not None
    assert result.name == "perl"
    assert result.epoch == 4
    assert result.version == "5.40.0"
    assert result.release == "2.el10"


@pytest.mark.asyncio
async def test_resolve_binary_rpm_to_source_build_not_found():
    """Test _resolve_binary_rpm_to_source_build returns None when RPM not in Koji."""
    from ymir.common.utils import _resolve_binary_rpm_to_source_build

    mock_session = flexmock()
    mock_session.should_receive("getRPM").and_return(None)
    flexmock(koji).should_receive("ClientSession").and_return(mock_session)

    result = await _resolve_binary_rpm_to_source_build(
        "https://brewhub.example.com", "missing-pkg-1.0-1.el10.x86_64", "missing-pkg", "missing"
    )

    assert result is None


@pytest.mark.asyncio
async def test_parse_dependency_subpackage_with_brew_resolution():
    """Test subpackage resolution via Brew getRPM when pkg_name != dep_component."""
    from ymir.common.utils import _parse_dependency_from_installed_pkgs_log

    log = "openssl-libs-1:3.2.2-6.el10.x86_64 1 2 abc installed\n"

    mock_session = flexmock()
    mock_session.should_receive("getRPM").with_args(
        {"name": "openssl-libs", "version": "3.2.2", "release": "6.el10", "arch": "x86_64"}
    ).and_return({"name": "openssl-libs", "build_id": 777, "epoch": 1})
    mock_session.should_receive("getBuild").with_args(777).and_return(
        {"name": "openssl", "version": "3.2.2", "release": "6.el10", "epoch": 1}
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_session)

    nevr, matched = await _parse_dependency_from_installed_pkgs_log(
        log, "openssl", ["openssl", "openssl-libs"], brew_url="https://brewhub.example.com"
    )

    assert nevr is not None
    assert nevr.name == "openssl"
    assert nevr.version == "3.2.2"
    assert nevr.release == "6.el10"
    assert nevr.epoch == 1
    assert matched == ["openssl-libs"]


@pytest.mark.asyncio
async def test_installed_pkgs_parser_ignores_noninstalled_and_similar_names():
    from ymir.common.utils import _parse_dependency_from_installed_pkgs_log

    log = "golang-extra-1.0-1.el10.x86_64 1 2 abc installed\ngolang-1.0-1.el10.x86_64 1 2 abc erased\n"
    assert await _parse_dependency_from_installed_pkgs_log(log, "golang", ["golang"]) == (None, None)


@pytest.mark.asyncio
async def test_parse_dependency_collects_all_matching_subpackages():
    """Test _parse_dependency_from_installed_pkgs_log collects all matching packages, not just the first."""
    from ymir.common.utils import _parse_dependency_from_installed_pkgs_log

    # Simulate a build that installed multiple binary RPMs from the same source
    log = (
        "golang-1.22.7-1.el10.x86_64 1 2 abc installed\n"
        "golang-bin-1.22.7-1.el10.x86_64 1 2 abc installed\n"
        "golang-src-1.22.7-1.el10.noarch 1 2 abc installed\n"
        "unrelated-pkg-1.0-1.el10.x86_64 1 2 abc installed\n"
    )

    nevr, matched = await _parse_dependency_from_installed_pkgs_log(
        log, "golang", ["golang", "golang-bin", "golang-src"]
    )

    assert nevr is not None
    assert nevr.name == "golang"
    assert nevr.version == "1.22.7"
    assert nevr.release == "1.el10"
    assert matched == ["golang", "golang-bin", "golang-src"]


@pytest.mark.asyncio
async def test_compare_build_timestamps_package_built_after_fix():
    """Test _compare_build_timestamps returns True when package built after fix."""
    from datetime import datetime

    from ymir.common.utils import _compare_build_timestamps

    package_build = {
        "name": "git-lfs",
        "completion_time": datetime(2026, 8, 25, 10, 0, 0),
    }
    fixed_build = {
        "name": "golang",
        "completion_time": datetime(2026, 8, 24, 15, 0, 0),
    }

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(package_build).and_return(fixed_build)

    result = await _compare_build_timestamps(
        "git-lfs", "git-lfs-3.4.1-13.el8_10", "golang", NEVR.from_string("golang-1.26.7-1.el10")
    )

    assert result is True


@pytest.mark.asyncio
async def test_compare_build_timestamps_package_built_before_fix():
    """Test _compare_build_timestamps returns False when package built before fix."""
    from datetime import datetime

    from ymir.common.utils import _compare_build_timestamps

    package_build = {
        "name": "git-lfs",
        "completion_time": datetime(2026, 7, 9, 13, 0, 0),
    }
    fixed_build = {
        "name": "golang",
        "completion_time": datetime(2026, 8, 24, 15, 0, 0),
    }

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(package_build).and_return(fixed_build)

    result = await _compare_build_timestamps(
        "git-lfs", "git-lfs-3.4.1-12.el8_10", "golang", NEVR.from_string("golang-1.26.7-1.el10")
    )

    assert result is False


@pytest.mark.asyncio
async def test_compare_build_timestamps_same_time():
    """Test _compare_build_timestamps returns True when built at same time."""
    from datetime import datetime

    from ymir.common.utils import _compare_build_timestamps

    same_time = datetime(2026, 8, 24, 15, 0, 0)
    package_build = {"name": "git-lfs", "completion_time": same_time}
    fixed_build = {"name": "golang", "completion_time": same_time}

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(package_build).and_return(fixed_build)

    result = await _compare_build_timestamps(
        "git-lfs", "git-lfs-3.4.1-12.el8_10", "golang", NEVR.from_string("golang-1.26.7-1.el10")
    )

    assert result is True


@pytest.mark.asyncio
async def test_compare_build_timestamps_missing_package_build():
    """Test _compare_build_timestamps returns None when package build not found."""
    from datetime import datetime

    from ymir.common.utils import _compare_build_timestamps

    fixed_build = {"name": "golang", "completion_time": datetime(2026, 8, 24, 15, 0, 0)}

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(None).and_return(fixed_build)

    result = await _compare_build_timestamps(
        "git-lfs", "git-lfs-3.4.1-12.el8_10", "golang", NEVR.from_string("golang-1.26.7-1.el10")
    )

    assert result is None


@pytest.mark.asyncio
async def test_compare_build_timestamps_missing_fixed_build():
    """Test _compare_build_timestamps returns False when fixed build not found."""
    from datetime import datetime

    from ymir.common.utils import _compare_build_timestamps

    package_build = {"name": "git-lfs", "completion_time": datetime(2026, 7, 9, 13, 0, 0)}

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(package_build).and_return(None)

    result = await _compare_build_timestamps(
        "git-lfs", "git-lfs-3.4.1-12.el8_10", "golang", NEVR.from_string("golang-1.26.7-1.el10")
    )

    assert result is None


@pytest.mark.asyncio
async def test_compare_build_timestamps_missing_completion_time():
    """Test _compare_build_timestamps returns False when completion_time missing."""
    from ymir.common.utils import _compare_build_timestamps

    package_build = {"name": "git-lfs"}  # No completion_time
    fixed_build = {"name": "golang"}  # No completion_time

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(package_build).and_return(fixed_build)

    result = await _compare_build_timestamps(
        "git-lfs", "git-lfs-3.4.1-12.el8_10", "golang", NEVR.from_string("golang-1.26.7-1.el10")
    )

    assert result is None


@pytest.mark.asyncio
async def test_compare_build_timestamps_wrong_package_name():
    """Test _compare_build_timestamps validates package name."""
    from datetime import datetime

    from ymir.common.utils import _compare_build_timestamps

    package_build = {
        "name": "wrong-package",  # Wrong name
        "completion_time": datetime(2026, 8, 25, 10, 0, 0),
    }
    fixed_build = {"name": "golang", "completion_time": datetime(2026, 8, 24, 15, 0, 0)}

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(package_build).and_return(fixed_build)

    result = await _compare_build_timestamps(
        "git-lfs", "git-lfs-3.4.1-12.el8_10", "golang", NEVR.from_string("golang-1.26.7-1.el10")
    )

    assert result is None


@pytest.mark.asyncio
async def test_compare_build_timestamps_wrong_dependency_name():
    """Test _compare_build_timestamps validates dependency name."""
    from datetime import datetime

    from ymir.common.utils import _compare_build_timestamps

    package_build = {"name": "git-lfs", "completion_time": datetime(2026, 8, 25, 10, 0, 0)}
    fixed_build = {
        "name": "wrong-dep",  # Wrong name
        "completion_time": datetime(2026, 8, 24, 15, 0, 0),
    }

    flexmock(_ymir_utils).should_receive("_get_koji_build").and_return(package_build).and_return(fixed_build)

    result = await _compare_build_timestamps(
        "git-lfs", "git-lfs-3.4.1-12.el8_10", "golang", NEVR.from_string("golang-1.26.7-1.el10")
    )

    assert result is None


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_all_koji_lookups_fail():
    """Test that TransientInfrastructureError is raised when all candidate Brew lookups fail."""
    from ymir.common.utils import TransientInfrastructureError

    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-111111",
            "fields": {"customfield_10578": "pkg-1.0-1.el10"},
        },
        {
            "key": "RHEL-222222",
            "fields": {"customfield_10578": "pkg-2.0-1.el10"},
        },
    ]

    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # All Brew lookups fail
    flexmock(_ymir_utils).should_receive("_get_koji_build").and_raise(
        ConnectionError("Koji connection refused")
    )

    with pytest.raises(TransientInfrastructureError, match="Brew lookup failed for 2 candidate"):
        await check_package_built_with_fixed_dependency(
            package="pkg",
            fix_version="rhel-10.0.z",
            dep_component="dep",
            fixed_dep_nvr=NEVR.from_string("dep-1.0-1.el10"),
            available_tools=[mock_tool],
        )


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_partial_koji_lookup_fail():
    """Test that TransientInfrastructureError is raised when any candidate Brew lookup fails."""
    from ymir.common.utils import TransientInfrastructureError

    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-111111",
            "fields": {"customfield_10578": "pkg-1.0-1.el10"},
        },
        {
            "key": "RHEL-222222",
            "fields": {"customfield_10578": "pkg-2.0-1.el10"},
        },
    ]

    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Newest candidate fails, older one succeeds
    def mock_get_build(url, nvr):
        if nvr == "pkg-2.0-1.el10":
            raise ConnectionError("Koji timeout")
        if nvr == "pkg-1.0-1.el10":
            return {"name": "pkg", "epoch": None, "version": "1.0", "release": "1.el10"}
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    with pytest.raises(TransientInfrastructureError, match="Brew lookup failed for 1 candidate"):
        await check_package_built_with_fixed_dependency(
            package="pkg",
            fix_version="rhel-10.0.z",
            dep_component="dep",
            fixed_dep_nvr=NEVR.from_string("dep-1.0-1.el10"),
            available_tools=[mock_tool],
        )


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_invalid_build_records():
    """Test that NVRs resolving to wrong package name in Koji return invalid_build_records."""
    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-999999",
            "fields": {"customfield_10578": "wrong-pkg-1.0-1.el10"},
        },
    ]

    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # NVR resolves to a different package name than expected
    def mock_get_build(url, nvr):
        if nvr == "wrong-pkg-1.0-1.el10":
            return {
                "name": "wrong-pkg",
                "epoch": None,
                "version": "1.0",
                "release": "1.el10",
            }
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, issue_key, nvr, reason = await check_package_built_with_fixed_dependency(
        package="my-pkg",
        fix_version="rhel-10.0.z",
        dep_component="dep",
        fixed_dep_nvr=NEVR.from_string("dep-1.0-1.el10"),
        available_tools=[mock_tool],
    )

    assert already_fixed is None
    assert issue_key is None
    assert nvr is None
    assert reason == "invalid_build_records:RHEL-999999"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_subpackage_list_unavailable():
    """Test when subpackage list fetch fails due to Koji error - should raise exception."""
    from ymir.common.utils import TransientInfrastructureError

    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-888888",
            "fields": {
                "customfield_10578": "some-app-5.0-1.el10",
            },
        }
    ]

    candidate_build = {
        "name": "some-app",
        "build_id": 99906,
        "epoch": None,
        "version": "5.0",
        "release": "1.el10",
    }
    fixed_build = {
        "name": "golang",
        "epoch": None,
        "version": "1.22.7",
        "release": "1.el10",
        "build_id": 123456,
    }

    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock Koji to fail on listRPMs (architecture lookup)
    mock_session = flexmock()
    mock_session.should_receive("listRPMs").and_raise(koji.GenericError("Koji connection timeout"))
    flexmock(koji).should_receive("ClientSession").and_return(mock_session)

    def mock_get_build(url, nvr):
        if nvr == "some-app-5.0-1.el10":
            return candidate_build
        if nvr == "golang-1.22.7-1.el10":
            return fixed_build
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    # Should raise TransientInfrastructureError when architecture list unavailable
    with pytest.raises(TransientInfrastructureError, match="Failed to fetch architecture list"):
        await check_package_built_with_fixed_dependency(
            package="some-app",
            fix_version="rhel-10.z",
            dep_component="golang",
            fixed_dep_nvr=NEVR.from_string("golang-1.22.7-1.el10"),
            available_tools=[mock_tool],
        )


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_epoch_normalization_multiarch():
    """Test that explicit epoch 0 and omitted epoch are treated as equivalent across architectures."""
    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-999999",
            "fields": {
                "customfield_10578": "some-package-2.0-1.el10",
            },
        }
    ]

    # x86_64 log shows dependency WITHOUT epoch prefix
    x86_64_log = b"""golang-1.22.7-1.el10.x86_64 1 2 abc installed
other-package-1.0-1.el10.x86_64 1 2 abc installed
"""

    # aarch64 log shows dependency WITH explicit epoch 0
    aarch64_log = b"""golang-0:1.22.7-1.el10.aarch64 1 2 abc installed
other-package-1.0-1.el10.aarch64 1 2 abc installed
"""

    candidate_build = {
        "name": "some-package",
        "build_id": 99907,
        "epoch": None,
        "version": "2.0",
        "release": "1.el10",
    }
    golang_build = {
        "name": "golang",
        "epoch": 0,  # Koji says epoch is 0
        "version": "1.22.7",
        "release": "1.el10",
        "build_id": 123456,
    }
    fixed_build = {
        "name": "golang",
        "epoch": None,
        "version": "1.22.5",
        "release": "1.el10",
        "build_id": 123456,
    }

    # Mock run_tool to return fresh coroutines each call
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock Koji listRPMs for candidate build architecture lookup (buildID-aware)
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [
                {"arch": "x86_64", "name": "some-package", "nvr": "some-package-2.0-1.el10"},
                {"arch": "aarch64", "name": "some-package", "nvr": "some-package-2.0-1.el10"},
            ]
            if buildID == 99907
            else [{"arch": "x86_64", "name": "golang", "nvr": "golang-1.22.7-1.el10"}]
        )
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    # Mock installed_pkgs.log fetch - return both x86_64 and aarch64 logs
    mock_response_x86 = flexmock(status_code=200, content=x86_64_log)
    mock_response_aarch64 = flexmock(status_code=200, content=aarch64_log)
    mock_response_404 = flexmock(status_code=404)

    async def mock_head(url, **kwargs):
        if "x86_64" in url:
            return mock_response_x86
        if "aarch64" in url:
            return mock_response_aarch64
        return mock_response_404

    async def mock_get(url, **kwargs):
        if "x86_64" in url:
            return mock_response_x86
        if "aarch64" in url:
            return mock_response_aarch64
        return mock_response_404

    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(mock_head)
    mock_client.should_receive("get").replace_with(mock_get)

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    # Mock _get_koji_build
    def mock_get_build(url, nvr):
        if nvr == "some-package-2.0-1.el10":
            return candidate_build
        if nvr == "golang-1.22.7-1.el10":
            return golang_build
        if nvr == "golang-1.22.5-1.el10":
            return fixed_build
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, issue_key, _nvr, _reason = await check_package_built_with_fixed_dependency(
        package="some-package",
        fix_version="rhel-10.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.22.5-1.el10"),
        available_tools=[mock_tool],
    )

    # Should recognize both architectures agree (epoch 0 == no epoch) and return True
    assert already_fixed is True
    assert issue_key == "RHEL-999999"
    assert _nvr == "some-package-2.0-1.el10"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_active_query_failed_no_closed():
    """Test when active query fails and no closed builds found - should raise exception."""
    from ymir.common.utils import TransientInfrastructureError

    mock_tool = flexmock()

    call_count = {"count": 0}

    async def mock_run_tool(tool, **kwargs):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return []  # No closed builds
        raise Exception("Jira timeout")  # Active query fails

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(mock_run_tool)

    # Should raise TransientInfrastructureError when no closed builds and active query failed
    with pytest.raises(TransientInfrastructureError, match="Jira query failed"):
        await check_package_built_with_fixed_dependency(
            package="some-pkg",
            fix_version="rhel-10.z",
            dep_component="golang",
            fixed_dep_nvr=NEVR.from_string("golang-1.22.7-1.el10"),
            available_tools=[mock_tool],
        )


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_closed_query_raises():
    """Test that closed-build Jira query failure raises TransientInfrastructureError."""
    from beeai_framework.tools import ToolError

    from ymir.common.utils import TransientInfrastructureError

    mock_tool = flexmock()

    flexmock(_ymir_utils).should_receive("run_tool").and_raise(ToolError("Jira connection refused"))

    with pytest.raises(TransientInfrastructureError, match="Closed-build Jira query failed"):
        await check_package_built_with_fixed_dependency(
            package="some-pkg",
            fix_version="rhel-10.z",
            dep_component="golang",
            fixed_dep_nvr=NEVR.from_string("golang-1.22.7-1.el10"),
            available_tools=[mock_tool],
        )


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_closed_query_malformed():
    """Test that malformed closed-build response raises TransientInfrastructureError."""
    from ymir.common.utils import TransientInfrastructureError

    mock_tool = flexmock()

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(lambda *args, **kwargs: _coro("not a list"))

    with pytest.raises(TransientInfrastructureError, match="Invalid response from closed-build"):
        await check_package_built_with_fixed_dependency(
            package="some-pkg",
            fix_version="rhel-10.z",
            dep_component="golang",
            fixed_dep_nvr=NEVR.from_string("golang-1.22.7-1.el10"),
            available_tools=[mock_tool],
        )


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_active_query_failed_with_closed():
    """An active-query outage must not make an old closed build look conclusive."""
    from ymir.common.utils import TransientInfrastructureError

    async def search(_tool, **kwargs):
        if "status in (Closed, Done)" in kwargs["jql"]:
            return [
                {
                    "key": "RHEL-777777",
                    "fields": {
                        "customfield_10578": "test-app-3.0-1.el10",
                    },
                }
            ]
        return None

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(search)

    with pytest.raises(TransientInfrastructureError, match="Active-build Jira query failed"):
        await check_package_built_with_fixed_dependency(
            package="test-app",
            fix_version="rhel-10.z",
            dep_component="golang",
            fixed_dep_nvr=NEVR.from_string("golang-1.22.5-1.el10"),
            available_tools=[flexmock()],
        )


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_closed_old_but_active_exists():
    """Test when closed build doesn't have fix but active builds exist - should request clarification."""
    mock_tool = flexmock()

    jira_closed = [
        {
            "key": "RHEL-111",
            "fields": {"customfield_10578": "pkg-1.0-1.el10"},
        }
    ]
    jira_active = [
        {
            "key": "RHEL-222",
            "fields": {
                "customfield_10578": "pkg-2.0-1.el10",
                "status": {"name": "In Progress"},
            },
        }
    ]

    installed_pkgs_log_content = b"""golang-1.22.4-1.el10.x86_64 1 2 abc installed
other-package-1.0-1.el10.x86_64 1 2 abc installed
"""

    closed_build = {
        "name": "pkg",
        "build_id": 99909,
        "epoch": None,
        "version": "1.0",
        "release": "1.el10",
    }
    used_golang = {"name": "golang", "epoch": None, "version": "1.22.4", "release": "1.el10", "build_id": 123}
    fixed_golang = {
        "name": "golang",
        "epoch": None,
        "version": "1.22.7",
        "release": "1.el10",
        "build_id": 124,
    }

    call_count = {"count": 0}

    async def mock_run_tool(tool, **kwargs):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return jira_closed
        return jira_active

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(mock_run_tool)

    # Mock Koji listRPMs for candidate build architecture lookup
    mock_session = flexmock()
    mock_session.should_receive("listRPMs").and_return(
        [{"arch": "x86_64", "name": "pkg", "nvr": "pkg-1.0-1.el10"}]
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_session)

    mock_head_response = flexmock(status_code=200)
    mock_get_response = flexmock(status_code=200, content=installed_pkgs_log_content)
    mock_client = flexmock()
    mock_client.should_receive("head").replace_with(
        lambda *a, **kw: _coro(mock_head_response)
    ).at_least().once()
    mock_client.should_receive("get").replace_with(
        lambda *a, **kw: _coro(mock_get_response)
    ).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    def mock_get_build(url, nvr):
        if nvr == "pkg-1.0-1.el10":
            return closed_build
        if nvr == "golang-1.22.4-1.el10":
            return used_golang
        if nvr == "golang-1.22.7-1.el10":
            return fixed_golang
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, issue_key, _nvr, reason = await check_package_built_with_fixed_dependency(
        package="pkg",
        fix_version="rhel-10.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.22.7-1.el10"),
        available_tools=[mock_tool],
    )

    # Should request clarification because active builds exist
    assert already_fixed is None
    assert reason == "active_builds_not_closed:RHEL-222"
    assert issue_key == "RHEL-111"  # Closed build issue
    assert _nvr == "pkg-1.0-1.el10"


@pytest.mark.asyncio
async def test_check_package_built_with_fixed_dependency_partial_logs_one_arch_missing():
    """Test when one architecture log is missing but another shows fixed dependency.

    This tests the fix for: "Missing architectures can be marked fixed"
    When one arch returns 404/410 and another shows the fixed dependency,
    should return clarification (partial_architecture_coverage), not True.
    """
    mock_tool = flexmock()

    jira_search_result = [
        {
            "key": "RHEL-242375",
            "fields": {
                "customfield_10578": "go-fdo-client-1.0.0-4.el10_2.7",
            },
        }
    ]

    # x86_64 log has the fixed dependency
    x86_64_log = b"golang-1.26.4-1.el10_2.x86_64 1 2 abc installed\n"
    # ppc64le log will be missing (404)

    candidate_build = {
        "name": "go-fdo-client",
        "build_id": 99910,
        "epoch": None,
        "version": "1.0.0",
        "release": "4.el10_2.7",
    }
    fixed_build = {
        "name": "golang",
        "epoch": None,
        "version": "1.26.4",
        "release": "1.el10_2",
        "build_id": 123456,
    }

    # Mock run_tool to return fresh coroutines each call (called twice: closed + active)
    flexmock(_ymir_utils).should_receive("run_tool").with_args(
        "search_jira_issues",
        available_tools=[mock_tool],
        jql=str,
        fields=list,
        max_results=50,
        fetch_all=True,
    ).replace_with(
        lambda *args, **kwargs: _coro(
            jira_search_result if "status in (Closed, Done)" in kwargs.get("jql", "") else []
        )
    )

    # Mock Koji listRPMs to return both x86_64 and ppc64le architectures
    mock_koji_session = flexmock()
    mock_koji_session.should_receive("listRPMs").replace_with(
        lambda buildID: (
            [
                {"arch": "x86_64", "name": "go-fdo-client", "nvr": "go-fdo-client-1.0.0-4.el10_2.7"},
                {"arch": "ppc64le", "name": "go-fdo-client", "nvr": "go-fdo-client-1.0.0-4.el10_2.7"},
            ]
            if buildID == 99910
            else [{"arch": "x86_64", "name": "golang", "nvr": "golang-1.26.4-1.el10_2"}]
        )
    )
    flexmock(koji).should_receive("ClientSession").and_return(mock_koji_session)

    # Mock httpx client: x86_64 succeeds, ppc64le returns 404
    mock_client = flexmock()

    def mock_head_fn(url):
        if "ppc64le" in url:
            return _coro(flexmock(status_code=404))
        return _coro(flexmock(status_code=200))

    def mock_get_fn(url):
        if "ppc64le" in url:
            return _coro(flexmock(status_code=404, content=b""))
        return _coro(flexmock(status_code=200, content=x86_64_log))

    mock_client.should_receive("head").replace_with(mock_head_fn).at_least().once()
    mock_client.should_receive("get").replace_with(mock_get_fn).at_least().once()

    flexmock(httpx).should_receive("AsyncClient").and_return(_AsyncContextManager(mock_client))

    # Mock _get_koji_build
    def mock_get_build(url, nvr):
        if nvr == "go-fdo-client-1.0.0-4.el10_2.7":
            return candidate_build
        if nvr == "golang-1.26.4-1.el10_2":
            return fixed_build
        return None

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, issue_key, _nvr, reason = await check_package_built_with_fixed_dependency(
        package="go-fdo-client",
        fix_version="rhel-10.2.z",
        dep_component="golang",
        fixed_dep_nvr=NEVR.from_string("golang-1.26.4-1.el10_2"),
        available_tools=[mock_tool],
    )

    # Should request clarification due to missing architecture, not return True
    assert already_fixed is None
    assert reason == "partial_architecture_coverage"
    assert issue_key == "RHEL-242375"
    assert _nvr == "go-fdo-client-1.0.0-4.el10_2.7"


@pytest.mark.asyncio
async def test_old_build_retries_brew_outage(monkeypatch):
    from ymir.common.utils import TransientInfrastructureError

    async def find(*_args):
        return [("RHEL-1", "pkg-1-1.el8")], []

    async def select(*_args):
        from specfile.utils import NEVR

        return NEVR(name="pkg", epoch=0, version="1", release="1.el8"), "RHEL-1"

    async def fetch(*_args, **_kwargs):
        raise TransientInfrastructureError("Brew HTTP 503")

    monkeypatch.setattr(_ymir_utils, "_find_completed_builds_jira", find)
    monkeypatch.setattr(_ymir_utils, "_select_highest_evr_build", select)
    monkeypatch.setattr(_ymir_utils, "_fetch_installed_pkgs_log", fetch)
    monkeypatch.setattr(
        _ymir_utils,
        "_get_koji_build",
        lambda *_args: {"name": "pkg", "build_id": 123, "completion_ts": 1},
    )
    session = flexmock()
    session.should_receive("listRPMs").and_return([{"arch": "x86_64"}])
    flexmock(koji).should_receive("ClientSession").and_return(session)

    with pytest.raises(TransientInfrastructureError, match="Brew HTTP 503"):
        await check_package_built_with_fixed_dependency("pkg", "rhel-8.z", "golang", "golang-1-1.el8", [])


@pytest.mark.asyncio
async def test_koji_lookup_exception_retries_after_candidate_selection(monkeypatch):
    from ymir.common.utils import TransientInfrastructureError

    async def find(*_args):
        return [("RHEL-1", "pkg-1-1.el8")], []

    async def select(*_args):
        from specfile.utils import NEVR

        return NEVR(name="pkg", epoch=0, version="1", release="1.el8"), "RHEL-1"

    def failed_lookup(*_args):
        raise ConnectionError("Koji unavailable")

    monkeypatch.setattr(_ymir_utils, "_find_completed_builds_jira", find)
    monkeypatch.setattr(_ymir_utils, "_select_highest_evr_build", select)
    monkeypatch.setattr(_ymir_utils, "_get_koji_build", failed_lookup)

    with pytest.raises(TransientInfrastructureError, match="Koji unavailable"):
        await check_package_built_with_fixed_dependency("pkg", "rhel-8.z", "golang", "golang-1-1.el8", [])


@pytest.mark.asyncio
async def test_check_package_built_filtered_active_empty_no_false_transient_error():
    """Regression: filtering active candidates to empty must not raise TransientInfrastructureError.

    When all closed builds resolve to the wrong package name and all active builds
    are also filtered out by package name, active_candidates should be [] (not None),
    so the function returns invalid_build_records instead of raising.
    """
    mock_tool = flexmock()

    closed_result = [
        {"key": "RHEL-111", "fields": {"customfield_10578": "wrong-pkg-1.0-1.el10"}},
    ]
    active_result = [
        {
            "key": "RHEL-222",
            "fields": {"customfield_10578": "also-wrong-1.0-1.el10", "status": {"name": "In Progress"}},
        },
    ]

    flexmock(_ymir_utils).should_receive("run_tool").replace_with(
        lambda *args, **kwargs: _coro(
            closed_result if "status in (Closed, Done)" in kwargs.get("jql", "") else active_result
        )
    )

    def mock_get_build(url, nvr):
        builds = {
            "wrong-pkg-1.0-1.el10": {
                "name": "wrong-pkg",
                "epoch": None,
                "version": "1.0",
                "release": "1.el10",
            },
            "also-wrong-1.0-1.el10": {
                "name": "also-wrong",
                "epoch": None,
                "version": "1.0",
                "release": "1.el10",
            },
        }
        return builds.get(nvr)

    flexmock(_ymir_utils).should_receive("_get_koji_build").replace_with(mock_get_build)

    already_fixed, _issue_key, _nvr, reason = await check_package_built_with_fixed_dependency(
        package="my-pkg",
        fix_version="rhel-10.0.z",
        dep_component="dep",
        fixed_dep_nvr=NEVR.from_string("dep-1.0-1.el10"),
        available_tools=[mock_tool],
    )

    assert already_fixed is None
    assert reason == "invalid_build_records:RHEL-111"
