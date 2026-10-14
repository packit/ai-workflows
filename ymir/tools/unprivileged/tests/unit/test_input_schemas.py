import json

from ymir.tools.unprivileged import filesystem, specfile, text


def _input_schemas(module):
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and name.endswith("Input") and hasattr(obj, "model_json_schema"):
            yield f"{module.__name__}.{name}", obj.model_json_schema()


def test_tool_input_schemas_contain_no_json_schema_format():
    """OpenAI's strict tool schemas reject non-standard formats such as pydantic's
    ``format: "path"``; path inputs must serialize as plain strings.
    """
    offenders = [
        name for module in (text, specfile, filesystem) for name, schema in _input_schemas(module)
        if '"format"' in json.dumps(schema)
    ]
    assert not offenders, f"tool input schemas emit JSON schema formats: {offenders}"
