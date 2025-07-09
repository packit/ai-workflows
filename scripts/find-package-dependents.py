#!/usr/bin/env python3

import argparse
import json
import logging
import re
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Dict, List, Set
from urllib.parse import urlparse
from hashlib import sha256

KNOWN_ARCHS: Set[str] = {"x86_64", "aarch64", "ppc64le", "s390x", "noarch"}

class RepoQueryError(Exception):
    """Raised when a dnf repoquery call fails or returns invalid data."""
    pass

def derive_repository_id_from_url(repository_url: str) -> str:
    """
    Derive a unique repository key from a full repository URL:
      - Use hostname[_port] as fallback.
      - Pick the last path segment that isn't 'os' or known architectures.
      - Append first 16 hex chars of SHA-256 for uniqueness.
    """
    parsed_url = urlparse(repository_url)

    segments = [segment for segment in parsed_url.path.strip("/").split("/") if segment]

    component = None
    for segment in reversed(segments):
        if segment == "os" or segment in KNOWN_ARCHS:
            continue
        component = segment
        break

    if not component:
        host = parsed_url.hostname or ""
        component = f"{host}_{parsed_url.port}" if parsed_url.port else host

    component_safe = re.sub(r"[^A-Za-z0-9_]+", "_", component)
    digest = sha256(repository_url.encode("utf-8")).hexdigest()[:16]
    return f"{component_safe}_{digest}"

def run_command(command: List[str]) -> str:
    """
    Run a command and log output
    Returns the stdout content.
    """
    logger = logging.getLogger(__name__)

    logger.debug(f"    ❯ {' '.join(command)}")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True
    )

    stdout_lines = []
    stderr_lines = []

    while True:
        stdout_line = process.stdout.readline()
        stderr_line = process.stderr.readline()

        if stdout_line:
            stdout_line = stdout_line.rstrip()
            stdout_lines.append(stdout_line)
            logger.debug(f"    {stdout_line}")

        if stderr_line:
            stderr_line = stderr_line.rstrip()
            stderr_lines.append(stderr_line)
            logger.debug(f"    {stderr_line}")

        if process.poll() is not None and not stdout_line and not stderr_line:
            break

    return_code = process.wait()
    logger.debug(f"\n");

    stdout_content = '\n'.join(stdout_lines)
    stderr_content = '\n'.join(stderr_lines)

    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command, stdout_content, stderr_content)

    return stdout_content

def query_direct_dependents(
        package_name: str,
        repository_paths: Dict[str, str]
    ) -> List[str]:
    command = ["dnf", "repoquery", "--disablerepo=*"]
    for repository_id, repository_url in repository_paths.items():
        command.append(f"--repofrompath=repo-{repository_id},{repository_url}")
    for repository_id in repository_paths:
        command.append(f"--enablerepo=repo-{repository_id}")
    command += [
        "--whatdepends", package_name,
        "--qf", "%{name}\n",
        "--quiet",
    ]

    try:
        stdout_content = run_command(command)
    except subprocess.CalledProcessError as error:
        raise RepoQueryError(
            f"Failed to query reverse dependencies for {package_name!r}: "
            f"{error.stderr.strip() if error.stderr else 'Unknown error'}"
        )

    dependencies: List[str] = []
    for line in stdout_content.splitlines():
        package_name_clean = line.strip()
        if package_name_clean:
            dependencies.append(package_name_clean)
    return sorted(set(dependencies))

def query_source_package(
        package_name: str,
        repository_paths: Dict[str, str]
    ) -> str:
    """Query the source package name for a given binary package."""
    command = ["dnf", "repoquery", "--disablerepo=*"]
    for repository_id, repository_url in repository_paths.items():
        command.append(f"--repofrompath=repo-{repository_id},{repository_url}")
    for repository_id in repository_paths:
        command.append(f"--enablerepo=repo-{repository_id}")
    command += [
        package_name,
        "--qf", "%{sourcerpm}\n",
        "--quiet",
    ]

    try:
        stdout_content = run_command(command)
    except subprocess.CalledProcessError as error:
        raise RepoQueryError(
            f"Failed to query source package for {package_name!r}: "
            f"{error.stderr.strip() if error.stderr else 'Unknown error'}"
        )

    source_rpm = stdout_content.strip()
    if not source_rpm:
        raise RepoQueryError(f"Empty source RPM returned for {package_name!r}")
    m = re.match(r'^(?P<name>.*)-[^-]+-[^-]+\.src\.rpm$', source_rpm)
    if not m:
        raise RepoQueryError(
            f"Unexpected source-RPM format for {package_name!r}: {source_rpm!r}"
        )
    return m.group("name")

def convert_to_source_packages(
        dependents_list: List[str],
        repository_paths: Dict[str, str]
    ) -> List[str]:
    """Convert a list of binary package names to their source package names."""
    source_packages: Set[str] = set()

    for package in dependents_list:
        source_package = query_source_package(package, repository_paths)
        source_packages.add(source_package)

    return sorted(source_packages)

def build_dependents_graph(
        root_package: str,
        repository_paths: Dict[str, str]
    ) -> Dict[str, List[str]]:
    dependents_graph: Dict[str, List[str]] = {}
    known_packages: Set[str] = set()
    queue = deque([root_package])

    while queue:
        package = queue.popleft()
        if package in known_packages:
            continue
        known_packages.add(package)

        direct_dependents = query_direct_dependents(package, repository_paths)
        dependents_graph[package] = direct_dependents
        for dependent_package in direct_dependents:
            if dependent_package not in known_packages:
                queue.append(dependent_package)

    return dependents_graph

def reduce_dependents_to_list(
        root_package: str,
        dependents_graph: Dict[str, List[str]]
    ) -> List[str]:
    all_dependents: Set[str] = set()
    queue = deque(dependents_graph.get(root_package, []))

    while queue:
        package = queue.popleft()
        if package in all_dependents:
            continue
        all_dependents.add(package)
        queue.extend(dependents_graph.get(package, []))

    return sorted(all_dependents)

def parse_command_line_arguments():
    parser = argparse.ArgumentParser(
        description="Find reverse dependencies of an RPM package."
    )
    parser.add_argument(
        "package_name",
        help="Name of the package to inspect"
    )
    parser.add_argument(
        "--base-url",
        dest="base_url",
        default="http://download.devel.redhat.com/rhel-10/nightly/RHEL-10/latest-RHEL-10",
        help="Base URL for nightly repositories"
    )
    parser.add_argument(
        "--repositories",
        dest="repository_names",
        default="BaseOS,AppStream,CRB",
        help=(
            "Comma-separated list of repository names (relative to base URL) "
            "or full repository URLs. "
            "Examples:\n"
            "  --repositories BaseOS,AppStream,CRB\n"
            "  --repositories BaseOS,https://download.devel.redhat.com/rhel-10/nightly/RHEL-10/latest-RHEL-10/compose/RT/x86_64/os\n"
        )
    )
    parser.add_argument(
        "--arch",
        choices=sorted(KNOWN_ARCHS),
        dest="arch",
        default="x86_64",
        help="CPU architecture (for example: x86_64, s390x)"
    )
    parser.add_argument(
        "--output-file",
        dest="output_file",
        type=Path,
        help="Write output to this file instead of stdout"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include transitive reverse dependencies (default is direct only)"
    )
    parser.add_argument(
        "--source-packages",
        action="store_true",
        help="Convert dependent package names to their source package names"
    )
    parser.add_argument(
        "--format",
        choices=["json", "plain"],
        default="plain",
        help="Output format: json or plain (one per line)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging"
    )
    return parser.parse_args()

def build_repository_paths(
        base_url: str,
        repository_names: str,
        arch: str
    ) -> Dict[str, str]:
    repositories = [repository.strip() for repository in repository_names.split(",") if repository.strip()]
    if not repositories:
        logging.error("At least one repository alias or URL must be provided")
        sys.exit(1)

    base = base_url.rstrip("/")
    paths: Dict[str, str] = {}
    for repository in repositories:
        if repository.startswith(("http://", "https://")):
            repository_url = repository.rstrip("/")
            repository_id = derive_repository_id_from_url(repository_url)
        else:
            repository_id = repository
            repository_url = f"{base}/compose/{repository_id}/{arch}/os/"
        paths[repository_id] = repository_url
    return paths

def main():
    arguments = parse_command_line_arguments()
    level = logging.DEBUG if arguments.verbose else logging.INFO
    logging.basicConfig(format="%(levelname)s: %(message)s", level=level)

    repositories = build_repository_paths(
        arguments.base_url, arguments.repository_names, arguments.arch
    )
    try:
        if arguments.all:
            dependents_graph = build_dependents_graph(arguments.package_name, repositories)
            dependents = reduce_dependents_to_list(arguments.package_name, dependents_graph)
            if arguments.source_packages:
                sources_graph = {}
                for package, package_dependents in dependents_graph.items():
                    source_dependents = convert_to_source_packages(package_dependents, repositories)
                    sources_graph[package] = source_dependents
                dependents_graph = sources_graph
                dependents = reduce_dependents_to_list(arguments.package_name, dependents_graph)
        else:
            dependents = query_direct_dependents(arguments.package_name, repositories)
            if arguments.source_packages:
                dependents = convert_to_source_packages(dependents, repositories)
        if arguments.format == "json":
            if arguments.all:
                output_dict: Dict[str, List[str]] = {}
                for package in dependents_graph:
                    package_dependents = reduce_dependents_to_list(package, dependents_graph)
                    output_dict[package] = package_dependents
            else:
                output_dict = {arguments.package_name: dependents}
            output_data = json.dumps(output_dict, indent=2)
        else:
            output_data = "\n".join(dependents)
        if arguments.output_file:
            arguments.output_file.write_text(output_data)
            logging.info("Wrote results to %s", arguments.output_file)
        else:
            print(output_data)
    except RepoQueryError as error:
        logging.error("%s", error)
        sys.exit(1)

if __name__ == "__main__":
    main()
