from contextlib import asynccontextmanager

import aiohttp
import pytest
from flexmock import flexmock

import ymir.tools.privileged.reviewer_resolver
from ymir.common.base_utils import KerberosError
from ymir.tools.privileged.reviewer_resolver import (
    _resolve_via_ldap,
    fetch_bugzilla_component_data,
    parse_component_file,
    resolve_gitlab_user_id,
    resolve_reviewers,
)

SAMPLE_COMPONENT_FILE = """\
Default Assignee: maintainer@redhat.com
QA Contact: qaengineer@redhat.com
Docs Contact:
Cc List:
"""


def test_parse_component_file():
    fields = parse_component_file(SAMPLE_COMPONENT_FILE)
    assert fields["Default Assignee"] == "maintainer@redhat.com"
    assert fields["QA Contact"] == "qaengineer@redhat.com"
    assert "Docs Contact" not in fields
    assert "Cc List" not in fields


def test_parse_component_file_empty():
    assert parse_component_file("") == {}


@pytest.mark.asyncio
async def test_fetch_bugzilla_component_data_success():
    @asynccontextmanager
    async def get(url, **kwargs):
        assert "RHEL10/bash" in url

        async def text():
            return SAMPLE_COMPONENT_FILE

        yield flexmock(status=200, text=text)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await fetch_bugzilla_component_data("bash", "10")
    assert result is not None
    assert result["Default Assignee"] == "maintainer@redhat.com"
    assert result["QA Contact"] == "qaengineer@redhat.com"


@pytest.mark.asyncio
async def test_fetch_bugzilla_component_data_not_found():
    @asynccontextmanager
    async def get(url, **kwargs):
        yield flexmock(status=404)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await fetch_bugzilla_component_data("nonexistent-pkg", "10")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_bugzilla_component_data_server_error():
    @asynccontextmanager
    async def get(url, **kwargs):
        yield flexmock(status=500)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await fetch_bugzilla_component_data("bash", "10")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_gitlab_user_id_by_email(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")

    @asynccontextmanager
    async def get(url, headers=None, params=None):
        assert headers["PRIVATE-TOKEN"] == "test-token"
        if params:
            assert params["search"] == "maintainer@redhat.com"

            async def json():
                return [{"id": 42, "username": "maintainer"}]

            yield flexmock(status=200, json=json)
        else:
            assert "/users/42" in url

            async def json():
                return {"id": 42, "public_email": "maintainer@redhat.com"}

            yield flexmock(status=200, json=json)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await resolve_gitlab_user_id("maintainer@redhat.com")
    assert result == 42


@pytest.mark.asyncio
async def test_resolve_gitlab_user_id_no_email_match(monkeypatch):
    """When the email search returns users but none match on public_email, return None."""
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    flexmock(ymir.tools.privileged.reviewer_resolver).should_receive("_resolve_via_ldap").replace_with(
        _no_ldap_fallback
    ).once()

    @asynccontextmanager
    async def get(url, headers=None, params=None):
        if params:

            async def json():
                return [{"id": 99, "username": "someone"}]

            yield flexmock(status=200, json=json)
        else:

            async def json():
                return {"id": 99, "public_email": "other@example.com"}

            yield flexmock(status=200, json=json)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await resolve_gitlab_user_id("maintainer@redhat.com")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_gitlab_user_id_not_found(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    flexmock(ymir.tools.privileged.reviewer_resolver).should_receive("_resolve_via_ldap").replace_with(
        _no_ldap_fallback
    ).once()

    @asynccontextmanager
    async def get(url, headers=None, params=None):
        async def json():
            return []

        yield flexmock(status=200, json=json)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await resolve_gitlab_user_id("nobody@redhat.com")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_gitlab_user_id_no_token(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)

    result = await resolve_gitlab_user_id("user@redhat.com")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_reviewers_full_flow(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    user_map = {
        "maintainer@redhat.com": 42,
        "qaengineer@redhat.com": 99,
    }
    id_to_email = {v: k for k, v in user_map.items()}

    @asynccontextmanager
    async def get(url, headers=None, params=None):
        if "gitlab.cee.redhat.com" in url:
            assert "RHEL10/bash" in url

            async def text():
                return SAMPLE_COMPONENT_FILE

            yield flexmock(status=200, text=text)
        elif params:
            email = params["search"]
            if email in user_map:

                async def json():
                    return [{"id": user_map[email], "username": email.split("@")[0]}]

                yield flexmock(status=200, json=json)
            else:

                async def json():
                    return []

                yield flexmock(status=200, json=json)
        else:
            user_id = int(url.rsplit("/", 1)[-1])

            async def json():
                return {"id": user_id, "public_email": id_to_email[user_id]}

            yield flexmock(status=200, json=json)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await resolve_reviewers("bash", "c10s")
    assert sorted(result) == [42, 99]


@pytest.mark.asyncio
async def test_resolve_reviewers_partial_failure(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    flexmock(ymir.tools.privileged.reviewer_resolver).should_receive("_resolve_via_ldap").replace_with(
        _no_ldap_fallback
    )

    @asynccontextmanager
    async def get(url, headers=None, params=None):
        if "gitlab.cee.redhat.com" in url:

            async def text():
                return SAMPLE_COMPONENT_FILE

            yield flexmock(status=200, text=text)
        elif params:
            search = params["search"]
            if search == "maintainer@redhat.com":

                async def json():
                    return [{"id": 42, "username": "maintainer"}]

                yield flexmock(status=200, json=json)
            else:

                async def json():
                    return []

                yield flexmock(status=200, json=json)
        else:

            async def json():
                return {"id": 42, "public_email": "maintainer@redhat.com"}

            yield flexmock(status=200, json=json)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await resolve_reviewers("bash", "c10s")
    assert result == [42]


@pytest.mark.asyncio
async def test_resolve_reviewers_component_not_found(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")

    @asynccontextmanager
    async def get(url, **kwargs):
        yield flexmock(status=404)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await resolve_reviewers("nonexistent-pkg", "c10s")
    assert result == []


@pytest.mark.parametrize(
    "branch, expected_rhel_major",
    [
        ("c10s", "10"),
        ("c9s", "9"),
        ("rhel-9.7.0", "9"),
        ("rhel-10.1", "10"),
    ],
)
@pytest.mark.asyncio
async def test_resolve_reviewers_maps_branch_to_rhel_version(
    monkeypatch,
    branch,
    expected_rhel_major,
):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    fetched_urls = []

    @asynccontextmanager
    async def get(url, headers=None, params=None):
        if "gitlab.cee.redhat.com" in url:
            fetched_urls.append(url)

            async def text():
                return "Default Assignee: user@redhat.com\n"

            yield flexmock(status=200, text=text)
        elif params:

            async def json():
                return [{"id": 1, "username": "user"}]

            yield flexmock(status=200, json=json)
        else:

            async def json():
                return {"id": 1, "public_email": "user@redhat.com"}

            yield flexmock(status=200, json=json)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    await resolve_reviewers("bash", branch)
    assert len(fetched_urls) == 1
    assert f"RHEL{expected_rhel_major}/bash" in fetched_urls[0]


@pytest.mark.asyncio
async def test_resolve_reviewers_unparseable_branch():
    result = await resolve_reviewers("bash", "some-unknown-branch")
    assert result == []


@pytest.mark.asyncio
async def test_resolve_reviewers_deduplicates(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")

    @asynccontextmanager
    async def get(url, headers=None, params=None):
        if "gitlab.cee.redhat.com" in url:

            async def text():
                return "Default Assignee: same@redhat.com\nQA Contact: same@redhat.com\n"

            yield flexmock(status=200, text=text)
        elif params:

            async def json():
                return [{"id": 42, "username": "same"}]

            yield flexmock(status=200, json=json)
        else:

            async def json():
                return {"id": 42, "public_email": "same@redhat.com"}

            yield flexmock(status=200, json=json)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await resolve_reviewers("bash", "c10s")
    assert result == [42]


LDAP_OUTPUT_WITH_GITLAB = """\
dn: uid=jdoe,cn=users,cn=accounts,dc=ipa,dc=redhat,dc=com
rhatSocialURL: Gitlab->https://gitlab.com/jdoe
rhatSocialURL: Github->https://github.com/jdoe
"""

LDAP_OUTPUT_NO_GITLAB = """\
dn: uid=someone,cn=users,cn=accounts,dc=ipa,dc=redhat,dc=com
rhatSocialURL: Github->https://github.com/someone
"""

LDAP_OUTPUT_EMPTY = """\
dn: uid=nobody,cn=users,cn=accounts,dc=ipa,dc=redhat,dc=com
"""

LDAP_HEADERS = {"PRIVATE-TOKEN": "test-token", "User-Agent": "ymir"}


async def _noop():
    pass


async def _no_ldap_fallback(*_args, **_kwargs):
    return None


def _mock_run_subprocess(stdout, returncode=0, stderr=""):
    async def _fake(cmd, **kwargs):
        return (returncode, stdout, stderr)

    flexmock(ymir.tools.privileged.reviewer_resolver).should_receive("run_subprocess").replace_with(
        _fake
    ).once()


@pytest.mark.asyncio
async def test_resolve_via_ldap_finds_gitlab_username(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    flexmock(ymir.tools.privileged.reviewer_resolver).should_receive("init_kerberos_ticket").replace_with(
        _noop
    ).once()

    _mock_run_subprocess(LDAP_OUTPUT_WITH_GITLAB)

    @asynccontextmanager
    async def get(url, headers=None, params=None):
        assert params == {"username": "jdoe"}

        async def json():
            return [{"id": 77, "username": "jdoe"}]

        yield flexmock(status=200, json=json)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await _resolve_via_ldap("jdoe@redhat.com", LDAP_HEADERS)
    assert result == 77


@pytest.mark.asyncio
async def test_resolve_via_ldap_no_social_url():
    flexmock(ymir.tools.privileged.reviewer_resolver).should_receive("init_kerberos_ticket").replace_with(
        _noop
    ).once()

    _mock_run_subprocess(LDAP_OUTPUT_EMPTY)

    result = await _resolve_via_ldap("nobody@redhat.com", LDAP_HEADERS)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_via_ldap_no_gitlab_url():
    flexmock(ymir.tools.privileged.reviewer_resolver).should_receive("init_kerberos_ticket").replace_with(
        _noop
    ).once()

    _mock_run_subprocess(LDAP_OUTPUT_NO_GITLAB)

    result = await _resolve_via_ldap("someone@redhat.com", LDAP_HEADERS)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_via_ldap_kerberos_failure():
    flexmock(ymir.tools.privileged.reviewer_resolver).should_receive("init_kerberos_ticket").and_raise(
        KerberosError("no ticket")
    ).once()

    result = await _resolve_via_ldap("user@redhat.com", LDAP_HEADERS)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_gitlab_user_id_falls_back_to_ldap(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    flexmock(ymir.tools.privileged.reviewer_resolver).should_receive("init_kerberos_ticket").replace_with(
        _noop
    ).once()

    _mock_run_subprocess(LDAP_OUTPUT_WITH_GITLAB)

    @asynccontextmanager
    async def get(url, headers=None, params=None):
        if params and "search" in params:

            async def json():
                return []

            yield flexmock(status=200, json=json)
        elif params and "username" in params:
            assert params["username"] == "jdoe"

            async def json():
                return [{"id": 77, "username": "jdoe"}]

            yield flexmock(status=200, json=json)
        else:

            async def json():
                return []

            yield flexmock(status=200, json=json)

    flexmock(aiohttp.ClientSession).should_receive("get").replace_with(get)

    result = await resolve_gitlab_user_id("jdoe@redhat.com")
    assert result == 77
