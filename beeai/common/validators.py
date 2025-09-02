from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator


def is_absolute_path(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("Argument must be an absolute path")
    return value

AbsolutePath = Annotated[Path, AfterValidator(is_absolute_path)]


def is_not_empty_string(value: str) -> str:
    if not value:
        raise ValueError("Argument must not be an empty string")
    return value

NonEmptyString = Annotated[str, AfterValidator(is_not_empty_string)]


def is_list_of_two_ints(value: list[int]) -> list[int]:
    if len(value) != 2:
        raise ValueError("Argument must be a list of two integers")
    return value

Range = Annotated[list[int], AfterValidator(is_list_of_two_ints)]
