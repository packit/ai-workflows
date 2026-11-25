"""Testcase outcome helpers shared by the JUnit results.xml readers (Testing Farm tools, MR testing agent)."""

import xml.etree.ElementTree as ET

MAX_FAILURE_MESSAGE_CHARS = 300

_RESULT_STATUS = {
    "passed": "passed",
    "pass": "passed",
    "failed": "failed",
    "fail": "failed",
    "error": "error",
    "skipped": "skipped",
    "skip": "skipped",
}
_CHILD_STATUS = {"failure": "failed", "error": "error", "skipped": "skipped"}


def testcase_status(case: ET.Element) -> tuple[str, str]:
    """Return a testcase's status and, for a failure, error, or skip, a short message.

    Testing Farm's JUnit files (results-junit.xml) mark outcomes with a child element and
    carry no ``result`` attribute, while results.xml carries ``result`` on each testcase.
    Child elements win, so both formats resolve the same way."""
    for tag in ("failure", "error", "skipped"):
        if (element := case.find(tag)) is not None:
            message = (element.get("message") or element.text or "").strip().replace("\n", " ")
            return _CHILD_STATUS[tag], message[:MAX_FAILURE_MESSAGE_CHARS]
    return _RESULT_STATUS.get(case.get("result") or "", "passed"), ""
