from types import SimpleNamespace

import pytest

from ymir.agents import utils


@pytest.mark.parametrize(
    ("chat_model", "expected_caching_kwarg"),
    [
        # Vertex Anthropic: we must explicitly request caching at construction,
        # because BeeAI 0.1.84+ defaults allow_prompt_caching=False for Vertex.
        ("vertexai:claude-opus-4-6", True),
        ("vertexai:claude-sonnet-4-6", True),
        # Vertex Gemini: not requested (caching is also forced off below).
        ("vertexai:gemini-2.5-pro", None),
        # Direct Anthropic: not requested — BeeAI enables caching here by default.
        ("anthropic:claude-opus-4-6", None),
    ],
)
def test_get_chat_model_requests_caching_only_for_vertex_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    chat_model: str,
    expected_caching_kwarg: bool | None,
) -> None:
    """get_chat_model must pass allow_prompt_caching=True to ChatModel.from_name
    for Vertex Anthropic models and leave every other provider at its BeeAI
    default (no explicit override)."""
    captured: dict = {}

    def from_name(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(allow_prompt_caching=None, tool_choice_support=None)

    monkeypatch.setenv("CHAT_MODEL", chat_model)
    monkeypatch.setattr(utils.ChatModel, "from_name", from_name)

    utils.get_chat_model()

    assert captured.get("allow_prompt_caching") is expected_caching_kwarg


def test_vertex_anthropic_request_retains_cache_injection_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end guard against the Oct-2026 caching regression: a real Vertex
    Claude model built by get_chat_model must forward cache_control_injection_points
    through request transformation, and drop them only when caching is disabled.

    This exercises the real VertexAIChatModel adapter — whose 0.1.84+ default of
    allow_prompt_caching=False silently strips the points — so it catches the bug
    regardless of how the provider id is spelled or where the flag is read.
    """
    from beeai_framework.backend.message import UserMessage
    from beeai_framework.backend.types import ChatModelInput

    # VertexAIChatModel construction requires project/location; credentials may
    # be empty (no network call is made during construction or transformation).
    monkeypatch.setenv("CHAT_MODEL", "vertexai:claude-opus-4-6")
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_VERTEX_LOCATION", "us-east5")
    monkeypatch.setenv("GOOGLE_VERTEX_CREDENTIALS", "")

    model = utils.get_chat_model()
    assert model.allow_prompt_caching is True

    points = [{"location": "message", "index": 0}]
    model._settings["cache_control_injection_points"] = points

    def transform() -> dict:
        return model._transform_input(ChatModelInput(messages=[UserMessage("hi")]))

    assert transform().get("cache_control_injection_points") == points

    # Confirm the flag is what controls it: with caching off BeeAI strips them.
    model.allow_prompt_caching = False
    assert "cache_control_injection_points" not in transform()
