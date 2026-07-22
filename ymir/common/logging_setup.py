"""Shared logging configuration for ymir entry points."""

import logging
import sys
import threading
from collections.abc import Iterable
from contextvars import ContextVar

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s:%(jira_issue)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

current_jira_issue: ContextVar[str | None] = ContextVar("current_jira_issue", default=None)
current_workflow: ContextVar[str | None] = ContextVar("current_workflow", default=None)

_buffered_handler: "BufferedTaskHandler | None" = None


class _JiraFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        issue = current_jira_issue.get()
        record.jira_issue = f" [{issue}]" if issue else ""
        return super().format(record)


class BufferedTaskHandler(logging.Handler):
    """Handler that buffers formatted log lines per Jira issue.

    Lines without a Jira issue context pass through immediately.
    Lines with context are buffered per-issue and flushed when the buffer
    reaches `buffer_size` or when `flush_task` is called explicitly.

    Multi-line messages can be kept together by wrapping the corresponding
    log calls between `begin_group()` and `end_group()`.  While a group is
    open the size threshold is deferred so that a multi-line message is
    never split across two flushes.
    """

    def __init__(self, buffer_size: int = 50) -> None:
        super().__init__()
        self._buffer_size = buffer_size
        self._buffers: dict[str, list[str]] = {}
        self._lock = threading.Lock()
        self._group_depth: ContextVar[int] = ContextVar("group_depth", default=0)

    def begin_group(self) -> None:
        self._group_depth.set(self._group_depth.get() + 1)

    def end_group(self) -> None:
        depth = self._group_depth.get() - 1
        self._group_depth.set(depth)
        if depth == 0 and (issue := current_jira_issue.get()):
            with self._lock:
                if len(self._buffers.get(issue, [])) >= self._buffer_size:
                    self._flush_issue(issue)

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        issue = current_jira_issue.get()
        if issue is None:
            with self._lock:
                sys.stdout.write(msg + "\n")
                sys.stdout.flush()
            return
        with self._lock:
            buf = self._buffers.setdefault(issue, [])
            buf.append(msg)
            if self._group_depth.get() == 0 and len(buf) >= self._buffer_size:
                self._flush_issue(issue)

    def flush_task(self, issue: str) -> None:
        with self._lock:
            self._flush_issue(issue)

    def flush(self) -> None:
        with self._lock:
            for issue in list(self._buffers):
                self._flush_issue(issue)
        super().flush()

    def close(self) -> None:
        self.flush()
        super().close()

    def _flush_issue(self, issue: str) -> None:
        buf = self._buffers.pop(issue, None)
        if buf:
            sys.stdout.write("\n".join(buf) + "\n")
            sys.stdout.flush()


class _LogWriteable:
    """Adapter routing `write()` calls through Python logging.

    Satisfies BeeAI's `Writeable` protocol so it can be used as the
    `target` for `GlobalTrajectoryMiddleware`.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger("agent.trajectory")

    def write(self, s: str) -> int:
        lines = [line for line in s.splitlines() if line]
        if not lines:
            return len(s)
        if len(lines) > 1 and _buffered_handler is not None:
            _buffered_handler.begin_group()
        try:
            for line in lines:
                self._logger.info("%s", line)
        finally:
            if len(lines) > 1 and _buffered_handler is not None:
                _buffered_handler.end_group()
        return len(s)


def get_trajectory_writeable() -> _LogWriteable:
    return _LogWriteable()


def flush_task_logs(issue: str) -> None:
    if _buffered_handler is not None:
        _buffered_handler.flush_task(issue)


def configure_logging(
    level: int = logging.INFO,
    extra_handlers: Iterable[logging.Handler] | None = None,
    buffer_size: int = 0,
) -> None:
    """Configure the root logger with timestamps and short logger names.

    Replaces any handlers already attached to the root logger so repeated
    calls (e.g. across tests) produce a consistent format.

    When `buffer_size` > 0, log lines emitted inside a task context
    (`current_jira_issue` set) are buffered per issue and flushed in
    contiguous batches of up to `buffer_size` lines.
    """
    global _buffered_handler

    formatter = _JiraFormatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    if buffer_size > 0:
        _buffered_handler = BufferedTaskHandler(buffer_size=buffer_size)
        handlers: list[logging.Handler] = [_buffered_handler]
    else:
        _buffered_handler = None
        handlers = [logging.StreamHandler(sys.stdout)]

    if extra_handlers:
        handlers.extend(extra_handlers)
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        root.addHandler(handler)
