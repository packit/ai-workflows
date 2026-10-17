from types import SimpleNamespace

import pytest

from ymir.agents import utils


@pytest.mark.parametrize(
    (
        "chat_model",
        "litellm_provider_id",
        "beeai_default_cache_setting",
        "ymir_expected_cache_setting",
    ),
    [
        ("vertexai:claude-opus-4-6", "vertexai", False, True),
        ("vertexai:claude-sonnet-4-6", "vertexai", False, True),
        ("vertexai:gemini-2.5-pro", "vertexai", False, False),
        ("anthropic:claude-opus-4-6", "anthropic", True, True),
    ],
)
def test_get_chat_model_only_enables_prompt_caching_for_vertex_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    chat_model: str,
    litellm_provider_id: str,
    beeai_default_cache_setting: bool,
    ymir_expected_cache_setting: bool,
) -> None:
    """Preserve caching for Vertex Anthropic after BeeAI 0.1.84 disables it
    by default for every VertexAIChatModel.

    The mock model simulates the BeeAI default without Vertex credentials.
    """
    captured: dict = {}
    mock_model = SimpleNamespace(
        _litellm_provider_id=litellm_provider_id,
        allow_prompt_caching=beeai_default_cache_setting,
    )

    def from_name(*args, **kwargs):
        captured.update(kwargs)
        return mock_model

    monkeypatch.setenv("CHAT_MODEL", chat_model)
    monkeypatch.setattr(utils.ChatModel, "from_name", from_name)

    assert utils.get_chat_model() is mock_model
    assert "allow_prompt_caching" not in captured
    assert mock_model.allow_prompt_caching is ymir_expected_cache_setting
