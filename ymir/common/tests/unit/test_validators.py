from pathlib import Path

import pytest
from pydantic import BaseModel, Field, ValidationError

from ymir.common.validators import AbsolutePath, ToolPath


class _Paths(BaseModel):
    absolute: AbsolutePath = Field(description="an absolute path")
    any_path: ToolPath = Field(description="any path")


def test_path_fields_emit_a_plain_string_json_schema():
    """pydantic's format:"path" is rejected by OpenAI strict tool schemas."""
    properties = _Paths.model_json_schema()["properties"]

    for name in ("absolute", "any_path"):
        assert properties[name]["type"] == "string"
        assert "format" not in properties[name]

    # the field description is preserved
    assert properties["absolute"]["description"] == "an absolute path"


def test_absolute_path_still_requires_an_absolute_path():
    with pytest.raises(ValidationError):
        _Paths(absolute="relative/path", any_path="/tmp/x")


def test_tool_path_accepts_relative_and_absolute():
    model = _Paths(absolute="/tmp/x", any_path="relative/path")
    assert model.absolute == Path("/tmp/x")
    assert model.any_path == Path("relative/path")
