"""Ymir-side glue for running Amalgamator as a patch writer.

``adapter`` converts Ymir values into Amalgamator's case file and reads its
artifacts; ``inputs`` derives those values from what Ymir already has on disk.
Both modules are usable on their own, without an agent workflow.
"""

from ymir.agents.amalgamator.adapter import AmalgamatorResult, build_case_config, run_case

__all__ = ["AmalgamatorResult", "build_case_config", "run_case"]
