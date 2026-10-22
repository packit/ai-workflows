from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator


def is_absolute_path(value: Path) -> Path:
    """Return an absolute path unchanged, raising ``ValueError`` otherwise."""
    if not value.is_absolute():
        raise ValueError("Argument must be an absolute path")
    return value


AbsolutePath = Annotated[Path, AfterValidator(is_absolute_path)]


def is_not_empty_string(value: str) -> str:
    """Return a nonempty string unchanged, raising ``ValueError`` for ``''``."""
    if not value:
        raise ValueError("Argument must not be an empty string")
    return value


NonEmptyString = Annotated[str, AfterValidator(is_not_empty_string)]


def deduplicate_and_sort(value: list) -> list:
    """Return the unique input values in sorted order.

    Values must be hashable and mutually orderable.
    """
    return sorted(set(value))


UniqueSortedList = Annotated[list[str], AfterValidator(deduplicate_and_sort)]
