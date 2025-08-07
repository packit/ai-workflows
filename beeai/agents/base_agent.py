import os
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

from beeai_framework.agents import AgentExecutionConfig
from beeai_framework.agents.experimental import RequirementAgent, RequirementAgentRunOutput
from beeai_framework.template import PromptTemplate, PromptTemplateInput


TInputSchema = TypeVar("TInputSchema", bound=BaseModel)
TOutputSchema = TypeVar("TOutputSchema", bound=BaseModel)


class BaseAgent(RequirementAgent, ABC):
    last_raw_response: RequirementAgentRunOutput | None = None

    @property
    @abstractmethod
    def input_schema(self) -> type[TInputSchema]: ...

    @property
    @abstractmethod
    def output_schema(self) -> type[TOutputSchema]: ...

    @property
    @abstractmethod
    def prompt(self) -> str: ...

    def _render_prompt(self, input: TInputSchema) -> str:
        template = PromptTemplate(
            PromptTemplateInput(schema=self.input_schema, template=self.prompt)
        )
        return template.render(input)

    async def _run_with_schema(
        self, input: TInputSchema, capture_raw_response: bool = False
    ) -> TOutputSchema:
        max_retries_per_step = int(os.getenv("BEEAI_MAX_RETRIES_PER_STEP", 5))
        total_max_retries = int(os.getenv("BEEAI_TOTAL_MAX_RETRIES", 10))
        max_iterations = int(os.getenv("BEEAI_MAX_ITERATIONS", 100))

        response = await self.run(
            prompt=self._render_prompt(input),
            expected_output=self.output_schema,
            execution=AgentExecutionConfig(
                max_retries_per_step=max_retries_per_step,
                total_max_retries=total_max_retries,
                max_iterations=max_iterations,
            ),
        )
        if capture_raw_response:
            self.last_raw_response = response
        return self.output_schema.model_validate_json(response.result.text)

    async def run_with_schema(
        self, input: TInputSchema, capture_raw_response: bool = False
    ) -> TOutputSchema:
        return await self._run_with_schema(input, capture_raw_response)


if os.getenv("LITELLM_DEBUG"):
    # the following two modules call `litellm_debug(False)` on import
    # import them explicitly now to ensure our call to `litellm_debug()` is not negated later
    import beeai_framework.adapters.litellm.chat
    import beeai_framework.adapters.litellm.embedding
    from beeai_framework.adapters.litellm.utils import litellm_debug

    litellm_debug(True)
