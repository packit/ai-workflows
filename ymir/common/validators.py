from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, WithJsonSchema

# pydantic serializes ``Path`` fields with ``format: "path"``, which is not a
# standard JSON Schema format and is rejected by OpenAI's strict tool schemas
# ("'path' is not a valid format"). Pin the JSON schema to a plain string;
# pydantic validation itself is unaffected.
ToolPath = Annotated[Path, WithJsonSchema({"type": "string"})]


def is_absolute_path(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("Argument must be an absolute path")
    return value


AbsolutePath = Annotated[ToolPath, AfterValidator(is_absolute_path)]


def is_not_empty_string(value: str) -> str:
    if not value:
        raise ValueError("Argument must not be an empty string")
    return value


NonEmptyString = Annotated[str, AfterValidator(is_not_empty_string)]


def deduplicate_and_sort(value: list) -> list:
    return sorted(set(value))


UniqueSortedList = Annotated[list[str], AfterValidator(deduplicate_and_sort)]
