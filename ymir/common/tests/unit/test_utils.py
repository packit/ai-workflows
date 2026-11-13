import asyncio
import subprocess

import httpx
import koji
import pytest
from flexmock import flexmock
from specfile.utils import EVR

import ymir.common.utils as _ymir_utils
from ymir.common.base_utils import KerberosError, extract_principal, init_kerberos_ticket
from ymir.common.utils import (
    NoBuildFoundError,
    _is_connection_error,
    _is_inherited_ystream_build,
    check_build_in_buildroot,
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
# check_build_in_buildroot
# ============================================================================


@pytest.mark.parametrize(
    ("latest_release", "fixed_release", "expected"),
    [
        ("1.el9", "1.el9_9", True),
        ("1.el10", "1.el10_2", True),
        # Different release base.
        ("1.el9", "2.el9_9", False),
        # Extra Z-stream suffix means a Z-stream-only change.
        ("1.el9", "1.el9_9.1", False),
        # Both Z-stream builds: no inheritance.
        ("1.el9_8", "1.el9_9", False),
        # Different major.
        ("1.el10", "1.el9_9", False),
        # No dist tag.
        ("1", "1.el9_9", False),
    ],
)
def test_is_inherited_ystream_build(latest_release, fixed_release, expected):
    latest = EVR(epoch=0, version="1.27.3", release=latest_release)
    fixed = EVR(epoch=0, version="1.27.3", release=fixed_release)
    assert _is_inherited_ystream_build(latest, fixed) is expected


def test_is_inherited_ystream_build_requires_same_epoch_and_version():
    fixed = EVR(epoch=0, version="1.27.3", release="1.el9_9")
    assert not _is_inherited_ystream_build(EVR(epoch=1, version="1.27.3", release="1.el9"), fixed)
    assert not _is_inherited_ystream_build(EVR(epoch=0, version="1.27.2", release="1.el9"), fixed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixed_version", "fixed_release", "latest_version", "latest_release", "expected"),
    [
        # Same version-release, different dist tag: the CentOS Stream build
        # inherits the Z-stream fix.
        ("1.27.3", "1.el9_9", "1.27.3", "1.el9", True),
        # Same version, lower release: not the same build.
        ("1.27.3", "2.el9_9", "1.27.3", "1.el9", False),
        # Newer build satisfies the fixed build.
        ("1.27.2", "1.el9_9", "1.27.3", "1.el9", True),
        # Older build does not.
        ("1.27.3", "1.el9_9", "1.27.2", "1.el9", False),
    ],
)
async def test_check_build_in_buildroot_compares_version_release(
    monkeypatch,
    fixed_version,
    fixed_release,
    latest_version,
    latest_release,
    expected,
):
    async def _mock_load_rhel_config():
        return {"current_y_streams": {"9": "rhel-9.10"}}

    fixed_build = {
        "epoch": 0,
        "version": fixed_version,
        "release": fixed_release,
        "nvr": f"golang-{fixed_version}-{fixed_release}",
    }
    latest_build = {
        "epoch": 0,
        "version": latest_version,
        "release": latest_release,
        "nvr": f"golang-{latest_version}-{latest_release}",
    }

    monkeypatch.setattr("ymir.common.config.load_rhel_config", _mock_load_rhel_config)
    monkeypatch.setattr(_ymir_utils, "_get_koji_build", lambda *_args: fixed_build)
    monkeypatch.setattr(_ymir_utils, "_get_latest_koji_build", lambda *_args: latest_build)

    assert (
        await check_build_in_buildroot(
            "c9s",
            "golang",
            fixed_build["nvr"],
            fix_version="rhel-9.10",
        )
        is expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_branch", "fix_version", "expected_tags"),
    [
        # Internal Z-stream branch: Brew only.
        ("rhel-9.9.0", "rhel-9.9.z", ["rhel-9.9.0-z-build"]),
        # CS branch with a Z-stream fix: CS Koji and Brew are both checked.
        ("c9s", "rhel-9.9.z", ["c9s-build", "rhel-9.9.0-z-build"]),
    ],
)
async def test_check_build_in_buildroot_keeps_zstream_comparison_strict(
    monkeypatch, target_branch, fix_version, expected_tags
):
    """A .el9 build with the same version-release must not satisfy an .el9_9 fix in a Z-stream buildroot."""

    async def _mock_load_rhel_config():
        return {"current_y_streams": {"9": "rhel-9.10"}}

    fixed_build = {"epoch": 0, "version": "1.2.3", "release": "1.el9_9", "nvr": "dep-1.2.3-1.el9_9"}
    latest_build = {"epoch": 0, "version": "1.2.3", "release": "1.el9", "nvr": "dep-1.2.3-1.el9"}
    queried_tags = []

    def _mock_latest(_koji_url, build_tag, _package):
        queried_tags.append(build_tag)
        return latest_build

    monkeypatch.setattr("ymir.common.config.load_rhel_config", _mock_load_rhel_config)
    monkeypatch.setattr(_ymir_utils, "_get_koji_build", lambda *_args: fixed_build)
    monkeypatch.setattr(_ymir_utils, "_get_latest_koji_build", _mock_latest)

    assert (
        await check_build_in_buildroot(target_branch, "dep", fixed_build["nvr"], fix_version=fix_version)
        is False
    )
    assert sorted(queried_tags) == sorted(expected_tags)


@pytest.mark.asyncio
async def test_check_build_in_buildroot_accepts_inherited_build_in_brew_ystream(monkeypatch):
    """The Brew Y-stream buildroot holds .el9 builds, like CentOS Stream."""

    async def _mock_load_rhel_config():
        return {"current_y_streams": {"9": "rhel-9.10"}}

    fixed_build = {"epoch": 0, "version": "1.27.3", "release": "1.el9_9", "nvr": "golang-1.27.3-1.el9_9"}
    latest_build = {"epoch": 0, "version": "1.27.3", "release": "1.el9", "nvr": "golang-1.27.3-1.el9"}
    queried_tags = []

    def _mock_latest(_koji_url, build_tag, _package):
        queried_tags.append(build_tag)
        return latest_build

    monkeypatch.setattr("ymir.common.config.load_rhel_config", _mock_load_rhel_config)
    monkeypatch.setattr(_ymir_utils, "_get_koji_build", lambda *_args: fixed_build)
    monkeypatch.setattr(_ymir_utils, "_get_latest_koji_build", _mock_latest)

    assert (
        await check_build_in_buildroot("rhel-9.10.0", "golang", fixed_build["nvr"], fix_version="rhel-9.10")
        is True
    )
    assert queried_tags == ["rhel-9.10.0-build"]
