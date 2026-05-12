from contextlib import asynccontextmanager

import aiohttp
import pytest
from beeai_framework.tools import ToolError
from flexmock import flexmock
from pydantic import ValidationError

from ymir.tools.privileged import logdetective as logdetective_module
from ymir.tools.privileged.logdetective import (
    AnalyzeLogsTool,
    AnalyzeLogsToolOutput,
    LogDetectiveFile,
    LogDetectiveResult,
    LogDetectiveSnippet,
)

SAMPLE_RESPONSE = {
    "explanation": {"text": "Build failed due to missing dependency libfoo."},
    "snippets": [
        {
            "text": "error: nothing provides libfoo needed by bar-1.0-1.el10.x86_64",
            "line_number": 42,
            "source_file": "build.log",
            "snippet_analysis": "Missing dependency in the buildroot.",
        },
    ],
    "solution": {"text": "Add libfoo to BuildRequires in the spec file."},
    "no_issue_found": False,
}

SAMPLE_RESPONSE_NO_ISSUE = {
    "explanation": {"text": "Build completed successfully."},
    "snippets": None,
    "solution": None,
    "no_issue_found": True,
}

BASE_URL = "http://logdetective.example.com"
TOKEN = "test-token-123"


@pytest.fixture(autouse=True)
def _patch_module_constants(monkeypatch):
    monkeypatch.setattr(logdetective_module, "LOG_DETECTIVE_URL", BASE_URL)
    monkeypatch.setattr(logdetective_module, "LOG_DETECTIVE_TOKEN", TOKEN)
    monkeypatch.setattr(logdetective_module, "MAX_LOG_DETECTIVE_FILES", 5)


def _mock_post(status, response_data=None, response_text=""):
    @asynccontextmanager
    async def post(url, json=None, headers=None):
        async def json_response():
            return response_data

        async def text():
            return response_text

        yield flexmock(status=status, json=json_response, text=text)

    return post


def _mock_post_capturing(status, response_data, captured):
    @asynccontextmanager
    async def post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers

        async def json_response():
            return response_data

        yield flexmock(status=status, json=json_response)

    return post


@pytest.mark.asyncio
async def test_analyze_logs_success():
    captured = {}
    flexmock(aiohttp.ClientSession).should_receive("post").replace_with(
        _mock_post_capturing(200, SAMPLE_RESPONSE, captured)
    )

    out = await AnalyzeLogsTool().run(
        input={
            "files": [
                {"name": "build.log", "url": "https://example.com/build.log"},
                {"name": "root.log", "url": "https://example.com/root.log"},
            ],
            "build_metadata": {"commentary": "PR #42 rebuild"},
        }
    )

    result = out.result
    assert result.explanation == "Build failed due to missing dependency libfoo."
    assert result.no_issue_found is False
    assert result.solution == "Add libfoo to BuildRequires in the spec file."
    assert len(result.snippets) == 1
    assert result.snippets[0].text == "error: nothing provides libfoo needed by bar-1.0-1.el10.x86_64"
    assert result.snippets[0].line_number == 42
    assert result.snippets[0].source_file == "build.log"
    assert result.snippets[0].snippet_analysis == "Missing dependency in the buildroot."

    assert captured["url"] == f"{BASE_URL}/analyze"
    assert captured["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert len(captured["json"]["files"]) == 2
    assert captured["json"]["files"][0] == {"name": "build.log", "url": "https://example.com/build.log"}
    assert captured["json"]["files"][1] == {"name": "root.log", "url": "https://example.com/root.log"}
    assert captured["json"]["build_metadata"] == {"commentary": "PR #42 rebuild"}


@pytest.mark.asyncio
async def test_analyze_logs_no_issue_found():
    flexmock(aiohttp.ClientSession).should_receive("post").replace_with(
        _mock_post(200, SAMPLE_RESPONSE_NO_ISSUE)
    )

    out = await AnalyzeLogsTool().run(
        input={"files": [{"name": "build.log", "url": "https://example.com/build.log"}]}
    )

    result = out.result
    assert result.no_issue_found is True
    assert result.snippets is None
    assert result.solution is None
    assert result.explanation == "Build completed successfully."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected_msg",
    [
        (401, "Authentication failed"),
        (503, "temporarily unavailable"),
        (422, "validation error"),
        (500, r"request failed \(500\)"),
    ],
)
async def test_analyze_logs_http_errors(status, expected_msg):
    flexmock(aiohttp.ClientSession).should_receive("post").replace_with(
        _mock_post(status, response_text="error details")
    )

    with pytest.raises(ToolError, match=expected_msg):
        await AnalyzeLogsTool().run(
            input={"files": [{"name": "build.log", "url": "https://example.com/build.log"}]}
        )


@pytest.mark.asyncio
async def test_analyze_logs_no_files():
    with pytest.raises(ToolError, match="Tool input validation error"):
        await AnalyzeLogsTool().run(input={"files": []})


@pytest.mark.asyncio
async def test_analyze_logs_duplicate_names():
    with pytest.raises(ToolError, match="unique"):
        await AnalyzeLogsTool().run(
            input={
                "files": [
                    {"name": "build.log", "url": "https://example.com/1.log"},
                    {"name": "build.log", "url": "https://example.com/2.log"},
                ]
            }
        )


def test_file_input_rejects_missing_url():
    with pytest.raises(ValidationError):
        LogDetectiveFile(name="build.log")


def test_file_input_rejects_extra_fields():
    with pytest.raises(ValidationError):
        LogDetectiveFile(name="build.log", url="https://example.com/build.log", content="data")


@pytest.mark.asyncio
async def test_analyze_logs_no_url_configured(monkeypatch):
    monkeypatch.setattr(logdetective_module, "LOG_DETECTIVE_URL", None)

    with pytest.raises(ToolError, match="URL not configured"):
        await AnalyzeLogsTool().run(
            input={"files": [{"name": "build.log", "url": "https://example.com/build.log"}]}
        )


@pytest.mark.asyncio
async def test_analyze_logs_without_token(monkeypatch):
    captured = {}
    flexmock(aiohttp.ClientSession).should_receive("post").replace_with(
        _mock_post_capturing(200, SAMPLE_RESPONSE, captured)
    )

    monkeypatch.setattr(logdetective_module, "LOG_DETECTIVE_TOKEN", None)

    await AnalyzeLogsTool().run(
        input={"files": [{"name": "build.log", "url": "https://example.com/build.log"}]}
    )

    assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_analyze_logs_malformed_response():
    flexmock(aiohttp.ClientSession).should_receive("post").replace_with(
        _mock_post(200, {"unexpected": "structure"})
    )

    with pytest.raises(ToolError, match="Unexpected response"):
        await AnalyzeLogsTool().run(
            input={"files": [{"name": "build.log", "url": "https://example.com/build.log"}]}
        )


@pytest.mark.asyncio
async def test_analyze_logs_non_json_response():
    @asynccontextmanager
    async def post(url, json=None, headers=None):
        async def bad_json():
            raise aiohttp.ContentTypeError(
                flexmock(real_url="http://example.com"),
                (),
                message="Attempt to decode JSON with unexpected mimetype: text/html",
            )

        yield flexmock(status=200, json=bad_json)

    flexmock(aiohttp.ClientSession).should_receive("post").replace_with(post)

    with pytest.raises(ToolError, match="non-JSON response"):
        await AnalyzeLogsTool().run(
            input={"files": [{"name": "build.log", "url": "https://example.com/build.log"}]}
        )


@pytest.mark.asyncio
async def test_analyze_logs_max_files_exceeded():
    files = [{"name": f"file{i}.log", "url": f"https://example.com/{i}.log"} for i in range(6)]

    with pytest.raises(ToolError, match="Tool input validation error"):
        await AnalyzeLogsTool().run(input={"files": files})


class TestGetTextContent:
    def test_full_response(self):
        result = LogDetectiveResult(
            explanation="Build failed due to missing dependency libfoo.",
            snippets=[
                LogDetectiveSnippet(
                    text="error: nothing provides libfoo",
                    line_number=42,
                    source_file="build.log",
                    snippet_analysis="Missing dependency.",
                ),
                LogDetectiveSnippet(
                    text="RPM build errors:",
                    line_number=100,
                    source_file="build.log",
                    snippet_analysis=None,
                ),
            ],
            solution="Add libfoo to BuildRequires.",
            no_issue_found=False,
        )
        text = AnalyzeLogsToolOutput(result=result).get_text_content()
        assert text.startswith("Explanation: Build failed")
        assert "- build.log:42: error: nothing provides libfoo" in text
        assert "  Analysis: Missing dependency." in text
        assert "- build.log:100: RPM build errors:" in text
        assert "Analysis:" not in text.split("RPM build errors:")[1].split("\n")[0]
        assert "Solution: Add libfoo to BuildRequires." in text

    def test_no_issue_found(self):
        result = LogDetectiveResult(
            explanation="Build completed successfully.",
            snippets=None,
            solution=None,
            no_issue_found=True,
        )
        text = AnalyzeLogsToolOutput(result=result).get_text_content()
        lines = text.split("\n")
        assert lines[0] == "No issues found."
        assert lines[1] == "Explanation: Build completed successfully."
        assert "Snippets:" not in text
        assert "Solution:" not in text

    def test_no_source_file(self):
        result = LogDetectiveResult(
            explanation="Error detected.",
            snippets=[
                LogDetectiveSnippet(
                    text="segfault",
                    line_number=7,
                    source_file=None,
                    snippet_analysis=None,
                ),
            ],
            solution=None,
            no_issue_found=False,
        )
        text = AnalyzeLogsToolOutput(result=result).get_text_content()
        assert "- line 7: segfault" in text
        assert "Solution:" not in text
