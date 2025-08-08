import os

import pytest

from beeai_framework.agents.experimental.utils._tool import FinalAnswerTool
from beeai_framework.tools.think import ThinkTool
from beeai_framework.utils.strings import to_json

from deepeval import assert_test
from deepeval.dataset import Golden
from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams, ToolCall

from base_agent import BaseAgent
from observability import setup_observability
from triage_agent import TriageAgent, InputSchema, OutputSchema, Resolution, NoActionData

from model import DeepEvalLLM
from _utils import create_dataset, evaluate_dataset


async def run_agent(agent: BaseAgent, test_case: LLMTestCase) -> None:
    await agent.run_with_schema(
        agent.input_schema.model_validate_json(test_case.input), capture_raw_response=True
    )
    response = agent.last_raw_response
    test_case.tools_called = []
    test_case.actual_output = response.answer.text
    for index, step in enumerate(response.state.steps):
        if not step.tool:
            continue
        prev_step = response.state.steps[index - 1] if index > 0 else None
        test_case.tools_called = [
            ToolCall(
                name=step.tool.name,
                description=step.tool.description,
                input_parameters=step.input,
                output=step.output.get_text_content(),
                reasoning=(
                    to_json(prev_step.input, indent=2, sort_keys=False)
                    if prev_step and isinstance(prev_step.tool, ThinkTool)
                    else None
                ),
            )
            for step in response.state.steps
            if step.tool and not isinstance(step.tool, FinalAnswerTool)
        ]


@pytest.mark.asyncio
async def test_triage():
    setup_observability(os.getenv("COLLECTOR_ENDPOINT"))

    dataset = await create_dataset(
        name="Triage",
        agent_factory=lambda: TriageAgent(),
        agent_run=run_agent,
        goldens=[
            Golden(
                input=InputSchema(issue="RHEL-12345").model_dump_json(),
                expected_output=OutputSchema(
                    resolution=Resolution.NO_ACTION,
                    data=NoActionData(reasoning="The issue is not a fixable bug", jira_issue="RHEL-12345"),
                ).model_dump_json(),
                expected_tools=[
                    # ToolCall(
                    #    name="get_jira_details",
                    #    reasoning="TODO",
                    #    input={"issue_key": "RHEL-12345"},
                    #    output="TODO",
                    # ),
                ],
            )
        ],
    )

    correctness_metric = GEval(
        name="Correctness",
        criteria="\n - ".join(
            [
                "Reasoning must be factually equal to the expected one",
                "`jira_issue` in the output must match `issue` in the input",
            ]
        ),
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.EXPECTED_OUTPUT,
            LLMTestCaseParams.TOOLS_CALLED,
            LLMTestCaseParams.EXPECTED_TOOLS,
        ],
        verbose_mode=True,
        model=DeepEvalLLM.from_name(os.getenv("CHAT_MODEL")),
        threshold=0.65,
    )
    metrics: list[BaseMetric] = [correctness_metric]
    evaluate_dataset(dataset, metrics)
