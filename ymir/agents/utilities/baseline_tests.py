import logging
import re
from dataclasses import dataclass
from functools import cached_property

from ymir.agents.utilities.compare_xunit import (
    XUnitComparison,
    XUnitComparisonStatus,
    compare_xunit_files,
)
from ymir.common.models import (
    FullIssue,
    TestingFarmRequest,
    TestingFarmRequestState,
)
from ymir.common.utils import run_tool

logger = logging.getLogger(__name__)

TESTING_FARM_URL = "https://api.testing-farm.io"


class RequestWrapper:
    """
    Wrapper around TestingFarmRequest or request ID string

    We start off with either a TestingFarmRequest object or a string ID, and
    lazily fetch the TestingFarmRequest object only when needed. This
    also has convenience properties for ID, URL, and JIRA-formatted link.
    """

    def __init__(self, request: str | TestingFarmRequest):
        self._request = request

    async def async_request(self, tools: list) -> TestingFarmRequest:
        if isinstance(self._request, TestingFarmRequest):
            return self._request
        data = await run_tool(
            "get_testing_farm_request",
            available_tools=tools,
            request_id=self._request,
        )
        self._request = TestingFarmRequest(**data)
        return self._request

    @cached_property
    def request(self) -> TestingFarmRequest:
        if isinstance(self._request, TestingFarmRequest):
            return self._request
        raise RuntimeError("Request not yet fetched; call async_request() first")

    @property
    def id(self) -> str:
        if isinstance(self._request, TestingFarmRequest):
            return self._request.id
        return self._request

    @property
    def url(self) -> str:
        return f"{TESTING_FARM_URL}/requests/{self.id}"

    @property
    def link(self) -> str:
        return f"[{self.id}|{self.url}]"


class BaselineComparison:
    def __init__(self, failed: str | TestingFarmRequest, baseline: str | TestingFarmRequest):
        self.failed = RequestWrapper(failed)
        self.baseline = RequestWrapper(baseline)

    @property
    def attachment_name(self) -> str:
        return f"comparison-{self.baseline.id}--{self.failed.id}.toml"

    @property
    def attachment_link(self) -> str:
        return f"[compare|^{self.attachment_name}]"


@dataclass(kw_only=True)
class BaselineTests:
    failure_comment: str
    comparisons: list[BaselineComparison]
    previous_build_nvr: str
    comment_id: str | None = None

    async def settled(self, tools: list) -> bool:
        for comparison in self.comparisons:
            request = await comparison.baseline.async_request(tools)
            if request.state not in (
                TestingFarmRequestState.COMPLETE,
                TestingFarmRequestState.ERROR,
                TestingFarmRequestState.CANCELED,
            ):
                return False
        return True

    async def complete(self, tools: list) -> bool:
        for comparison in self.comparisons:
            request = await comparison.baseline.async_request(tools)
            if request.state != TestingFarmRequestState.COMPLETE:
                return False
        return True

    @staticmethod
    def _request_outcome(request: TestingFarmRequest) -> str:
        if request.state == TestingFarmRequestState.COMPLETE:
            return request.result
        return request.state

    async def format_issue_comment(
        self, *, include_attachments: bool = False, tools: list | None = None
    ) -> str:
        if tools:
            is_complete = await self.complete(tools)
            is_settled = await self.settled(tools)
        else:
            # Fallback for when requests are already loaded
            is_complete = all(
                isinstance(c.baseline._request, TestingFarmRequest)
                and c.baseline._request.state == TestingFarmRequestState.COMPLETE
                for c in self.comparisons
            )
            is_settled = all(
                isinstance(c.baseline._request, TestingFarmRequest)
                and c.baseline._request.state
                in (
                    TestingFarmRequestState.COMPLETE,
                    TestingFarmRequestState.ERROR,
                    TestingFarmRequestState.CANCELED,
                )
                for c in self.comparisons
            )

        if is_complete:
            message = "Reproduced"
            state_header = "Result"
        elif is_settled:
            message = "Failed to reproduce"
            state_header = "Result"
        else:
            message = "Reproducing"
            state_header = "State"

        return (
            self.failure_comment
            + "\n\n"
            + (
                f"{message} failed tests with previous build {self.previous_build_nvr}:\n"
                f"||Architecture||Original Request||Request With Old Build||{state_header}"
                f"{'||Comparison' if include_attachments else ''}"
                "||\n"
                + "\n".join(
                    f"|{', '.join(comparison.failed.request.arches)}"
                    f"|{comparison.failed.link}"
                    f"|{comparison.baseline.link}"
                    f"|{self._request_outcome(comparison.baseline.request)}"
                    f"{('|' + comparison.attachment_link) if include_attachments else ''}"
                    f"|"
                    for comparison in self.comparisons
                )
            )
        )

    async def create_attachments(
        self, issue_key: str, dry_run: bool = False, tools: list | None = None
    ) -> None:
        attachments: list[tuple[str, bytes, str]] = []
        for comparison in self.comparisons:
            failed_request = comparison.failed.request
            baseline_request = comparison.baseline.request

            metadata = {}

            metadata |= {
                "build_a": baseline_request.build_nvr,
                "testing_farm_request_id_a": baseline_request.id,
            }
            if baseline_request.error_reason:
                metadata["error_reason_a"] = baseline_request.error_reason

            metadata |= {
                "build_b": failed_request.build_nvr,
                "testing_farm_request_id_b": failed_request.id,
            }
            if failed_request.error_reason:
                metadata["error_reason_b"] = failed_request.error_reason

            def create_not_generated_comparison(reason: str, metadata=metadata) -> XUnitComparison:
                return XUnitComparison(
                    status=XUnitComparisonStatus(
                        generated=False,
                        reason=reason,
                    ),
                    metadata=metadata,
                )

            match (baseline_request.result_xunit_url, failed_request.result_xunit_url):
                case (None, None):
                    comparison_result = create_not_generated_comparison(
                        "XUnit results missing for runs A and B"
                    )
                case (None, _):
                    comparison_result = create_not_generated_comparison("XUnit results missing for run A")
                case (_, None):
                    comparison_result = create_not_generated_comparison("XUnit results missing for run B")
                case _:
                    comparison_result = await compare_xunit_files(
                        baseline_request.result_xunit_url,
                        failed_request.result_xunit_url,
                        metadata=metadata,
                    )

            attachment_bytes = comparison_result.to_toml().encode("utf-8")
            logger.info("About to attach %s", attachment_bytes.decode("utf-8"))
            attachments.append((comparison.attachment_name, attachment_bytes, "text/plain"))

        if tools:
            await run_tool(
                "add_jira_attachments",
                available_tools=tools,
                issue_key=issue_key,
                attachments=[
                    {"filename": name, "content": content.decode("utf-8")} for name, content, _ in attachments
                ],
            )
        else:
            logger.warning("No tools available, cannot upload attachments for %s", issue_key)

    @staticmethod
    async def create(
        failure_comment: str,
        failed_request_ids: list[str],
        previous_build_nvr: str,
        dry_run: bool = False,
        tools: list | None = None,
    ) -> "BaselineTests":
        tests: list[BaselineComparison] = []

        for failed_request_id in failed_request_ids:
            if tools:
                failed_data = await run_tool(
                    "get_testing_farm_request",
                    available_tools=tools,
                    request_id=failed_request_id,
                )
                failed_request = TestingFarmRequest(**failed_data)
            else:
                raise RuntimeError("Tools required for creating baseline tests")

            logger.info(
                "Starting reproduction with previous build %s for failed test run %s",
                previous_build_nvr,
                failed_request.id,
            )

            try:
                if tools:
                    baseline_data = await run_tool(
                        "reproduce_testing_farm_request",
                        available_tools=tools,
                        request_id=failed_request.id,
                        build_nvr=previous_build_nvr,
                    )
                    baseline_request = TestingFarmRequest(**baseline_data)
                else:
                    raise RuntimeError("Tools required for creating baseline tests")

                tests.append(BaselineComparison(failed=failed_request, baseline=baseline_request))
            except Exception as e:
                raise RuntimeError(
                    f"Failed to start reproduction of test run {failed_request.id} "
                    f"with previous build {previous_build_nvr}: {e}",
                ) from e

        return BaselineTests(
            failure_comment=failure_comment,
            comparisons=tests,
            previous_build_nvr=previous_build_nvr,
        )

    @staticmethod
    def load_from_issue(issue: FullIssue) -> "BaselineTests | None":
        for comment in reversed(issue.comments):
            lines = comment.body.splitlines()
            leading_lines = []

            line_iter = iter(lines)
            for line in line_iter:
                if m := re.match(
                    ".*failed tests with previous build (.*):",
                    line,
                ):
                    previous_build_nvr = m.group(1)
                    break

                leading_lines.append(line)
            else:
                continue

            for line in line_iter:
                if line.startswith("||"):
                    break
            else:
                continue

            comparisons: list[BaselineComparison] = []
            for line in line_iter:
                if not line.startswith("|"):
                    break

                line = re.sub(r"\[([^|]+)\|([^]]+)\]", r"\1", line)  # Remove links
                parts = line.split("|")
                # parts should be ["", "<arch>", "<failed test id>", "<baseline test id>", ...]
                if len(parts) >= 4:
                    failed_request_id = parts[2].strip()
                    baseline_request_id = parts[3].strip()
                    comparisons.append(
                        BaselineComparison(failed=failed_request_id, baseline=baseline_request_id)
                    )
            return BaselineTests(
                failure_comment="\n".join(leading_lines).strip(),
                comparisons=comparisons,
                previous_build_nvr=previous_build_nvr,
                comment_id=comment.id,
            )

        return None
