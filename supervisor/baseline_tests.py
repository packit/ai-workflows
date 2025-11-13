from dataclasses import dataclass
from functools import cached_property
import logging
import re

from supervisor.compare_xunit import (
    XUnitComparison,
    XUnitComparisonStatus,
    compare_xunit_files,
)
from supervisor.jira_utils import add_issue_attachments


from .supervisor_types import FullIssue, TestingFarmRequest, TestingFarmRequestState
from .testing_farm_utils import (
    TESTING_FARM_URL,
    testing_farm_get_request,
    testing_farm_reproduce_request_with_build,
)

logger = logging.getLogger(__name__)


class RequestWrapper:
    """
    Wrapper around TestingFarmRequest or request ID string

    We start off with either a TestingFarmRequest object or a string ID, and
    lazily fetch the TestingFarmRequest object only when needed. This
    also has convenience properties for ID, URL, and JIRA-formatted link.
    """

    def __init__(self, request: str | TestingFarmRequest):
        self._request = request

    @cached_property
    def request(self) -> TestingFarmRequest:
        if isinstance(self._request, TestingFarmRequest):
            return self._request
        else:
            return testing_farm_get_request(self._request)

    @property
    def id(self) -> str:
        if isinstance(self._request, TestingFarmRequest):
            return self._request.id
        else:
            return self._request

    @property
    def url(self) -> str:
        return f"{TESTING_FARM_URL}/requests/{self.id}"

    @property
    def link(self) -> str:
        return f"[{self.id}|{self.url}]"


class BaselineComparison:
    def __init__(
        self, failed: str | TestingFarmRequest, baseline: str | TestingFarmRequest
    ):
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

    def settled(self) -> bool:
        return all(
            comparison.baseline.request.state
            in (
                TestingFarmRequestState.COMPLETE,
                TestingFarmRequestState.ERROR,
                TestingFarmRequestState.CANCELED,
            )
            for comparison in self.comparisons
        )

    def complete(self) -> bool:
        return all(
            comparison.baseline.request.state == TestingFarmRequestState.COMPLETE
            for comparison in self.comparisons
        )

    def format_issue_comment(self, *, include_attachments: bool = False) -> str:
        if self.complete():
            message = "Reproduced"
            state_header = "Result"
        elif self.settled():
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
                    f"|{comparison.baseline.request.result
                        if comparison.baseline.request.state == TestingFarmRequestState.COMPLETE
                        else comparison.baseline.request.state}"
                    f"{('|' + comparison.attachment_link) if include_attachments else ''}"
                    f"|"
                    for comparison in self.comparisons
                )
            )
        )

    async def create_attachments(self, issue_key: str, dry_run: bool = False) -> None:
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

            def create_not_generated_comparison(reason: str) -> XUnitComparison:
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
                    comparison_result = create_not_generated_comparison(
                        "XUnit results missing for run A"
                    )
                case (_, None):
                    comparison_result = create_not_generated_comparison(
                        "XUnit results missing for run B"
                    )
                case _:
                    comparison_result = await compare_xunit_files(
                        baseline_request.result_xunit_url,
                        failed_request.result_xunit_url,
                        metadata=metadata,
                    )

            attachment_bytes = comparison_result.to_toml().encode("utf-8")
            logger.info("About to attach %s", attachment_bytes.decode("utf-8"))
            attachments.append(
                (comparison.attachment_name, attachment_bytes, "text/plain")
            )

        add_issue_attachments(issue_key, attachments, dry_run=dry_run)

    @staticmethod
    def create(
        failure_comment: str,
        failed_request_ids: list[str],
        previous_build_nvr: str,
        dry_run: bool = False,
    ) -> "BaselineTests":
        tests: list[BaselineComparison] = []

        for failed_request in failed_request_ids:
            failed_request = testing_farm_get_request(failed_request)

            logger.info(
                "Starting reproduction with previous build %s for failed test run %s",
                previous_build_nvr,
                failed_request,
            )

            try:
                baseline_request = testing_farm_reproduce_request_with_build(
                    request=failed_request,
                    build_nvr=previous_build_nvr,
                    dry_run=dry_run,
                )
                tests.append(
                    BaselineComparison(failed=failed_request, baseline=baseline_request)
                )
            except Exception as e:
                raise RuntimeError(
                    f"Failed to start reproduction of test run {failed_request} with previous build {previous_build_nvr}: {e}",
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
                        BaselineComparison(
                            failed=failed_request_id, baseline=baseline_request_id
                        )
                    )
            return BaselineTests(
                failure_comment="\n".join(leading_lines).strip(),
                comparisons=comparisons,
                previous_build_nvr=previous_build_nvr,
                comment_id=comment.id,
            )

        return None
