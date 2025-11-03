import asyncio
from enum import StrEnum
import xml.etree.ElementTree as ET

from pydantic import BaseModel, Field
import tomli_w

from .http_utils import aiohttp_session


class XUnitComparisonStatus(BaseModel):
    generated: bool
    reason: str | None = None


class XUnitTestCaseResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"
    MISSING = "missing"


class XUnitTestCase(BaseModel):
    name: str
    url: str
    ref: str
    log_url: str
    result: XUnitTestCaseResult


class XUnitTestSuite(BaseModel):
    arch: str
    name: str
    test_cases: list[XUnitTestCase]


class ComparisonResult(StrEnum):
    WORKS = "works"
    """Test case that passed in both reports."""
    REGRESSION = "regression"
    """Test case that passed in the first report but failed or errored in the second."""
    FIXED = "fixed"
    """Test case that failed or errored in the first report but passed in the second."""
    BROKEN = "broken"
    """Test case that failed or errored in both reports."""
    DIFFERENCE = "difference"
    """Test case with other combinations of status."""


class XUnitTestCaseComparison(BaseModel):
    name: str
    arch: str
    url: str
    ref: str
    result_a: XUnitTestCaseResult
    result_b: XUnitTestCaseResult
    log_url_a: str
    log_url_b: str


class XUnitComparisonCounts(BaseModel):
    works: int = 0
    regression: int = 0
    fixed: int = 0
    broken: int = 0
    difference: int = 0


TOML_HEADER = """\
# XUnit Comparison Report
# It contains the results of comparing two XUnit test reports.
# Each section lists test cases that differ between the two reports.
# The 'total_counts' section summarizes the number of test cases in each comparison category.
#
# Comparison categories:
# - regression: Test cases that passed in the first report but failed in the second.
# - fixed: Test cases that failed in the first report but passed in the second.
# - broken: Test cases that failed in both reports.
# - works: Test cases that passed in both reports (not listed in detail).
# - difference: Test cases with other combinations of status.
"""


class XUnitComparison(BaseModel):
    status: XUnitComparisonStatus
    metadata: dict[str, str] = Field(default_factory=dict)
    total_counts: XUnitComparisonCounts = Field(default_factory=XUnitComparisonCounts)
    regression: list[XUnitTestCaseComparison] = Field(default_factory=list)
    fixed: list[XUnitTestCaseComparison] = Field(default_factory=list)
    broken: list[XUnitTestCaseComparison] = Field(default_factory=list)
    difference: list[XUnitTestCaseComparison] = Field(default_factory=list)

    def to_toml(self) -> str:
        dict_output = self.model_dump()

        # Clean up the output
        if sum(c for c in dict_output["total_counts"].values()) == 0:
            del dict_output["total_counts"]

        empty_lists = [
            k for k, v in dict_output.items() if isinstance(v, list) and not v
        ]
        for k in empty_lists:
            del dict_output[k]

        return TOML_HEADER + tomli_w.dumps(dict_output)


class XUnitParseError(Exception):
    pass


def parse_xunit(xml_content: str) -> list[XUnitTestSuite]:
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        raise XUnitParseError("Failed to parse XUnit XML") from e

    test_suites = []

    for testsuite_el in root.findall("./testsuite"):
        test_cases = []

        arch_el = testsuite_el.find(
            "./testing-environment[@name='provisioned']/property[@name='arch']"
        )
        arch = arch_el.get("value") if arch_el is not None else None
        if arch is None:
            raise ValueError("Architecture not found in XUnit XML")

        for testcase_el in testsuite_el.findall("./testcase"):
            name = testcase_el.get("name")
            if not name:
                raise XUnitParseError("Test case without name found")

            fmf_id_el = testcase_el.find("fmf-id")
            if fmf_id_el is None:
                raise XUnitParseError(f"Test case {name} doesn't have fmf-id")
            url = fmf_id_el.get("url")
            ref = fmf_id_el.get("ref")

            if not url or not ref:
                raise XUnitParseError(f"Test case {name} has incomplete fmf-id")

            log_el = testcase_el.find("./logs/log[@name='testout.log']")
            log_url = log_el.get("href") if log_el is not None else None

            if log_url is None:
                log_url = ""

            if testcase_el.find("failure") is not None:
                result = XUnitTestCaseResult.FAIL
            elif testcase_el.find("error") is not None:
                result = XUnitTestCaseResult.ERROR
            elif testcase_el.find("skipped") is not None:
                result = XUnitTestCaseResult.SKIPPED
            else:
                result = XUnitTestCaseResult.PASS

            test_case = XUnitTestCase(
                name=name,
                url=url,
                ref=ref,
                log_url=log_url,
                result=result,
            )
            test_cases.append(test_case)

        test_suite = XUnitTestSuite(
            name=testsuite_el.get("name") or "unknown",
            arch=arch,
            test_cases=test_cases,
        )
        test_suites.append(test_suite)

    return test_suites


def compare_test_suites(
    suite_a: XUnitTestSuite, suite_b: XUnitTestSuite, output: XUnitComparison
):
    """Compare two test suites and update the output.

    Differences *from* suite_a *to* suite_b are recorded in the output.
    """

    case_map_a = {tc.name: tc for tc in suite_a.test_cases}
    case_map_b = {tc.name: tc for tc in suite_b.test_cases}

    all_keys = set(case_map_a.keys()) | set(case_map_b.keys())

    for key in all_keys:
        tc_a = case_map_a.get(key)
        tc_b = case_map_b.get(key)

        some_case = tc_a or tc_b
        assert some_case is not None

        result_a = tc_a.result if tc_a else XUnitTestCaseResult.MISSING
        result_b = tc_b.result if tc_b else XUnitTestCaseResult.MISSING

        log_url_a = tc_a.log_url if tc_a else ""
        log_url_b = tc_b.log_url if tc_b else ""

        if (
            result_a == XUnitTestCaseResult.PASS
            and result_b == XUnitTestCaseResult.PASS
        ):
            comparison_result = ComparisonResult.WORKS
        elif result_a == XUnitTestCaseResult.PASS and result_b in (
            XUnitTestCaseResult.FAIL,
            XUnitTestCaseResult.ERROR,
        ):
            comparison_result = ComparisonResult.REGRESSION
        elif (
            result_a in (XUnitTestCaseResult.FAIL, XUnitTestCaseResult.ERROR)
            and result_b == XUnitTestCaseResult.PASS
        ):
            comparison_result = ComparisonResult.FIXED
        elif result_a in (
            XUnitTestCaseResult.FAIL,
            XUnitTestCaseResult.ERROR,
        ) and result_b in (XUnitTestCaseResult.FAIL, XUnitTestCaseResult.ERROR):
            comparison_result = ComparisonResult.BROKEN
        else:
            comparison_result = ComparisonResult.DIFFERENCE

        old_count = getattr(output.total_counts, comparison_result.value, 0)
        setattr(output.total_counts, comparison_result.value, old_count + 1)

        if comparison_result != ComparisonResult.WORKS:
            comparison = XUnitTestCaseComparison(
                name=some_case.name,
                arch=suite_a.arch,
                url=some_case.url,
                ref=some_case.ref,
                result_a=result_a,
                result_b=result_b,
                log_url_a=log_url_a,
                log_url_b=log_url_b,
            )

            getattr(output, comparison_result.value).append(comparison)


async def compare_xunit_files(
    xunit_url_a: str, xunit_url_b: str, *, metadata: dict[str, str] = {}
) -> XUnitComparison:
    """
    Download and compare two XUnit files.

    Downloads XUnit files from the given URLs, compare them, and returns a
    comparison result that includes counts of different comparison results and
    details about test cases that differ.

    Note that the format of the files is Testing Farm's version of the JUnit
    XML format. While this is sometimes referred to as the xUnit format and
    is widely used across different testing frameworks, it is entirely
    different than the format from xUnit.net. "XUnit" is used here
    only for compactnesss; JUnit XML would be more accurate.

    Args:
        xunit_url_a: URL of the first XUnit file.
        xunit_url_b: URL of the second XUnit file.
        metadata: Optional metadata to include in the comparison result.

    Returns:
        The comparison result as an XUnitComparison object.

    Raises:
        HTTPError: If downloading either of the XUnit files fails.
        ValueError: If the XUnit files contain different test suites and cannot be compared.
            test suite identity is determined by (name, arch) pairs.
    """
    session = aiohttp_session()

    async def fetch_url(url: str) -> str:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.text()

    xml_content_a, xml_content_b = await asyncio.gather(
        fetch_url(xunit_url_a),
        fetch_url(xunit_url_b),
    )

    test_suites_a = parse_xunit(xml_content_a)
    test_suites_b = parse_xunit(xml_content_b)

    test_suite_a_keys = {(suite.name, suite.arch) for suite in test_suites_a}
    test_suite_b_keys = {(suite.name, suite.arch) for suite in test_suites_b}

    if test_suite_a_keys != test_suite_b_keys:
        raise ValueError(
            "XUnit files contain different test suites and cannot be compared:\n"
            + "\n".join(
                (
                    f"File A test suites: {test_suite_a_keys}",
                    f"File B test suites: {test_suite_b_keys}",
                )
            )
        )

    output: XUnitComparison = XUnitComparison(
        status=XUnitComparisonStatus(
            generated=True,  # If we return this object, comparison was successful
            reason="Comparison generated successfully",
        ),
        metadata=dict(metadata),
    )

    # Iterate over each test suite by matching (name, arch) pairs; if a test
    # suite has multiple suites with the same (name, arch), the first one
    # is arbitrarily used.
    for name, arch in test_suite_a_keys:
        suite_a = next(
            suite
            for suite in test_suites_a
            if suite.name == name and suite.arch == arch
        )
        suite_b = next(
            suite
            for suite in test_suites_b
            if suite.name == name and suite.arch == arch
        )

        compare_test_suites(suite_a, suite_b, output)

    return output


if __name__ == "__main__":
    from .http_utils import with_aiohttp_session
    import sys

    @with_aiohttp_session()
    async def main():
        result = await compare_xunit_files(sys.argv[1], sys.argv[2])
        print(result.to_toml())

    asyncio.run(main())
