#!/usr/bin/env python3
"""Select the local build backend, refresh its token, then run Compose."""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


def backend_from_dotenv() -> str:
    """Read only BUILD_BACKEND; never execute or load the rest of .env."""
    dotenv = Path(".env")
    if not dotenv.is_file():
        return ""

    backend = ""
    for line in dotenv.read_text().splitlines():
        match = re.match(r"^\s*(?:export\s+)?BUILD_BACKEND\s*=(.*)$", line)
        if match is None:
            continue
        try:
            values = shlex.split(match.group(1), comments=True)
        except ValueError as exc:
            raise ValueError("Invalid BUILD_BACKEND in .env") from exc
        if len(values) > 1:
            raise ValueError("BUILD_BACKEND in .env must be a single value")
        backend = values[0] if values else ""
    return backend


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: run-compose-with-backend.py COMPOSE_COMMAND [ARGS...]")

    selected = os.environ.get("BUILD_BACKEND")
    if selected is None:
        selected = backend_from_dotenv()
    backend = selected.strip().lower() or "konflux"
    if backend not in {"konflux", "copr"}:
        raise SystemExit(f"Unsupported BUILD_BACKEND: {selected!r}")

    os.environ["BUILD_BACKEND"] = backend
    if backend == "konflux":
        token_script = Path(__file__).resolve().with_name("refresh-konflux-token.sh")
        subprocess.run([str(token_script)], check=True)

    # Compose replaces this process so signals and its exit status pass through.
    os.execvp(sys.argv[1], sys.argv[1:])  # noqa: S606


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
