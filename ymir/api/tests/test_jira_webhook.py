"""Unit tests for the Jira webhook endpoint."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from ymir.api.jira_webhook import (
    _extract_command,
    _extract_command_from_adf,
)
from ymir.api.server import create_app

BOT_ACCOUNT_ID = "557058:test-bot-id"
WEBHOOK_SECRET = "test-secret-value"  # pragma: allowlist secret


class FakeRedis:
    """Minimal in-memory Redis mock for hash operations."""

    def __init__(self):
        self._data: dict[str, dict[str, bytes]] = {}

    async def hget(self, name: str, key: str):
        return self._data.get(name, {}).get(key)

    async def hset(self, name: str, key: str, value: str | bytes):
        self._data.setdefault(name, {})[key] = value.encode() if isinstance(value, str) else value

    async def hdel(self, name: str, *keys: str):
        bucket = self._data.get(name, {})
        for k in keys:
            bucket.pop(k, None)

    async def hgetall(self, name: str):
        return dict(self._data.get(name, {}))

    async def eval(self, script: str, num_keys: int, *args):
        return None


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("JIRA_BOT_ACCOUNT_ID", BOT_ACCOUNT_ID)
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", WEBHOOK_SECRET)


@pytest_asyncio.fixture
async def client(fake_redis):
    app = create_app(redis_conn=fake_redis)
    async with TestClient(TestServer(app)) as c:
        yield c


# -- ADF helpers --------------------------------------------------------------


def _adf_mention_body(command_text: str) -> dict:
    """Build a realistic Jira Cloud ADF comment body with a bot mention."""
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": BOT_ACCOUNT_ID,
                            "text": "@Ymir Bot",
                            "userType": "DEFAULT",
                        },
                    },
                    {"type": "text", "text": f" {command_text}"},
                ],
            }
        ],
    }


def _adf_plain_body(text: str) -> dict:
    """Build an ADF document with plain text only (no mention)."""
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def _comment_payload(body: dict | str, event: str = "comment_created") -> dict:
    return {
        "webhookEvent": event,
        "comment": {
            "body": body,
            "author": {"accountId": "user-123", "displayName": "Test User"},
        },
        "issue": {"key": "RHEL-99999"},
    }


# -- Unit tests for extraction helpers ----------------------------------------


class TestExtractCommandFromAdf:
    def test_basic_mention_and_command(self):
        adf = _adf_mention_body("consolidate expat rhel-9.8.0")
        assert _extract_command_from_adf(adf, BOT_ACCOUNT_ID) == "consolidate expat rhel-9.8.0"

    def test_no_mention(self):
        adf = _adf_plain_body("just a comment")
        assert _extract_command_from_adf(adf, BOT_ACCOUNT_ID) is None

    def test_wrong_account_id(self):
        adf = _adf_mention_body("consolidate expat rhel-9.8.0")
        assert _extract_command_from_adf(adf, "wrong-id") is None

    def test_mention_without_trailing_text(self):
        adf = {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "mention",
                            "attrs": {"id": BOT_ACCOUNT_ID, "text": "@Bot"},
                        },
                    ],
                }
            ],
        }
        assert _extract_command_from_adf(adf, BOT_ACCOUNT_ID) is None

    def test_mention_with_multiple_text_nodes(self):
        """Text split across multiple nodes is concatenated."""
        adf = {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "mention",
                            "attrs": {"id": BOT_ACCOUNT_ID, "text": "@Bot"},
                        },
                        {"type": "text", "text": " consolidate"},
                        {"type": "text", "text": " expat rhel-9.8.0"},
                    ],
                }
            ],
        }
        assert _extract_command_from_adf(adf, BOT_ACCOUNT_ID) == "consolidate expat rhel-9.8.0"

    def test_empty_content(self):
        assert _extract_command_from_adf({"type": "doc", "content": []}, BOT_ACCOUNT_ID) is None

    def test_missing_content_key(self):
        assert _extract_command_from_adf({"type": "doc"}, BOT_ACCOUNT_ID) is None


class TestExtractCommand:
    def test_dict_dispatches_to_adf(self):
        adf = _adf_mention_body("consolidate expat rhel-9.8.0")
        assert _extract_command(adf, BOT_ACCOUNT_ID) == "consolidate expat rhel-9.8.0"

    def test_non_dict_returns_none(self):
        assert _extract_command("a string", BOT_ACCOUNT_ID) is None  # type: ignore[arg-type]

    def test_other_type_returns_none(self):
        assert _extract_command(42, BOT_ACCOUNT_ID) is None  # type: ignore[arg-type]


# -- Authentication -----------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_secret_header(client):
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(_adf_mention_body("consolidate expat rhel-9.8.0")),
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_wrong_secret_header(client):
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(_adf_mention_body("consolidate expat rhel-9.8.0")),
        headers={"X-Webhook-Secret": "wrong-value"},  # pragma: allowlist secret
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_valid_secret_header(client):
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(_adf_mention_body("consolidate expat rhel-9.8.0")),
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )
    assert resp.status == 201


# -- Event filtering ----------------------------------------------------------


@pytest.mark.asyncio
async def test_non_comment_event_ignored(client):
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(_adf_plain_body("anything"), event="jira:issue_updated"),
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ignored"] is True


# -- Bot mention detection ----------------------------------------------------


@pytest.mark.asyncio
async def test_no_bot_mention_ignored(client):
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(_adf_plain_body("just a regular comment")),
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ignored"] is True
    assert body["reason"] == "no bot mention"


# -- Command dispatch ---------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidate_command_basic(client):
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(_adf_mention_body("consolidate expat rhel-9.8.0")),
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["submitted"] is True


@pytest.mark.asyncio
async def test_consolidate_command_with_options(client):
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(
            _adf_mention_body(
                "consolidate expat rhel-9.8.0 --source-issues RHEL-111 RHEL-222 --release-strategy merged"
            )
        ),
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["submitted"] is True


@pytest.mark.asyncio
async def test_unknown_command(client):
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(_adf_mention_body("do-something-unknown arg1")),
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )
    assert resp.status == 400
    body = await resp.json()
    assert "unknown command" in body["error"]


@pytest.mark.asyncio
async def test_malformed_consolidate_args(client):
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(_adf_mention_body("consolidate")),
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )
    assert resp.status == 400
    body = await resp.json()
    assert "invalid consolidate arguments" in body["error"]


# -- Bot account not configured -----------------------------------------------


@pytest.mark.asyncio
async def test_bot_account_not_configured(client, monkeypatch):
    monkeypatch.delenv("JIRA_BOT_ACCOUNT_ID")
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(_adf_mention_body("consolidate expat rhel-9.8.0")),
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ignored"] is True
    assert body["reason"] == "bot account not configured"


# -- Fail-closed when secret not configured ------------------------------------


@pytest.mark.asyncio
async def test_fail_closed_when_secret_not_configured(client, monkeypatch):
    """When JIRA_WEBHOOK_SECRET is unset, requests must be rejected (fail closed)."""
    monkeypatch.delenv("JIRA_WEBHOOK_SECRET")
    resp = await client.post(
        "/api/jira/webhook",
        json=_comment_payload(_adf_mention_body("consolidate expat rhel-9.8.0")),
    )
    assert resp.status == 500
    body = await resp.json()
    assert "webhook secret not configured" in body["error"]


# -- Error comment scheduling -------------------------------------------------


@pytest.mark.asyncio
async def test_error_triggers_jira_comment(client):
    """A 400 response from dispatch should schedule a Jira comment."""
    with patch("ymir.api.jira_webhook.jira_reply.post_comment", new_callable=AsyncMock) as mock_post:
        resp = await client.post(
            "/api/jira/webhook",
            json=_comment_payload(_adf_mention_body("do-something-unknown arg1")),
            headers={"X-Webhook-Secret": WEBHOOK_SECRET},
        )
        assert resp.status == 400
        await asyncio.sleep(0)  # let fire-and-forget create_task drain

    mock_post.assert_awaited_once()
    call_args = mock_post.call_args
    assert call_args[0][0] == "RHEL-99999"
    assert "unknown command" in call_args[0][1]


@pytest.mark.asyncio
async def test_success_does_not_trigger_jira_comment(client):
    """A 201 success response should NOT schedule a Jira comment."""
    with patch("ymir.api.jira_webhook.jira_reply.post_comment", new_callable=AsyncMock) as mock_post:
        resp = await client.post(
            "/api/jira/webhook",
            json=_comment_payload(_adf_mention_body("consolidate expat rhel-9.8.0")),
            headers={"X-Webhook-Secret": WEBHOOK_SECRET},
        )
        assert resp.status == 201

    mock_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_comment_with_missing_issue_key(client):
    """If the webhook payload has no issue key, no comment should be scheduled."""
    payload = {
        "webhookEvent": "comment_created",
        "comment": {"body": _adf_mention_body("do-something-unknown arg1")},
    }
    with patch("ymir.api.jira_webhook.jira_reply.post_comment", new_callable=AsyncMock) as mock_post:
        resp = await client.post(
            "/api/jira/webhook",
            json=payload,
            headers={"X-Webhook-Secret": WEBHOOK_SECRET},
        )
        assert resp.status == 400
        await asyncio.sleep(0)

    mock_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_consolidate_triggers_comment(client):
    """Malformed consolidate args should trigger an error comment."""
    with patch("ymir.api.jira_webhook.jira_reply.post_comment", new_callable=AsyncMock) as mock_post:
        resp = await client.post(
            "/api/jira/webhook",
            json=_comment_payload(_adf_mention_body("consolidate")),
            headers={"X-Webhook-Secret": WEBHOOK_SECRET},
        )
        assert resp.status == 400
        await asyncio.sleep(0)  # let fire-and-forget create_task drain

    mock_post.assert_awaited_once()
    assert mock_post.call_args[0][0] == "RHEL-99999"
    assert "invalid consolidate arguments" in mock_post.call_args[0][1]
