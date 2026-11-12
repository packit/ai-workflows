"""Upstream release tag conventions used when picking a backport base commit.

Upstream projects name their release tags inconsistently: ``v2.2.5``,
``libexpat-2_2_5``, ``REL_9_6`` and a dozen other shapes. Both the agent's
``find_base_commit`` tool and the Amalgamator harness pick the base commit, and
they should choose by the same rules for the same package version — so the
pattern list lives here, free of framework imports.
"""

import re


def _base_commit_package_names(pkg_name: str | None) -> tuple[str | None, str | None]:
    """Return the package name and its version-stripped variant.

    RHEL versioned packages (nodejs22, python39) keep the major version in the
    name; tag patterns are also tried without it.
    """
    pkg_base = re.sub(r"[-_]?\d+$", "", pkg_name) if pkg_name else None
    if pkg_base == pkg_name:
        pkg_base = None
    return pkg_name, pkg_base


def base_commit_tag_patterns(version: str, pkg_name: str | None) -> list[str]:
    """Build the ordered list of tag names to try for an upstream release."""
    pkg_name, pkg_base = _base_commit_package_names(pkg_name)

    ver_under = version.replace(".", "_")
    # Split digit-letter boundaries then uppercase (e.g. 9.9p1 -> 9_9_P1)
    ver_upper = re.sub(r"(\d)([a-zA-Z])", r"\1_\2", ver_under).upper()

    tag_patterns = [
        f"v{version}",
        f"{version}",
        f"release-{version}",
        f"{version}-release",
        f"rel-{version}",
        f"{version}.0",
        f"v{version}.0",
        f"v{ver_under}",
        f"V_{ver_upper}",
        # Added when the patterns moved here: libexpat tags releases R_2_2_5.
        # This intentionally widens the agent tool's matching as well.
        f"R_{ver_upper}",
        f"REL_{ver_under}",
        f"REL{ver_under}",
    ]

    if pkg_name:
        tag_patterns.extend(
            [
                f"{pkg_name}-{ver_under}",
                f"{pkg_name}-{version}",
                f"{pkg_name}_{ver_under}",
                f"{pkg_name.upper()}-{ver_under}",
                f"{pkg_name.upper()}_{ver_upper}",
            ]
        )

    if pkg_base:
        tag_patterns.extend(
            [
                f"{pkg_base}-{ver_under}",
                f"{pkg_base}-{version}",
                f"{pkg_base}_{ver_under}",
                f"{pkg_base.upper()}-{ver_under}",
                f"{pkg_base.upper()}_{ver_upper}",
            ]
        )

    return list(dict.fromkeys(tag_patterns))


def find_base_commit_tag(tags: list[str] | set[str], version: str, pkg_name: str | None) -> str | None:
    """Return the first tag matching *version* conventions, or ``None``."""
    tag_set = set(tags)
    for tag in base_commit_tag_patterns(version, pkg_name):
        if tag in tag_set:
            return tag
    return None
