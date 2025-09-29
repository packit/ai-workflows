import asyncio
import os
import subprocess
import pytest
from flexmock import flexmock
from unittest.mock import AsyncMock

from common.utils import init_kerberos_ticket, KerberosError, extract_principal


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
        flexmock(os.path).should_receive("exists").with_args("/nonexistent/ccache").and_return(
            False
        )
        mock_subprocess = flexmock(asyncio).should_receive("create_subprocess_exec").never()

        # we should avoid calling klist when the ccache file doesn't exist
        with pytest.raises(
            KerberosError, match="No valid Kerberos ticket found and KEYTAB_FILE is not set"
        ):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_klist_command_failure_raises_error(self, monkeypatch):
        """Test that klist command failure raises KerberosError."""
        mock_proc = flexmock(returncode=1)
        mock_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(b"error output", b"stderr output"))()
        )

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(AsyncMock(return_value=mock_proc)())

        with pytest.raises(KerberosError, match="Failed to list Kerberos tickets"):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_valid_ticket_in_cache_returns_principal(self, monkeypatch):
        """Test that valid ticket in cache returns the principal."""
        klist_output = b"""Principal name                 Cache name
--------------                 ----------
user@EXAMPLE.COM         KCM:1000
"""
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(klist_output, b""))()
        )

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(AsyncMock(return_value=mock_proc)())

        result = await init_kerberos_ticket()
        assert result == "user@EXAMPLE.COM"

    @pytest.mark.asyncio
    async def test_expired_ticket_ignored(self, monkeypatch):
        """Test that expired tickets are ignored."""
        klist_output = b"""Principal name                 Cache name
--------------                 ----------
user@EXAMPLE.COM         FILE:.secrets/ccache/krb5cc (Expired)
"""
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(klist_output, b""))()
        )

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(AsyncMock(return_value=mock_proc)())

        with pytest.raises(
            KerberosError, match="No valid Kerberos ticket found and KEYTAB_FILE is not set"
        ):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_no_tickets_in_cache(self, monkeypatch):
        """Test behavior when klist returns no tickets."""
        klist_output = b"""Principal name                 Cache name
--------------                 ----------
"""
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(klist_output, b""))()
        )

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(AsyncMock(return_value=mock_proc)())

        with pytest.raises(
            KerberosError, match="No valid Kerberos ticket found and KEYTAB_FILE is not set"
        ):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_keytab_principal_already_in_cache(self, monkeypatch):
        """Test that existing keytab principal in cache is used."""
        klist_output = b"""Principal name                 Cache name
--------------                 ----------
jotnar-bot@IPA.REDHAT.COM    KCM:1000
"""
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(klist_output, b""))()
        )

        monkeypatch.setenv("KEYTAB_FILE", "/path/to/keytab")
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(AsyncMock(return_value=mock_proc)())

        from common import utils

        flexmock(utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
            AsyncMock(return_value="jotnar-bot@IPA.REDHAT.COM")()
        )

        result = await init_kerberos_ticket()
        assert result == "jotnar-bot@IPA.REDHAT.COM"

    @pytest.mark.asyncio
    async def test_keytab_kinit_success(self, monkeypatch):
        """Test successful kinit with keytab when principal not in cache."""
        klist_output = b"""Principal name                 Cache name
--------------                 ----------
"""
        mock_klist_proc = flexmock(returncode=0)
        mock_klist_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(klist_output, b""))()
        )

        mock_kinit_proc = flexmock(returncode=0)
        mock_kinit_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(b"error output", b"stderr output"))()
        )

        monkeypatch.setenv("KEYTAB_FILE", "/path/to/keytab")
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)

        def mock_create_subprocess(*args, **kwargs):
            if args[0] == "klist":
                return AsyncMock(return_value=mock_klist_proc)()
            elif args[0] == "kinit":
                return AsyncMock(return_value=mock_kinit_proc)()

        flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
            mock_create_subprocess
        )

        from common import utils

        flexmock(utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
            AsyncMock(return_value="jotnar-bot@IPA.REDHAT.COM")()
        )

        result = await init_kerberos_ticket()
        assert result == "jotnar-bot@IPA.REDHAT.COM"

    @pytest.mark.asyncio
    async def test_keytab_kinit_failure(self, monkeypatch):
        """Test kinit failure with keytab raises error."""
        klist_output = b"""Principal name                 Cache name
--------------                 ----------
"""
        mock_klist_proc = flexmock(returncode=0)
        mock_klist_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(klist_output, b""))()
        )

        mock_kinit_proc = flexmock(returncode=1)
        mock_kinit_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(b"error output", b"stderr output"))()
        )

        monkeypatch.setenv("KEYTAB_FILE", "/path/to/keytab")
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)

        def mock_create_subprocess(*args, **kwargs):
            if args[0] == "klist":
                return AsyncMock(return_value=mock_klist_proc)()
            elif args[0] == "kinit":
                return AsyncMock(return_value=mock_kinit_proc)()

        flexmock(asyncio).should_receive("create_subprocess_exec").replace_with(
            mock_create_subprocess
        )

        from common import utils

        flexmock(utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
            AsyncMock(return_value="jotnar-bot@IPA.REDHAT.COM")()
        )

        with pytest.raises(KerberosError, match="kinit command failed"):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_keytab_extract_principal_failure(self, monkeypatch):
        """Test extract_principal failure raises error."""
        monkeypatch.setenv("KEYTAB_FILE", "/path/to/keytab")
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")

        from common import utils

        flexmock(utils).should_receive("extract_principal").with_args("/path/to/keytab").and_return(
            AsyncMock(return_value=None)()
        )

        with pytest.raises(KerberosError, match="Failed to extract principal from keytab file"):
            await init_kerberos_ticket()

    @pytest.mark.asyncio
    async def test_multiple_valid_principals_returns_first(self, monkeypatch):
        """Test that first valid principal is returned when multiple exist."""
        klist_output = b"""Principal name                 Cache name
--------------                 ----------
user1@EXAMPLE.COM         KCM:1000
user2@EXAMPLE.COM         KCM:1001
"""
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(klist_output, b""))()
        )

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(AsyncMock(return_value=mock_proc)())

        result = await init_kerberos_ticket()
        assert result == "user1@EXAMPLE.COM"

    @pytest.mark.asyncio
    async def test_mixed_valid_and_expired_principals(self, monkeypatch):
        """Test that expired principals are ignored and valid ones are used."""
        klist_output = b"""Principal name                 Cache name
--------------                 ----------
expired@EXAMPLE.COM      FILE:.secrets/ccache/krb5cc (Expired)
valid@EXAMPLE.COM        KCM:1000
"""
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(klist_output, b""))()
        )

        monkeypatch.delenv("KEYTAB_FILE", raising=False)
        monkeypatch.setenv("KRB5CCNAME", "/path/to/ccache")
        flexmock(os.path).should_receive("exists").with_args("/path/to/ccache").and_return(True)
        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist", "-l", stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).and_return(AsyncMock(return_value=mock_proc)())

        result = await init_kerberos_ticket()
        assert result == "valid@EXAMPLE.COM"


class TestExtractPrincipal:
    """Test cases for extract_principal() helper function."""

    @pytest.mark.asyncio
    async def test_extract_principal_success(self):
        """Test successful principal extraction from keytab."""
        klist_output = b"""Keytab name: FILE:openshift/jotnar-bot.keytab
KVNO Principal
---- --------------------------------------------------------------------------
   2 jotnar-bot@IPA.REDHAT.COM (aes256-cts-hmac-sha1-96)  (0xabcdef0000000000000000000000000000000000000000000000000000000000)
   2 jotnar-bot@IPA.REDHAT.COM (aes128-cts-hmac-sha1-96)  (0xabcdef000000000000000000000000000)
"""
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(klist_output, b""))()
        )

        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist",
            "-k",
            "-K",
            "-e",
            "/path/to/keytab",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).and_return(AsyncMock(return_value=mock_proc)())

        result = await extract_principal("/path/to/keytab")
        assert result == "jotnar-bot@IPA.REDHAT.COM"

    @pytest.mark.asyncio
    async def test_extract_principal_klist_failure(self):
        """Test extract_principal when klist command fails."""
        mock_proc = flexmock(returncode=1)
        mock_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(b"error", b"stderr"))()
        )

        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist",
            "-k",
            "-K",
            "-e",
            "/path/to/keytab",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).and_return(AsyncMock(return_value=mock_proc)())

        with pytest.raises(KerberosError, match="klist command failed"):
            await extract_principal("/path/to/keytab")

    @pytest.mark.asyncio
    async def test_extract_principal_no_valid_key(self):
        """Test extract_principal when no valid key found in output."""
        klist_output = b"""Keytab name: FILE:openshift/jotnar-bot.keytab
KVNO Principal
---- --------------------------------------------------------------------------
"""
        mock_proc = flexmock(returncode=0)
        mock_proc.should_receive("communicate").and_return(
            AsyncMock(return_value=(klist_output, b""))()
        )

        flexmock(asyncio).should_receive("create_subprocess_exec").with_args(
            "klist",
            "-k",
            "-K",
            "-e",
            "/path/to/keytab",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).and_return(AsyncMock(return_value=mock_proc)())

        with pytest.raises(KerberosError, match="No valid key found in the keytab file"):
            await extract_principal("/path/to/keytab")
