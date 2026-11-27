from contextlib import asynccontextmanager

import pytest
from flexmock import flexmock

import ymir.tools.unprivileged.greenwave as greenwave_mod
from ymir.tools.unprivileged.greenwave import FetchTestingFarmResultsInput, FetchTestingFarmResultsTool

ARTIFACT_URL = "https://artifacts.osci.redhat.com/testing-farm/cbcb1edf-5f3a-4bcf-98da-08a87e084cef"


class _StreamedBody:
    """Mimics aiohttp's StreamReader: read(n) returns only the first buffered chunk, not n bytes."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def read(self, n=-1):
        return self._chunks[0]

    async def iter_chunked(self, n):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_fetch_returns_whole_results_xml_when_body_arrives_in_chunks():
    chunks = [b"<testsuites>" + b"a" * 8000, b"b" * 5000 + b"</testsuites>"]
    response = flexmock(status=200, content=_StreamedBody(chunks))

    @asynccontextmanager
    async def fake_get(session, url, **kwargs):
        yield response

    flexmock(greenwave_mod).should_receive("aiohttp_get_with_retries").replace_with(fake_get)

    tool = FetchTestingFarmResultsTool()
    result = await tool.run(input=FetchTestingFarmResultsInput(artifact_url=ARTIFACT_URL))

    assert result.result == b"".join(chunks).decode("utf-8")


@pytest.mark.asyncio
async def test_fetch_summarizes_junit_xml_to_one_line_per_testcase(monkeypatch):
    junit = (
        '<testsuites><testsuite name="/plans/all">'
        '<testcase name="/Security/CVE-2026-1"><system-out>' + "noise" * 5000 + "</system-out></testcase>"
        '<testcase name="/Regression/bz1"><failure message="assert 1 == 2">long trace</failure></testcase>'
        '<testcase name="/Smoke/x" result="error"/>'
        "</testsuite></testsuites>"
    )
    response = flexmock(status=200, content=_StreamedBody([junit.encode()]))

    @asynccontextmanager
    async def fake_get(session, url, **kwargs):
        yield response

    flexmock(greenwave_mod).should_receive("aiohttp_get_with_retries").replace_with(fake_get)

    result = await FetchTestingFarmResultsTool().run(
        input=FetchTestingFarmResultsInput(artifact_url=ARTIFACT_URL)
    )

    assert result.result == (
        "Testsuite /plans/all: 3 testcases\n"
        "  [passed] /Security/CVE-2026-1\n"
        "  [failed] /Regression/bz1: assert 1 == 2\n"
        "  [error] /Smoke/x"
    )
