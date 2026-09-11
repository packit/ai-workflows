from beeai_framework.tools import ToolError

from ymir.tools.base import tool_error_context
from ymir.tools.errors import ToolErrorWithContext, explain_to_llm

SECRET = "glpat-0123456789ABCDEFGHIJ"  # pragma: allowlist secret


def _wrap(error_message: str, raised: Exception, **kwargs) -> ToolErrorWithContext:
    """Produce a ToolErrorWithContext exactly like a tool would via the context manager."""
    try:
        with tool_error_context(error_message, **kwargs):
            raise raised
    except ToolErrorWithContext as e:
        return e
    raise AssertionError("tool_error_context did not wrap the exception")


def test_excludes_raw_cause():
    """The raw cause (not opted-in) must not reach the LLM, even unredacted."""
    err = _wrap("Failed to reach service", RuntimeError(f"secret={SECRET} leaked"))

    reason = explain_to_llm(err)

    assert reason == "ToolErrorWithContext: Failed to reach service"
    assert SECRET not in reason


def test_excludes_additional_context():
    """Observability context must not reach the LLM."""
    err = _wrap("Failed", RuntimeError("boom"), url="https://internal.example.com/admin")

    reason = explain_to_llm(err)

    assert reason == "ToolErrorWithContext: Failed"
    assert "internal.example.com" not in reason
    assert "additional_context" not in reason


def test_wrapped_toolerror_message_reaches_llm():
    """A ToolError raised inside the context is forwarded to the LLM via the error chain."""
    err = _wrap("Failed to clone repository", ToolError("clone_path must be under /git-repos"))

    reason = explain_to_llm(err)

    assert "ToolErrorWithContext: Failed to clone repository" in reason
    assert "ToolError: clone_path must be under /git-repos" in reason


def test_name_context_key_is_rendered_others_hidden():
    """The allowlisted tool name is shown; non-allowlisted context keys stay hidden."""
    err = ToolErrorWithContext("boom", context={"name": "some_tool", "other": "hidden_value"})

    reason = explain_to_llm(err)

    assert "some_tool" in reason
    assert "hidden_value" not in reason
    assert "other" not in reason


def test_non_framework_error_renders_type_only():
    """A non-FrameworkError must never render its (unredacted) message."""
    reason = explain_to_llm(ValueError(f"plain secret {SECRET}"))

    assert reason == "ValueError"
    assert SECRET not in reason


def test_toolerror_without_context_message_is_preserved():
    """Detail baked into a plain ToolError message (unprivileged-tool pattern) survives."""
    err = ToolError("Command git-add failed: pathspec did not match")

    reason = explain_to_llm(err)

    assert reason == "ToolError: Command git-add failed: pathspec did not match"
