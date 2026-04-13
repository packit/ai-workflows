import asyncio
import os
import subprocess

import httpx
import pytest
from flexmock import flexmock

import ymir.common.utils as _ymir_utils
from ymir.common.utils import (
    KerberosError,
    _is_connection_error,
    extract_principal,
    init_kerberos_ticket,
    mcp_tools,
)


async def _coro(val):
    return val


async def _noop(*args, **kwargs):
    pass


class TestInitKerberosTicket:
    """Test cases for init_kerberos_ticket() function."""

    @pytest.mark.asyncio
    async def test_missing_krb5ccname_raises_error(self, monkeypatch):
        """Test that missing KRB5CCNAME environment variable raises KerberosError."""
        monkeypatch.delenv("KRB5CCNAME", raising=False)
        monkeypatch.delenv("KEYTAB_FILE", raising=False)

        with pytest.raises(KerberosError, match="KRB5CCNAME environment variable is not set"):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_ccache_file_not_exists_no_keytab_raises_error(self, monkeypatch):
        """Test that non-existent ccache file with no keytab raises error."""
        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        monkeypatch.setenv("KRB5CCNAME", "/nonexistent/ccache")
        flexmock(os.path).should_receive("exists").with_args("/nonexistent/ccache").and_return(False)
        flexmock(asyncio).should_receive("create_subprocess_exec").never()

        # we should avoid calling klist when the ccache file doesn't exist
        with pytest.raises(
            KerberosError,
            match="No valid Kerberos ticket found and KEYTAB_FILE is not set",
        ):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_klist_command_failure_raises_error(self, monkeypatch):
        """Test that klist command failure raises KerberosError."""
        mock_proc = flexmock(returncode=1)
        mock_proc.should_receive("communicate").and_return(_coro((b"error output", b"stderr output")))

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(_coro(mock_proc))

        with pytest.raises(KerberosError, match="Failed to list Kerberos tickets"):
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
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
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
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
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
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
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
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(_coro(mock_proc))

        from ymir.common import utils

        flexmock(utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
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
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)

        def mock_create_subprocess(*args, **kwargs):
            if args[0] == "klist":
                return _coro(mock_klist_proc)
            if args[0] == "kinit":
                return _coro(mock_kinit_proc)
            return None

        flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(mock_create_subprocess)

        from ymir.common import utils

        flexmock(utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
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
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)

        def mock_create_subprocess(*args, **kwargs):
            if args[0] == "klist":
                return _coro(mock_klist_proc)
            if args[0] == "kinit":
                return _coro(mock_kinit_proc)
            return None

        flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(mock_create_subprocess)

        from ymir.common import utils

        flexmock(utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
            _coro("jotnar-bot@IPA.REDHAT.COM")
        )

        with pytest.raises(KerberosError, match="kinit command failed"):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_keytab_extract_principal_failure(self, monkeypatch):
        """Test extract_principal failure raises error."""
        monkeypatch.setenv("KEYTAB_FILE", "/path/to/keytab")
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")

        from ymir.common import utils

        flexmock(utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
            _coro(None)
        )

        with pytest.raises(KerberosError, match="Failed to extract principal from keytab file"):
            await init_kerberos_ticket()

    @pytest.mark.parametrize(
        ("ccache_name", "path_to_check", "expect_exists_call"),
        [
            ("KCM:1000", None, False),
            ("FILE:/path/to/ccache", "/path/to/ccache", True),
            ("/path/to/ccache", "/path/to/ccache", True),
        ],
        ids=["kcm_type", "file_type", "legacy_format"],
    )
    @pytest.mark.asyncio
    async def test_krb5ccname_type_handling(
        self, monkeypatch, ccache_name, path_to_check, expect_exists_call
    ):
        """Test that KRB5CCNAME is handled correctly for different types."""
        klist_output = (
            b"Principal name                 Cache name\n"
            b"--------------                 ----------\n"
            b"user@EXAMPLE.COM         KCM:1000\n"
        )
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(_coro((klist_output, b"")))

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        monkeypatch.setenv("KRB5CCNAME", ccache_name)

        if expect_exists_call:
            flexmock(os.path).should_receive("exists").with_args(path_to_check).and_return(True)
        else:
            flexmock(os.path).should_receive("exists").never()

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
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
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
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
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
    def __init__(self, exc=None):
        self._exc = exc

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return flexmock(), flexmock()

    async def __aexit__(self, *args):
        return False


def make_sse_cm(exc=None):
    """Async context manager mock for sse_client. Raises exc on entry if given."""
    return _SSEContextManager(exc)


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
    flexmock(_ymir_utils.MCPTool).should_receive("from_client").and_return(_coro(FAKE_TOOLS))
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
    flexmock(_ymir_utils.MCPTool).should_receive("from_client").and_return(_coro(FAKE_TOOLS))
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
