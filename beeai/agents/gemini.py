import os
from typing_extensions import Unpack

from beeai_framework.adapters.litellm.chat import LiteLLMChatModel
from beeai_framework.backend.chat import ChatModelKwargs
from beeai_framework.backend.constants import ProviderName


class GeminiChatModel(LiteLLMChatModel):
    @property
    def provider_id(self) -> ProviderName:
        return "gemini"

    def __init__(
        self,
        model_id: str | None = None,
        *,
        api_key: str | None = None,
        **kwargs: Unpack[ChatModelKwargs],
    ) -> None:
        super().__init__(
            model_id if model_id else os.getenv("GEMINI_CHAT_MODEL", "gemini-2.5-flash"),
            provider_id="gemini",
            **kwargs,
        )

        self._assert_setting_value("api_key", api_key, envs=["GOOGLE_API_KEY"])
