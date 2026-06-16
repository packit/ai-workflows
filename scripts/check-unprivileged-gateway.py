#!/usr/bin/env python3
"""Pre-push hook: ensure unprivileged MCP gateway registers all tools.

Scans ymir/tools/unprivileged/ for Tool subclasses and checks that each
one is imported and instantiated in the unprivileged gateway module.

Set SKIP_GATEWAY_CHECK=1 to bypass this check.
"""

import ast
import os
import sys
from pathlib import Path

TOOLS_DIR = Path("ymir/tools/unprivileged")
GATEWAY_FILE = TOOLS_DIR / "gateway.py"
EXCLUDE_DIRS = {"tests", "__pycache__"}


def find_tool_classes(directory: Path) -> dict[str, Path] | None:
    """Find all Tool subclasses defined in the unprivileged tools directory.

    Returns None if any file cannot be parsed.
    """
    tools: dict[str, Path] = {}
    for py_file in sorted(directory.rglob("*.py")):
        if any(part in EXCLUDE_DIRS for part in py_file.parts):
            continue
        if py_file == GATEWAY_FILE:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except (SyntaxError, OSError) as e:
            print(f"Failed to parse {py_file}: {e}", file=sys.stderr)
            return None
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not node.bases:
                continue
            for base in node.bases:
                base_name = None
                if isinstance(base, ast.Subscript):
                    if isinstance(base.value, ast.Name):
                        base_name = base.value.id
                    elif isinstance(base.value, ast.Attribute):
                        base_name = base.value.attr
                elif isinstance(base, ast.Name):
                    base_name = base.id
                if base_name in ("Tool", "CloneableTool"):
                    tools[node.name] = py_file
    return tools


def find_registered_classes(gateway_file: Path) -> set[str]:
    """Find tool classes instantiated inside the gateway's register_many() call."""
    tree = ast.parse(gateway_file.read_text(encoding="utf-8"))
    registered: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_many"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            continue
        for elt in node.args[0].elts:
            if isinstance(elt, ast.Call) and isinstance(elt.func, ast.Name):
                registered.add(elt.func.id)
    return registered


def main() -> int:
    if os.environ.get("SKIP_GATEWAY_CHECK"):
        return 0

    if not GATEWAY_FILE.exists():
        print(f"Gateway file not found: {GATEWAY_FILE}", file=sys.stderr)
        return 1

    all_tools = find_tool_classes(TOOLS_DIR)
    if all_tools is None:
        return 1
    registered = find_registered_classes(GATEWAY_FILE)

    missing = {name: path for name, path in all_tools.items() if name not in registered}

    if not missing:
        return 0

    print("The following unprivileged tools are not registered in the MCP gateway:\n")
    for name, path in sorted(missing.items()):
        print(f"  {name}  ({path})")

    print(f"\nPlease add them to {GATEWAY_FILE}")
    print("\nTo bypass this check:\n  SKIP_GATEWAY_CHECK=1 git push ...")

    return 1


if __name__ == "__main__":
    sys.exit(main())
