import pytest

from supervisor import compare_xunit
from supervisor.compare_xunit import (
    XUnitTestCaseComparison,
    XUnitTestCaseResult,
    XUnitComparison,
    XUnitComparisonCounts,
    XUnitComparisonStatus,
    XUnitParseError,
    compare_xunit_files,
    parse_xunit,
)


# Test fixtures - minimal but realistic XUnit XML examples


def create_minimal_xunit(test_cases_xml: str, suite_name: str = "/plans/test") -> str:
    """Create a minimal Testing Farm XUnit XML with the given test cases."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<testsuites overall-result="passed">
  <testsuite name="{suite_name}" result="passed" tests="1" stage="complete">
    <testing-environment name="provisioned">
      <property name="arch" value="x86_64"/>
    </testing-environment>
{test_cases_xml}
  </testsuite>
</testsuites>"""


def create_testcase_xml(
    name: str,
    result: str = "pass",
    url: str = "https://example.com/git/tests",
    ref: str = "master",
    log_url: str = "https://example.com/log.txt",
) -> str:
    """Create a minimal testcase XML element."""
    result_element = ""
    if result == "fail":
        result_element = "    <failure/>"
    elif result == "error":
        result_element = "    <error/>"
    elif result == "skipped":
        result_element = "    <skipped/>"

    log_element = ""
    if log_url:
        log_element = f"""    <logs>
      <log href="{log_url}" name="testout.log"/>
    </logs>"""

    return f"""    <testcase name="{name}" result="{result}">
      <fmf-id url="{url}" ref="{ref}" name="{name}"/>
{log_element}
{result_element}
    </testcase>"""


# Tests for parse_xunit


class TestParseXunit:
    def test_parse_valid_xunit_single_suite(self):
        """Test parsing a valid XUnit file with a single test suite."""
        testcase = create_testcase_xml("/test/case1", "pass")
        xunit_xml = create_minimal_xunit(testcase)

        suites = parse_xunit(xunit_xml)

        assert len(suites) == 1
        assert suites[0].name == "/plans/test"
        assert suites[0].arch == "x86_64"
        assert len(suites[0].test_cases) == 1
        assert suites[0].test_cases[0].name == "/test/case1"
        assert suites[0].test_cases[0].result == XUnitTestCaseResult.PASS
        assert suites[0].test_cases[0].url == "https://example.com/git/tests"
        assert suites[0].test_cases[0].ref == "master"
        assert suites[0].test_cases[0].log_url == "https://example.com/log.txt"

    def test_parse_xunit_different_result_types(self):
        """Test parsing test cases with different result types."""
        testcases = "\n".join(
            [
                create_testcase_xml("/test/pass", "pass"),
                create_testcase_xml("/test/fail", "fail"),
                create_testcase_xml("/test/error", "error"),
                create_testcase_xml("/test/skipped", "skipped"),
            ]
        )
        xunit_xml = create_minimal_xunit(testcases)

        suites = parse_xunit(xunit_xml)

        results = [tc.result for tc in suites[0].test_cases]
        assert results == [
            XUnitTestCaseResult.PASS,
            XUnitTestCaseResult.FAIL,
            XUnitTestCaseResult.ERROR,
            XUnitTestCaseResult.SKIPPED,
        ]

    def test_parse_xunit_missing_log_url(self):
        """Test parsing when log URL is missing (optional field)."""
        testcase = create_testcase_xml("/test/case1", "pass", log_url="")
        xunit_xml = create_minimal_xunit(testcase)

        suites = parse_xunit(xunit_xml)

        assert suites[0].test_cases[0].log_url == ""

    def test_parse_xunit_multiple_suites(self):
        """Test parsing XUnit with multiple test suites."""
        xunit_xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites overall-result="passed">
  <testsuite name="/plans/suite1" result="passed" tests="1" stage="complete">
    <testing-environment name="provisioned">
      <property name="arch" value="x86_64"/>
    </testing-environment>
    <testcase name="/test/case1" result="pass">
      <fmf-id url="https://example.com/git/tests" ref="master" name="/test/case1"/>
    </testcase>
  </testsuite>
  <testsuite name="/plans/suite2" result="passed" tests="1" stage="complete">
    <testing-environment name="provisioned">
      <property name="arch" value="aarch64"/>
    </testing-environment>
    <testcase name="/test/case2" result="pass">
      <fmf-id url="https://example.com/git/tests" ref="master" name="/test/case2"/>
    </testcase>
  </testsuite>
</testsuites>"""

        suites = parse_xunit(xunit_xml)

        assert len(suites) == 2
        assert suites[0].name == "/plans/suite1"
        assert suites[0].arch == "x86_64"
        assert suites[1].name == "/plans/suite2"
        assert suites[1].arch == "aarch64"

    def test_parse_xunit_invalid_xml(self):
        """Test that invalid XML raises XUnitParseError."""
        invalid_xml = "<testsuites><unclosed>"

        with pytest.raises(XUnitParseError, match="Failed to parse XUnit XML"):
            parse_xunit(invalid_xml)

    def test_parse_xunit_missing_arch(self):
        """Test that missing architecture raises ValueError."""
        xunit_xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites overall-result="passed">
  <testsuite name="/plans/test" result="passed" tests="1" stage="complete">
    <testcase name="/test/case1" result="pass">
      <fmf-id url="https://example.com/git/tests" ref="master" name="/test/case1"/>
    </testcase>
  </testsuite>
</testsuites>"""

        with pytest.raises(ValueError, match="Architecture not found"):
            parse_xunit(xunit_xml)

    def test_parse_xunit_missing_testcase_name(self):
        """Test that missing testcase name raises XUnitParseError."""
        xunit_xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites overall-result="passed">
  <testsuite name="/plans/test" result="passed" tests="1" stage="complete">
    <testing-environment name="provisioned">
      <property name="arch" value="x86_64"/>
    </testing-environment>
    <testcase result="pass">
      <fmf-id url="https://example.com/git/tests" ref="master" name="/test/case1"/>
    </testcase>
  </testsuite>
</testsuites>"""

        with pytest.raises(XUnitParseError, match="Test case without name found"):
            parse_xunit(xunit_xml)

    def test_parse_xunit_missing_fmf_id(self):
        """Test that missing fmf-id raises XUnitParseError."""
        xunit_xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites overall-result="passed">
  <testsuite name="/plans/test" result="passed" tests="1" stage="complete">
    <testing-environment name="provisioned">
      <property name="arch" value="x86_64"/>
    </testing-environment>
    <testcase name="/test/case1" result="pass">
    </testcase>
  </testsuite>
</testsuites>"""

        with pytest.raises(XUnitParseError, match="doesn't have fmf-id"):
            parse_xunit(xunit_xml)

    def test_parse_xunit_incomplete_fmf_id(self):
        """Test that incomplete fmf-id (missing url or ref) raises XUnitParseError."""
        xunit_xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites overall-result="passed">
  <testsuite name="/plans/test" result="passed" tests="1" stage="complete">
    <testing-environment name="provisioned">
      <property name="arch" value="x86_64"/>
    </testing-environment>
    <testcase name="/test/case1" result="pass">
      <fmf-id url="https://example.com/git/tests" name="/test/case1"/>
    </testcase>
  </testsuite>
</testsuites>"""

        with pytest.raises(XUnitParseError, match="has incomplete fmf-id"):
            parse_xunit(xunit_xml)


# Tests for compare_xunit_files and comparison logic


def create_mock_http_session(xunit_xml_responses: list[str], monkeypatch):
    """Create a mocked HTTP session that returns the given XUnit XML responses in order."""
    responses_iter = iter(xunit_xml_responses)

    # Create a class that acts as an async context manager for the response
    class MockResponse:
        def __init__(self, xml_content):
            self.status = 200
            self._xml_content = xml_content

        def raise_for_status(self):
            pass

        async def text(self):
            return self._xml_content

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    # Create mock session with a get method that returns a MockResponse
    class MockSession:
        def get(self, url):
            return MockResponse(next(responses_iter))

    mock_session = MockSession()

    # Patch the aiohttp_session function in the compare_xunit module
    monkeypatch.setattr(compare_xunit, "aiohttp_session", lambda: mock_session)

    return mock_session


class TestCompareXunitFiles:
    @pytest.mark.parametrize(
        "result1,result2,expected_category,expected_field",
        [
            # WORKS: both pass
            ("pass", "pass", "works", "works"),
            # REGRESSION: pass -> fail/error
            ("pass", "fail", "regression", "regression"),
            ("pass", "error", "regression", "regression"),
            # FIXED: fail/error -> pass
            ("fail", "pass", "fixed", "fixed"),
            ("error", "pass", "fixed", "fixed"),
            # BROKEN: fail/error in both
            ("fail", "fail", "broken", "broken"),
            ("error", "error", "broken", "broken"),
            ("fail", "error", "broken", "broken"),
            ("error", "fail", "broken", "broken"),
            # DIFFERENCE: other combinations
            ("pass", "skipped", "difference", "difference"),
            ("skipped", "pass", "difference", "difference"),
            ("fail", "skipped", "difference", "difference"),
        ],
    )
    @pytest.mark.asyncio
    async def test_compare_result_combinations(
        self, result1, result2, expected_category, expected_field, monkeypatch
    ):
        """Test various result combinations are classified correctly."""
        testcase1 = create_testcase_xml("/test/case1", result1)
        testcase2 = create_testcase_xml("/test/case1", result2)

        xunit_xml_1 = create_minimal_xunit(testcase1)
        xunit_xml_2 = create_minimal_xunit(testcase2)

        create_mock_http_session([xunit_xml_1, xunit_xml_2], monkeypatch)

        result = await compare_xunit_files(
            "https://example.com/file1.xml", "https://example.com/file2.xml"
        )

        # Check the count for the expected category
        assert getattr(result.total_counts, expected_category) == 1

        # Check the list for the expected field (unless it's "works" which isn't listed)
        if expected_field != "works":
            comparison_list = getattr(result, expected_field)
            assert len(comparison_list) == 1

    @pytest.mark.asyncio
    async def test_compare_missing_tests(self, monkeypatch):
        """Test that tests missing in either file are handled correctly."""
        testcases_1 = "\n".join(
            [
                create_testcase_xml("/test/case1", "pass"),
                create_testcase_xml("/test/case2", "pass"),
            ]
        )
        testcases_2 = "\n".join(
            [
                create_testcase_xml("/test/case1", "pass"),
                create_testcase_xml("/test/case3", "pass"),
            ]
        )

        xunit_xml_1 = create_minimal_xunit(testcases_1)
        xunit_xml_2 = create_minimal_xunit(testcases_2)

        create_mock_http_session([xunit_xml_1, xunit_xml_2], monkeypatch)

        result = await compare_xunit_files(
            "https://example.com/file1.xml", "https://example.com/file2.xml"
        )

        # case1: PASS -> PASS = WORKS
        # case2: PASS -> MISSING = DIFFERENCE
        # case3: MISSING -> PASS = DIFFERENCE
        assert result.total_counts.works == 1
        assert result.total_counts.difference == 2
        assert len(result.difference) == 2

        # Verify one has MISSING in file b, one has MISSING in file a
        missing_in_file_b = [
            d for d in result.difference if d.result_b == XUnitTestCaseResult.MISSING
        ]
        missing_in_file_a = [
            d for d in result.difference if d.result_a == XUnitTestCaseResult.MISSING
        ]
        assert len(missing_in_file_b) == 1
        assert len(missing_in_file_a) == 1

    @pytest.mark.asyncio
    async def test_compare_with_metadata(self, monkeypatch):
        """Test that metadata is included in the comparison result and TOML output."""
        testcase = create_testcase_xml("/test/case1", "pass")
        xunit_xml = create_minimal_xunit(testcase)

        create_mock_http_session([xunit_xml, xunit_xml], monkeypatch)

        metadata = {
            "baseline_build": "rhel-9.5.0-20250101.1",
            "candidate_build": "rhel-9.5.0-20250102.1",
            "test_run_id": "12345",
        }

        result = await compare_xunit_files(
            "https://example.com/file1.xml",
            "https://example.com/file2.xml",
            metadata=metadata,
        )

        # Verify metadata is in the result
        assert result.metadata == metadata
        assert result.metadata["baseline_build"] == "rhel-9.5.0-20250101.1"
        assert result.metadata["candidate_build"] == "rhel-9.5.0-20250102.1"
        assert result.metadata["test_run_id"] == "12345"

        # Verify metadata appears in TOML output
        toml_output = result.to_toml()
        assert "[metadata]" in toml_output
        assert 'baseline_build = "rhel-9.5.0-20250101.1"' in toml_output
        assert 'candidate_build = "rhel-9.5.0-20250102.1"' in toml_output
        assert 'test_run_id = "12345"' in toml_output

    @pytest.mark.asyncio
    async def test_compare_multiple_test_cases(self, monkeypatch):
        """Test comparison with multiple test cases of different types."""
        testcases_1 = "\n".join(
            [
                create_testcase_xml("/test/works", "pass"),
                create_testcase_xml("/test/regression", "pass"),
                create_testcase_xml("/test/fixed", "fail"),
                create_testcase_xml("/test/broken", "error"),
            ]
        )
        testcases_2 = "\n".join(
            [
                create_testcase_xml("/test/works", "pass"),
                create_testcase_xml("/test/regression", "fail"),
                create_testcase_xml("/test/fixed", "pass"),
                create_testcase_xml("/test/broken", "fail"),
            ]
        )

        xunit_xml_1 = create_minimal_xunit(testcases_1)
        xunit_xml_2 = create_minimal_xunit(testcases_2)

        create_mock_http_session([xunit_xml_1, xunit_xml_2], monkeypatch)

        result = await compare_xunit_files(
            "https://example.com/file1.xml", "https://example.com/file2.xml"
        )

        assert result.total_counts.works == 1
        assert result.total_counts.regression == 1
        assert result.total_counts.fixed == 1
        assert result.total_counts.broken == 1
        assert len(result.regression) == 1
        assert len(result.fixed) == 1
        assert len(result.broken) == 1
        assert result.regression[0].name == "/test/regression"
        assert result.fixed[0].name == "/test/fixed"
        assert result.broken[0].name == "/test/broken"

    @pytest.mark.asyncio
    async def test_compare_mismatched_suites_raises_error(self, monkeypatch):
        """Test that mismatched test suites between files raises ValueError."""
        testcase = create_testcase_xml("/test/case1", "pass")
        xunit_xml_1 = create_minimal_xunit(testcase, suite_name="/plans/suite1")
        xunit_xml_2 = create_minimal_xunit(testcase, suite_name="/plans/suite2")

        create_mock_http_session([xunit_xml_1, xunit_xml_2], monkeypatch)

        with pytest.raises(
            ValueError, match="XUnit files contain different test suites"
        ):
            await compare_xunit_files(
                "https://example.com/file1.xml", "https://example.com/file2.xml"
            )

    @pytest.mark.asyncio
    async def test_compare_multiple_suites_same_tests(self, monkeypatch):
        """Test comparison with multiple test suites containing the same tests."""
        xunit_xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites overall-result="passed">
  <testsuite name="/plans/suite1" result="passed" tests="1" stage="complete">
    <testing-environment name="provisioned">
      <property name="arch" value="x86_64"/>
    </testing-environment>
    <testcase name="/test/case1" result="pass">
      <fmf-id url="https://example.com/git/tests" ref="master" name="/test/case1"/>
      <logs>
        <log href="https://example.com/log1.txt" name="testout.log"/>
      </logs>
    </testcase>
  </testsuite>
  <testsuite name="/plans/suite2" result="passed" tests="1" stage="complete">
    <testing-environment name="provisioned">
      <property name="arch" value="aarch64"/>
    </testing-environment>
    <testcase name="/test/case1" result="pass">
      <fmf-id url="https://example.com/git/tests" ref="master" name="/test/case1"/>
      <logs>
        <log href="https://example.com/log2.txt" name="testout.log"/>
      </logs>
    </testcase>
  </testsuite>
</testsuites>"""

        create_mock_http_session([xunit_xml, xunit_xml], monkeypatch)

        result = await compare_xunit_files(
            "https://example.com/file1.xml", "https://example.com/file2.xml"
        )

        # Both suites have the same test passing in both files
        assert result.total_counts.works == 2


# Tests for XUnitComparison output


class TestXUnitComparisonOutput:
    def test_comparison_result_counts(self):
        """Test that XUnitComparisonCounts are correctly initialized and updated."""
        counts = XUnitComparisonCounts()
        assert counts.works == 0
        assert counts.regression == 0
        assert counts.fixed == 0
        assert counts.broken == 0
        assert counts.difference == 0

        counts.works = 5
        counts.regression = 2
        assert counts.works == 5
        assert counts.regression == 2

    def test_xunit_comparison_default_values(self):
        """Test that XUnitComparison has correct default values."""
        comparison = XUnitComparison(
            status=XUnitComparisonStatus(
                generated=True, reason="Comparison generated successfully"
            )
        )
        assert comparison.status.generated is True
        assert comparison.status.reason == "Comparison generated successfully"
        assert comparison.total_counts.works == 0
        assert len(comparison.regression) == 0
        assert len(comparison.fixed) == 0
        assert len(comparison.broken) == 0
        assert len(comparison.difference) == 0

    def test_xunit_comparison_to_toml(self):
        """Test TOML output generation with multiple entries and empty list removal."""
        comparison = XUnitComparison(
            status=XUnitComparisonStatus(
                generated=True, reason="Comparison generated successfully"
            )
        )
        comparison.total_counts.works = 10
        comparison.total_counts.regression = 2

        comparison.regression.append(
            XUnitTestCaseComparison(
                name="/test/case1",
                arch="x86_64",
                url="https://example.com/git/tests",
                ref="master",
                result_a=XUnitTestCaseResult.PASS,
                result_b=XUnitTestCaseResult.FAIL,
                log_url_a="https://example.com/log1.txt",
                log_url_b="https://example.com/log2.txt",
            )
        )
        comparison.regression.append(
            XUnitTestCaseComparison(
                name="/test/case2",
                arch="aarch64",
                url="https://example.com/git/tests",
                ref="master",
                result_a=XUnitTestCaseResult.PASS,
                result_b=XUnitTestCaseResult.ERROR,
                log_url_a="https://example.com/log3.txt",
                log_url_b="https://example.com/log4.txt",
            )
        )

        toml_output = comparison.to_toml()

        # Basic structure
        assert "# XUnit Comparison Report" in toml_output
        assert "[status]" in toml_output
        assert "generated = true" in toml_output
        assert 'reason = "Comparison generated successfully"' in toml_output
        assert "[total_counts]" in toml_output
        assert "works = 10" in toml_output
        assert "regression = 2" in toml_output

        # Multiple entries
        assert toml_output.count("[[regression]]") == 2
        assert 'name = "/test/case1"' in toml_output
        assert 'name = "/test/case2"' in toml_output
        assert 'arch = "x86_64"' in toml_output
        assert 'arch = "aarch64"' in toml_output
        assert 'result_a = "pass"' in toml_output
        assert 'result_b = "fail"' in toml_output

        # Empty lists should not appear
        assert "[[fixed]]" not in toml_output
        assert "[[broken]]" not in toml_output
        assert "[[difference]]" not in toml_output

    def test_test_case_comparison_fields(self):
        """Test that XUnitTestCaseComparison has all required fields."""
        comparison = XUnitTestCaseComparison(
            name="/test/case1",
            arch="x86_64",
            url="https://example.com/git/tests",
            ref="master",
            result_a=XUnitTestCaseResult.PASS,
            result_b=XUnitTestCaseResult.FAIL,
            log_url_a="https://example.com/log1.txt",
            log_url_b="https://example.com/log2.txt",
        )

        assert comparison.name == "/test/case1"
        assert comparison.arch == "x86_64"
        assert comparison.url == "https://example.com/git/tests"
        assert comparison.ref == "master"
        assert comparison.result_a == XUnitTestCaseResult.PASS
        assert comparison.result_b == XUnitTestCaseResult.FAIL
        assert comparison.log_url_a == "https://example.com/log1.txt"
        assert comparison.log_url_b == "https://example.com/log2.txt"

    def test_xunit_comparison_empty_total_counts_removed_from_toml(self):
        """Test that total_counts is removed from TOML when all counts are 0."""
        comparison = XUnitComparison(
            status=XUnitComparisonStatus(
                generated=False, reason="XUnit results missing for runs A and B"
            ),
            metadata={"build_a": "build1", "build_b": "build2"},
        )

        toml_output = comparison.to_toml()

        # Status and metadata should be present
        assert "[status]" in toml_output
        assert "generated = false" in toml_output
        assert 'reason = "XUnit results missing for runs A and B"' in toml_output
        assert "[metadata]" in toml_output
        assert 'build_a = "build1"' in toml_output
        assert 'build_b = "build2"' in toml_output

        # total_counts should be removed when empty
        assert "[total_counts]" not in toml_output
