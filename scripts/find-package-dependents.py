#!/usr/bin/env python3

import argparse
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
from collections import deque
from hashlib import sha256
from pathlib import Path
from typing import Dict, List, Set, Generator, Any
from urllib.parse import urlparse


EXIT_SUCCESS = 0
EXIT_REPO_QUERY_ERROR = 1
EXIT_NO_DEPENDENTS_FOUND = 2
EXIT_INVALID_ARGUMENTS = 3
EXIT_CACHE_UPDATE_ERROR = 4
EXIT_PACKAGE_NOT_FOUND = 5

KNOWN_ARCHS: Set[str] = {"x86_64", "aarch64", "ppc64le", "s390x", "noarch"}


class RepoQueryMetrics:
    """
    Tracks metrics for dnf repoquery calls.

    This class focuses purely on metrics gathering:
    - Call counting by type
    - Statistics generation
    """

    def __init__(self):
        self._call_count: int = 0
        self._calls_by_type: Dict[str, int] = {}
        self._filter_calls: int = 0
        self._filter_failures: int = 0

    def log_call(self, purpose: str, package_name: str) -> None:
        """
        Log a dnf repoquery call with detailed information.

        Args:
            purpose: The purpose of the repoquery call (e.g., 'find_direct_dependents')
            package_name: The package being queried
        """
        self._call_count += 1
        self._calls_by_type[purpose] = self._calls_by_type.get(purpose, 0) + 1

    def log_filter_call(self, package_name: str, success: bool) -> None:
        """
        Log a filter command call.

        Args:
            package_name: The package being filtered
            success: Whether the filter command succeeded
        """
        self._filter_calls += 1
        if not success:
            self._filter_failures += 1

    def get_stats(self) -> Dict[str, Any]:
        """
        Get current statistics about dnf repoquery usage.

        Returns:
            Dictionary containing statistics about dnf repoquery calls
        """
        return {
            "total_calls": self._call_count,
            "calls_by_type": self._calls_by_type.copy(),
            "filter_calls": self._filter_calls,
            "filter_failures": self._filter_failures,
        }


class SourcePackageCache:
    """
    Caches source package mappings for performance optimization.

    This class handles the caching of binary package to source package mappings
    to avoid repeated repoquery calls for the same package.
    """

    def __init__(self):
        self._cache: Dict[str, str] = {}

    def get(self, package_name: str) -> str | None:
        """
        Get a cached source package name.

        Args:
            package_name: The binary package name

        Returns:
            The cached source package name, None if not cached, or '' if package not found
        """
        return self._cache.get(package_name)

    def set(self, package_name: str, source_package_name: str | None) -> None:
        """
        Cache a source package mapping.

        Args:
            package_name: The binary package name
            source_package_name: The source package name, or '' if package not found
        """
        self._cache[package_name] = source_package_name
        if source_package_name == '':
            logging.debug(f"   Cached package not found: {package_name} → not found")
        else:
            logging.debug(f"   Cached source package mapping: {package_name} → {source_package_name}")

    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary containing cache statistics
        """
        found_count = sum(1 for result in self._cache.values() if result is not None)
        not_found_count = len(self._cache) - found_count
        return {
            "cache_size": len(self._cache),
            "found_count": found_count,
            "not_found_count": not_found_count,
            "cached_packages": list(sorted(self._cache.keys())),
        }


class FilterCache:
    """
    Caches filter command results for performance optimization.

    This class handles the caching of filter command results to avoid running
    the same filter command multiple times for the same package.
    """

    def __init__(self):
        self._cache: Dict[str, bool] = {}

    def get(self, package_name: str) -> bool | None:
        """
        Get a cached filter result.

        Args:
            package_name: The package name

        Returns:
            The cached filter result (True if package passed filter, False if failed), or None if not cached
        """
        return self._cache.get(package_name)

    def set(self, package_name: str, passed_filter: bool) -> None:
        """
        Cache a filter result.

        Args:
            package_name: The package name
            passed_filter: Whether the package passed the filter (True) or failed (False)
        """
        self._cache[package_name] = passed_filter
        logging.debug(f"   Cached filter result: {package_name} → {'pass' if passed_filter else 'fail'}")

    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary containing cache statistics
        """
        passed_count = sum(1 for result in self._cache.values() if result)
        failed_count = len(self._cache) - passed_count
        return {
            "cache_size": len(self._cache),
            "passed_count": passed_count,
            "failed_count": failed_count,
            "cached_packages": list(sorted(self._cache.keys())),
        }


class DependencyCache:
    """
    Caches dependency query results for performance optimization.

    This class handles the caching of dnf repoquery --whatdepends results to avoid
    repeated calls for the same package. It also tracks whether the cached results
    are partial (limited by max_results) or complete.
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get(self, package_name: str) -> List[str] | None:
        """
        Get cached dependencies for a package.

        Args:
            package_name: The package name

        Returns:
            The cached list of dependent packages, or None if not cached
        """
        entry = self._cache.get(package_name)
        if entry is not None:
            return entry["dependents"]
        return None

    def has(self, package_name: str) -> bool:
        """
        Check if a package has any cached results (partial or complete).

        Args:
            package_name: The package name

        Returns:
            True if the package has cached results, False otherwise
        """
        return package_name in self._cache

    def has_all(self, package_name: str) -> bool:
        """
        Check if a package has complete (non-partial) cached results.

        Args:
            package_name: The package name

        Returns:
            True if the package has complete cached results, False otherwise
        """
        entry = self._cache.get(package_name)
        if entry is not None:
            return not entry["partial"]
        return False

    def set(self, package_name: str, dependents: List[str], partial: bool = False) -> None:
        """
        Cache dependency results for a package.

        Args:
            package_name: The package name
            dependents: List of dependent package names
            partial: Whether the results are partial (limited by max_results)
        """
        self._cache[package_name] = {
            "dependents": dependents,
            "partial": partial
        }
        partial_info = " (partial)" if partial else ""
        logging.debug(f"   Cached dependency results: {package_name} → {len(dependents)} dependents{partial_info}")

    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary containing cache statistics
        """
        total_dependents = sum(len(entry["dependents"]) for entry in self._cache.values())
        partial_count = sum(1 for entry in self._cache.values() if entry["partial"])
        complete_count = len(self._cache) - partial_count
        return {
            "cache_size": len(self._cache),
            "total_dependents": total_dependents,
            "complete_count": complete_count,
            "partial_count": partial_count,
            "cached_packages": list(sorted(self._cache.keys())),
        }


class RepoQueryError(Exception):
    """Raised when a dnf repoquery call fails or returns invalid data."""

    def __init__(self, message: str, exit_code: int = EXIT_REPO_QUERY_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


class NoDependentsFoundError(Exception):
    """Raised when no dependents are found for a package."""

    def __init__(self, package_name: str):
        super().__init__(f"No dependents found for package: {package_name}")
        self.package_name = package_name
        self.exit_code = EXIT_NO_DEPENDENTS_FOUND


class PackageNotFoundError(Exception):
    """Raised when a package is not found in the repositories."""

    def __init__(self, package_name: str):
        super().__init__(f"Package not found in repositories: {package_name}")
        self.package_name = package_name
        self.exit_code = EXIT_PACKAGE_NOT_FOUND


def run_filter_command(package_name: str, filter_command: str, metrics: RepoQueryMetrics, filter_cache: FilterCache, verbose: bool = False) -> bool:
    """
    Run a filter command on a package to determine if it should be included.

    Args:
        package_name: The package name to check
        filter_command: The shell command to run
        metrics: Metrics object to track filter command usage
        filter_cache: Cache object to store filter results
        verbose: Whether to enable verbose logging

    Returns:
        True if the command succeeds (package should be included), False otherwise
    """
    if not filter_command or not filter_command.strip():
        return True

    cached_result = filter_cache.get(package_name)
    if cached_result is not None:
        logging.debug(f"📋 Filter cache hit: Filter result for {package_name} → {'pass' if cached_result else 'fail'}")
        return cached_result

    logging.debug(f"🔍 Running filter command on package: {package_name}")

    try:
        result = run_command(
            filter_command,
            extra_environment={"PACKAGE": package_name}
        )

        success = result["return_code"] == 0
        metrics.log_filter_call(package_name, success)

        filter_cache.set(package_name, success)

        if success:
            logging.debug(f"   ✅ Filter command succeeded for {package_name}")
        else:
            logging.debug(f"   ❌ Filter command failed for {package_name} (exit code: {result['return_code']})")

        return success

    except Exception as e:
        logging.debug(f"   💥 Filter command error for {package_name}: {e}")
        metrics.log_filter_call(package_name, False)
        filter_cache.set(package_name, False)
        return False


def update_dnf_cache(repository_paths: Dict[str, str], verbose: bool = False) -> None:
    """
    Update dnf cache for all repositories once upfront.
    This allows subsequent repoquery calls to use --cacheonly for better performance.

    Args:
        repository_paths: Dictionary mapping repository IDs to URLs
        verbose: Whether to enable verbose logging

    Raises:
        RepoQueryError: If the dnf cache update fails
    """

    logging.debug("🔄 Updating dnf cache for all repositories...")

    try:
        result = dnf("makecache --refresh", repository_paths, verbose, cache_only=False)
        logging.debug("✅ Dnf cache updated successfully")
        logging.debug(f"Cache update output: {result}")
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip() if error.stderr else "Unknown error"
        raise RepoQueryError(f"Failed to update dnf cache: {stderr}", EXIT_CACHE_UPDATE_ERROR)


def derive_repository_id_from_url(repository_url: str) -> str:
    """
    Derive a unique repository key from a full repository URL.

    The algorithm is:
    - Uses hostname[_port] as fallback if no suitable path segment is found
    - Picks the last path segment that isn't 'os' or known architectures
    - Appends first 16 hex chars of SHA-256 for uniqueness

    Args:
        repository_url: The full repository URL to derive an ID from

    Returns:
        A unique repository identifier string
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


def get_signal_name(signal_num: int) -> str:
    """
    Get the name of a signal number.

    Args:
        signal_num: The signal number

    Returns:
        The signal name as a string
    """
    try:
        return signal.Signals(signal_num).name
    except ValueError:
        return f"SIG{signal_num}"


def quote_command(args):
    """
    Join a list of arguments into a command string, only quoting arguments
    that would be semantically different if left unquoted.

    An argument needs quoting if leaving it unquoted would cause the shell
    to interpret it differently (split it into multiple arguments, perform
    expansions, etc.).

    Args:
        args: List of string arguments

    Returns:
        String representing the command with minimal quoting
    """
    quoted_args = []

    for arg in args:
        if not arg:
            quoted_args.append(shlex.quote(arg))
            continue

        try:
            if shlex.split(arg) == [arg]:
                quoted_args.append(arg)
            else:
                quoted_args.append(shlex.quote(arg))
        except ValueError:
            quoted_args.append(shlex.quote(arg))

    return ' '.join(quoted_args)

def run_command(command: List[str] | str, extra_environment: Dict[str, str] | None = None) -> Dict[str, Any]:
    """
    Run a command and log output.

    Args:
        command: List of command arguments to execute (or string)
        extra_environment: Optional dictionary of additional environment variables

    Returns:
        Dictionary containing 'return_code' and 'output' keys

    Raises:
        subprocess.CalledProcessError: If the command returns a non-zero exit code
    """
    if isinstance(command, list):
        command_string = quote_command(command)
    else:
        command_string = command

    logging.debug(f"\n        ❯ {command_string}")

    environment = None
    if extra_environment:
        environment = os.environ.copy()
        environment.update(extra_environment)

    result = subprocess.run(
        command_string,
        env=environment,
        capture_output=True,
        shell=True,
        text=True
    )

    for line in result.stderr.splitlines():
        if line.strip():
            logging.debug(f"        {line.strip()}")

    for line in result.stdout.splitlines():
        if line.strip():
            logging.debug(f"        {line.strip()}")

    if result.returncode < 0:
        signal_name = get_signal_name(abs(result.returncode))
        logging.debug(f"        Process killed by signal {abs(result.returncode)} ({signal_name})")
    else:
        logging.debug(f"        Process exited with code {result.returncode}")

    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, command,
            result.stdout,
            result.stderr
        )

    return {"return_code": result.returncode, "output": result.stdout}


def dnf(command: str, repository_paths: Dict[str, str], verbose: bool = False, cache_only: bool = True) -> str:
    """
    Execute a dnf command with repository setup and return stdout content.

    This function factors out the repeated pattern of setting up dnf commands
    with repository configuration and executing them.

    Args:
        command: The dnf command as a string (e.g., "repoquery --whatdepends package")
        repository_paths: Dictionary mapping repository IDs to URLs
        verbose: Whether to enable verbose logging
        cache_only: Whether to use --cacheonly flag (default: True)

    Returns:
        The stdout content as a string (stripped)

    Raises:
        subprocess.CalledProcessError: If the command returns a non-zero exit code
    """
    command_parts = shlex.split(command)
    full_command = ["dnf"] + command_parts

    full_command.extend(["--disablerepo=*"])
    if not verbose:
        full_command.append("--quiet")
    if cache_only:
        full_command.append("--cacheonly")
    for repository_id, repository_url in repository_paths.items():
        full_command.append(f"--repofrompath=repo-{repository_id},{repository_url}")
    for repository_id in repository_paths:
        full_command.append(f"--enablerepo=repo-{repository_id}")

    result = run_command(full_command)
    return result["output"].strip()


def generate_direct_dependents(
        package_name: str,
        repository_paths: Dict[str, str],
        metrics: RepoQueryMetrics,
        dependency_cache: DependencyCache,
        verbose: bool = False,
        cache_only: bool = False,
        max_results: int | None = None
    ) -> Generator[str, None, None]:
    """
    Generator that yields direct dependents one at a time.

    Args:
        package_name: The package to find direct dependents for
        repository_paths: Dictionary mapping repository IDs to URLs
        metrics: Metrics object to track repoquery calls
        dependency_cache: Cache object to store dependency results
        verbose: Whether to enable verbose logging
        cache_only: If True, only return cached results and never make repoquery calls
        max_results: Maximum number of dependents to yield (None for unlimited)

    Yields:
        Package names that directly depend on the given package

    Raises:
        RepoQueryError: If the dnf repoquery call fails
    """
    logging.debug(f"\n🔍 Finding direct dependents for package: {package_name}")

    if (not cache_only and dependency_cache.has_all(package_name)) or (cache_only and dependency_cache.has(package_name)):
        cached_dependents = dependency_cache.get(package_name)
        logging.debug(f"📋 Dependency cache hit: Dependents for {package_name} → {len(cached_dependents)} dependents")
        count = 0
        for dependent_name in cached_dependents:
            if max_results is not None and count >= max_results:
                break
            yield dependent_name
            count += 1
        return

    if cache_only:
        logging.debug(f"📋 CACHE ONLY MODE: No sufficient cached dependents for {package_name}, skipping repoquery call")
        return

    metrics.log_call("dnf repoquery --whatdepends", package_name)
    try:
        stdout_content = dnf(f"repoquery --whatdepends {package_name} --qf '%{{name}}\\n'", repository_paths, verbose)
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip() if error.stderr else "Unknown error"
        raise RepoQueryError(
            f"Failed to query reverse dependencies for {package_name!r}: {stderr}"
        )

    seen: Set[str] = set()
    dependents_found = 0
    dependents_list: List[str] = []
    is_partial = False
    count = 0

    for line in stdout_content.splitlines():
        if max_results is not None and count > max_results:
            is_partial = True
            break

        dependent_name = line.strip()
        if dependent_name and dependent_name not in seen:
            seen.add(dependent_name)
            dependents_found += 1
            dependents_list.append(dependent_name)
            logging.debug(f"\n   Found dependent: {dependent_name}")

            if max_results is None or count <= max_results:
                dependency_cache.set(package_name, dependents_list, partial=True)
                yield dependent_name
                count += 1

    dependency_cache.set(package_name, dependents_list, is_partial)
    logging.debug(f"\n   Total direct dependents found for {package_name}: {len(dependents_list)} ({'partial' if is_partial else 'complete'})")


def query_source_package(
        package_name: str,
        repository_paths: Dict[str, str],
        metrics: RepoQueryMetrics,
        source_cache: SourcePackageCache,
        verbose: bool = False,
        allow_missing: bool = False
    ) -> str:
    """
    Query the source package name for a given binary package.

    Args:
        package_name: The binary package name to find the source package for
        repository_paths: Dictionary mapping repository IDs to URLs
        metrics: Metrics object to track repoquery calls
        source_cache: Cache object to store source package mappings
        verbose: Whether to enable verbose logging
        allow_missing: Whether to allow missing packages to be non-fatal

    Returns:
        The source package name, or empty string if package not found and allow_missing is True

    Raises:
        RepoQueryError: If the query fails or returns invalid data
        PackageNotFoundError: If the package is not found and allow_missing is False
    """

    cached_source_package = source_cache.get(package_name)
    if cached_source_package == '' and not allow_missing:
        logging.debug(f"\n📋 Source cache hit: Package {package_name} → not found")
        raise PackageNotFoundError(package_name)

    if cached_source_package:
        logging.debug(f"\n📋 Source cache hit: Source package for {package_name} → {cached_source_package}")
        return cached_source_package

    logging.debug(f"\n🔍 Querying source package for binary package: {package_name}")

    metrics.log_call("dnf repoquery --qf '%{sourcerpm}'", package_name)
    try:
        stdout_content = dnf(f"repoquery {package_name} --qf '%{{sourcerpm}}\\n'", repository_paths, verbose)
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip() if error.stderr else "Unknown error"
        raise RepoQueryError(
            f"Failed to query source package for {package_name!r}: {stderr}"
        )

    source_rpm = stdout_content.strip()
    if not source_rpm:
        source_cache.set(package_name, '')
        if allow_missing:
            logging.debug(f"   Package {package_name} not found, but continuing due to --allow-missing")
            return ''
        raise PackageNotFoundError(package_name)

    m = re.match(r'^(?P<name>.*)-[^-]+-[^-]+\.src\.rpm$', source_rpm)
    if not m:
        raise RepoQueryError(
            f"Unexpected source-RPM format for {package_name!r}: {source_rpm!r}"
        )

    source_package_name = m.group("name")
    logging.debug(f"\n   Source package for {package_name}: {source_package_name}")

    source_cache.set(package_name, source_package_name)

    return source_package_name


def query_package_description(
        package_name: str,
        repository_paths: Dict[str, str],
        metrics: RepoQueryMetrics,
        verbose: bool = False
    ) -> str:
    """
    Query the description for a given package.

    Args:
        package_name: The package name to find the description for
        repository_paths: Dictionary mapping repository IDs to URLs
        metrics: Metrics object to track repoquery calls
        verbose: Whether to enable verbose logging

    Returns:
        The package description with newlines removed

    Raises:
        RepoQueryError: If the query fails or returns invalid data
    """

    logging.debug(f"\n🔍 Querying description for package: {package_name}")

    metrics.log_call("dnf repoquery --qf '%{description}'", package_name)
    try:
        stdout_content = dnf(f"repoquery {package_name} --qf %{{description}}", repository_paths, verbose)
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.strip() if error.stderr else "Unknown error"
        raise RepoQueryError(
            f"Failed to query description for {package_name!r}: {stderr}"
        )

    description = stdout_content.strip()
    if description:
        description = " ".join(description.splitlines())

    logging.debug(f"\n   Description for {package_name}: {description}")

    return description


def convert_to_source_packages(
        dependents: Generator[str, None, None],
        repository_paths: Dict[str, str],
        metrics: RepoQueryMetrics,
        source_cache: SourcePackageCache,
        filter_cache: FilterCache,
        max_results: int | None = None,
        verbose: bool = False,
        filter_command: str | None = None,
        allow_missing: bool = False
    ) -> Generator[str, None, None]:
    """
    Generator that converts a stream of binary package names into source package names.

    Args:
        dependents: Generator yielding binary package names
        repository_paths: Dictionary mapping repository IDs to URLs
        max_results: Maximum number of unique source packages to yield (None for unlimited)
        filter_command: Optional shell command to run on each source package
        allow_missing: Whether to allow missing packages to be non-fatal

    Yields:
        Source package names (unique, up to max_results if specified)

    Raises:
        RepoQueryError: If any source package query fails
    """
    logging.debug("🔄 Converting binary packages to source packages")

    source_packages: Set[str] = set()
    converted_count = 0

    for package in dependents:
        if max_results is not None and converted_count >= max_results:
            logging.debug(f"Reached max_results={max_results}, stopping conversion")
            break

        logging.debug(f"   Converting binary package: {package}")
        source_package = query_source_package(
            package, repository_paths, metrics, source_cache, verbose, allow_missing
        )

        if not source_package:
            logging.debug(f"   Skipping binary package {package} (no source package found)")
            continue

        if source_package not in source_packages:
            source_packages.add(source_package)

            if filter_command:
                if not run_filter_command(source_package, filter_command, metrics, filter_cache, verbose):
                    logging.debug(f"   Skipping source package {source_package} due to filter command")
                    continue

            converted_count += 1
            logging.debug(f"   New source package found: {source_package}")
            yield source_package
        else:
            logging.debug(f"   Source package already seen: {source_package}")

    logging.debug(f"   Total unique source packages converted: {converted_count}")


def compute_transitive_closure(
        root_package: str,
        dependents_map: Dict[str, Dict[str, Any]],
        max_results: int | None = None,
        filter_function = None,
    ) -> Dict[str, Dict[str, Any]]:
    """
    Compute the transitive closure of the dependency graph.

    For each package, returns a dictionary containing the list of all packages that can be reached
    from it through the dependency graph (in breadth-first order) and a partial flag indicating
    whether the results are incomplete due to max_results being reached.
    If filter_function is provided, it should take a package name and a list of current dependents
    and return True if the package should be included, False if it should be filtered out.
    Filtered packages are excluded from the graph but still traversed to find their dependents.

    Args:
        root_package: root package name to check for partial results
        dependents_map: Dictionary mapping package names to their dependent packages
        max_results: Maximum number of results to return (including the root package)
        filter_function: Optional function that takes a package name and dependents list and returns True/False

    Returns:
        Dictionary mapping package names to dictionaries containing 'dependents' list and 'partial' flag
    """
    graph: Dict[str, Dict[str, Any]] = {}

    # The root package gets all results but itself as dependents
    max_root_dependents = max_results - 1 if max_results is not None else None

    for package, entry in dependents_map.items():
        known_packages: Set[str] = set()
        transitive_dependents: List[str] = []
        queue = deque(entry["dependents"])
        is_partial = entry["partial"]

        # We stop reading from the queue once the root package has all the results the user asked for
        # (But we still need to extend the queue for the remaining of the loop to know if we're missing out
        # on any results because of the max_results limit)
        root_hit_max_dependents = False

        while queue:
            dependent = queue.popleft()

            if dependent in known_packages:
                continue

            # If the root package has hit its dependent limit, we don't add this dependent to the results,
            # but we still need to use the dependent to extend the queue so we can know if we're missing out
            # on any results because of the max_results limit
            if package == root_package and max_root_dependents is not None and len(transitive_dependents) >= max_root_dependents:
                root_hit_max_dependents = True

            if package != root_package or not root_hit_max_dependents:
                # Mark this package as seen to prevent future cycles
                known_packages.add(dependent)

                # Apply optional filtering function
                if filter_function is None or filter_function(dependent, transitive_dependents):
                    transitive_dependents.append(dependent)

            if dependent in dependents_map:
                dependent_entry = dependents_map[dependent]
                if dependent_entry["partial"]:
                    is_partial = True

                queue.extend(dependent_entry["dependents"])

            if root_hit_max_dependents:
                # At this point the queue accurately reflects what work is left to do
                # so we can use it to know if the results are complete for the root package
                is_partial = bool(queue)
                break

        if not max_results or len(graph.keys()) < max_results:
            graph[package] = {
                "dependents": transitive_dependents,
                "partial": is_partial
            }

    return graph


def build_dependents_list(
        package_name: str,
        repository_paths: Dict[str, str],
        show_source_packages: bool,
        source_cache: SourcePackageCache,
        metrics: RepoQueryMetrics,
        filter_cache: FilterCache,
        dependency_cache: DependencyCache,
        max_results: int | None = None,
        verbose: bool = False,
        keep_cycles: bool = False,
        filter_command: str | None = None,
        allow_missing: bool = False
    ) -> List[str]:
    """
    Build a list of dependents for a given package.

    Args:
        package_name: The package to find dependents for
        repository_paths: Dictionary mapping repository IDs to URLs
        show_source_packages: Whether to convert to source package names
        max_results: Maximum number of results to return
        filter_command: Optional shell command to run on each dependent package
        allow_missing: Whether to allow missing packages to be non-fatal

    Returns:
        List of dependent package names (binary or source depending on show_source_packages)
    """
    logging.debug(f"🔄 Building dependents list for: {package_name}")
    logging.debug(f"   Show source packages: {show_source_packages}")
    logging.debug(f"   Max results: {max_results}")
    logging.debug(f"   Filter command: {filter_command}")

    dependents = generate_direct_dependents(package_name, repository_paths, metrics, dependency_cache, verbose, cache_only=False)

    if show_source_packages:
        dependents = convert_to_source_packages(
            dependents, repository_paths, metrics, source_cache, filter_cache,
            max_results, verbose, filter_command, allow_missing
        )

    collected_packages: List[str] = []
    discovered_count = 0
    is_partial = False
    for dependent_package in dependents:
        if max_results is not None and discovered_count >= max_results:
            is_partial = True
            break

        if not keep_cycles and package_name == dependent_package:
            continue

        if filter_command:
            if not run_filter_command(dependent_package, filter_command, metrics, filter_cache, verbose):
                logging.debug(f"   Skipping dependent package {dependent_package} due to filter command")
                continue

        discovered_count += 1
        if max_results is None or discovered_count <= max_results:
            collected_packages.append(dependent_package)

    logging.debug(f"   Total dependents collected: {len(collected_packages)} ({'partial' if is_partial else 'complete'})")

    if len(collected_packages) == 0:
        raise NoDependentsFoundError(package_name)

    return collected_packages


def build_dependents_graph(
        root_package: str,
        repository_paths: Dict[str, str],
        show_source_packages: bool,
        source_cache: SourcePackageCache,
        metrics: RepoQueryMetrics,
        filter_cache: FilterCache,
        dependency_cache: DependencyCache,
        max_results: int | None = None,
        keep_cycles: bool = False,
        verbose: bool = False,
        filter_command: str | None = None,
        allow_missing: bool = False
    ) -> Dict[str, Dict[str, Any]]:
    """
    Build a transitive graph of reverse dependencies for the given package.

    Stops collecting dependents of the root package once max_results is reached.
    From that point we fill in as much as we can of the graph without doing more
    repoquery calls.

    Returns:
        Dictionary mapping package names to dictionaries containing 'dependents' list and 'partial' flag
    """
    logging.debug(f"🔄 Building dependents graph for: {root_package}")
    logging.debug(f"   Show source packages: {show_source_packages}")
    logging.debug(f"   Max results: {max_results}")
    logging.debug(f"   Filter command: {filter_command}")

    known_packages: Set[str] = {root_package}
    queue = deque([root_package])
    dependents_map: Dict[str, Dict[str, Any]] = {}
    result_count = 0

    result_limit_hit = False
    while queue:
        package = queue.popleft()
        dependents_list: List[str] = []

        any_filtered_dependents = False
        for dependent in generate_direct_dependents(
                package, repository_paths, metrics, dependency_cache, verbose,
                cache_only=result_limit_hit
        ):
            if show_source_packages:
                dependent = query_source_package(
                    dependent,
                    repository_paths,
                    metrics,
                    source_cache,
                    verbose,
                    allow_missing
                )

            if not dependent:
                continue
            if not keep_cycles and dependent in known_packages:
                continue

            if dependent not in known_packages:
                known_packages.add(dependent)
                queue.append(dependent)

            dependent_is_filtered = filter_command and not run_filter_command(
                dependent,
                filter_command,
                metrics,
                filter_cache,
                verbose
            )

            if not dependent_is_filtered:
                dependents_list.append(dependent)

                if package == root_package and max_results is not None:
                    result_count += 1
                    if result_count >= max_results:
                        result_limit_hit = True
                        break
            else:
                any_filtered_dependents = True

        dependents_map[package] = {"dependents": dependents_list, "partial": result_limit_hit or any_filtered_dependents}

    for package, entry in dependents_map.items():
        has_unknown_dependents = any(dependent not in dependents_map for dependent in entry["dependents"])
        has_partial_dependents = any(not dependency_cache.has_all(dependent) for dependent in entry["dependents"])
        entry["partial"] = entry["partial"] or has_unknown_dependents or has_partial_dependents

    def filter_function(package_name: str, dependents_list: List[str]) -> bool:
        if max_results is not None and len(dependents_list) >= max_results:
            return False

        if not filter_command:
            return True

        return filter_cache.get(package_name) is not False

    # We add 1 to max_results to make room for the root package
    if max_results is not None:
        max_results += 1

    dependents_graph = compute_transitive_closure(root_package, dependents_map, max_results, filter_function)

    if not dependents_graph.get(root_package):
        raise NoDependentsFoundError(root_package)

    return dependents_graph


def max_result_type(value: str) -> int:
    """
    Convert a string to an integer, failing if the value is not a positive integer.
    """
    try:
        result = int(value)
    except ValueError:
        result = -1

    if result <= 0:
        raise argparse.ArgumentTypeError(f"result limit must be positive whole number, got: {value}")

    return result


def parse_command_line_arguments() -> argparse.Namespace:
    """
    Parse command line arguments for the package dependents finder.

    Returns:
        argparse.Namespace: Parsed command line arguments
    """
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
            "or full repository URLs.\\n"
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
        "--max-results",
        type=max_result_type,
        help="Maximum number of results to return (limits both queries and output)"
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

    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Skip dnf cache update and use existing cache only"
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print detailed statistics about repoquery calls and cache usage"
    )
    parser.add_argument(
        "--show-cycles",
        action="store_true",
        help="Show cycles in dependency graph"
    )
    parser.add_argument(
        "--filter-command",
        help="Optional shell command to run on each dependent package to filter results. "
             "The command receives PACKAGE environment variable set to the package name. "
             "If the command returns a non-zero exit code, the package is pruned from output. "
             "Example: --filter-command 'echo $PACKAGE | grep -q \"^kernel$\"'"
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Include package descriptions in output. For plain format, descriptions are appended to package names. "
             "For JSON format, descriptions are added as a 'description' field to each package object."
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Redirect all log output to this file instead of stderr"
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Allow missing packages to be non-fatal to operation. "
             "If a package is not found in repositories, continue with empty results instead of exiting with error."
    )
    return parser.parse_args()


def build_repository_paths(
        base_url: str,
        repository_names: str,
        arch: str
    ) -> Dict[str, str]:
    """
    Build repository paths from base URL and repository names.

    Args:
        base_url: Base URL for the repositories
        repository_names: Comma-separated list of repository names or full URLs
        arch: CPU architecture (e.g., 'x86_64', 'aarch64')

    Returns:
        Dictionary mapping repository IDs to their full URLs

    Raises:
        SystemExit: If no valid repositories are provided
    """
    repositories = [
        repo.strip() for repo in repository_names.split(",")
        if repo.strip()
    ]
    if not repositories:
        logging.error("At least one repository alias or URL must be provided")
        sys.exit(EXIT_INVALID_ARGUMENTS)

    base = base_url.rstrip("/")
    paths: Dict[str, str] = {}
    for repository in repositories:
        if repository.startswith(("http://", "https://")):
            repository_url = repository.rstrip("/")
            repository_id = derive_repository_id_from_url(repository_url)
            paths[repository_id] = repository_url
            continue

        repository_id = repository
        repository_url = f"{base}/compose/{repository}/{arch}/os/"
        paths[repository_id] = repository_url

        repository_id = repository + "-sources"
        repository_url = f"{base}/compose/{repository}/source/tree/"
        paths[repository_id] = repository_url
    return paths


def set_up_logging(verbose: bool, log_file: Path | None) -> None:
    """
    Set up logging configuration.

    Args:
        verbose: Whether to enable debug logging
        log_file: Optional file path to redirect logs to
    """
    level = logging.DEBUG if verbose else logging.INFO

    if log_file:
        logger = logging.getLogger()
        logger.setLevel(level)

        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(file_handler)

        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.ERROR)
        stderr_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(stderr_handler)
    else:
        logging.basicConfig(format="%(message)s", level=level)


def log_operation(
        package_name: str,
        all_dependents: bool,
        source_packages: bool,
        max_results: int | None,
        filter_command: str | None,
        output_file: Path | None
    ) -> None:
    """
    Log information about the operation being performed.

    Args:
        package_name: Name of the package to inspect
        all_dependents: Whether to include transitive dependencies
        source_packages: Whether to convert to source package names
        max_results: Maximum number of results to return
        filter_command: Optional shell command to filter packages
        output_file: Optional output file path
    """
    operation = "transitive" if all_dependents else "direct"
    package_type = "as source packages" if source_packages else ""
    max_info = f" (max {max_results})" if max_results else ""
    filter_info = f" with filter: {filter_command}" if filter_command else ""
    output_info = f" to \"{output_file}\"" if output_file else ""

    logging.info(
        f"\n🔍 Finding {operation} reverse dependencies of \"{package_name}\" "
        f"{package_type}{max_info}{filter_info}{output_info}\n"
    )


def set_up_repositories_and_cache(
        base_url: str,
        repository_names: str,
        arch: str,
        no_refresh: bool,
        verbose: bool
    ) -> Dict[str, str]:
    """
    Set up repositories and update dnf cache if needed.

    Args:
        base_url: Base URL for nightly repositories
        repository_names: Comma-separated list of repository names
        arch: CPU architecture
        no_refresh: Whether to skip dnf cache update
        verbose: Whether to enable verbose logging

    Returns:
        Dictionary mapping repository IDs to URLs
    """
    repositories = build_repository_paths(base_url, repository_names, arch)

    if not no_refresh:
        update_dnf_cache(repositories, verbose)
    else:
        logging.info("⏭️  Skipping dnf cache update (using existing cache)")

    return repositories


def collect_package_descriptions(
        arguments: argparse.Namespace,
        repositories: Dict[str, str],
        metrics: RepoQueryMetrics,
        dependents_data: List[Dict[str, Any]] | List[str]
    ) -> Dict[str, str]:
    """
    Collect package descriptions for all relevant packages.

    Args:
        arguments: Parsed command line arguments
        repositories: Dictionary mapping repository IDs to URLs
        metrics: Metrics object to track repoquery calls
        dependents_data: Either a list of package dictionaries (for --all) or a list of strings (for direct only)

    Returns:
        Dictionary mapping package names to their descriptions
    """
    package_descriptions = {}

    if arguments.all:
        all_packages = set()
        for package_entry in dependents_data:
            all_packages.add(package_entry["package"])
            all_packages.update(package_entry["dependents"])

        for package in all_packages:
            description = query_package_description(
                package, repositories, metrics, arguments.verbose
            )
            package_descriptions[package] = description
    else:
        all_packages_to_describe = [arguments.package_name] + dependents_data
        for package in all_packages_to_describe:
            description = query_package_description(
                package, repositories, metrics, arguments.verbose
            )
            package_descriptions[package] = description

    return package_descriptions


def generate_output(
        arguments: argparse.Namespace,
        dependents_data: List[Dict[str, Any]] | List[str],
        package_descriptions: Dict[str, str] | None
    ) -> str:
    """
    Generate output in the requested format.

    Args:
        arguments: Parsed command line arguments
        dependents_data: Either a list of package dictionaries (for --all) or a list of strings (for direct only)
        package_descriptions: Dictionary of package descriptions if --describe is used, None otherwise

    Returns:
        Formatted output string
    """
    if arguments.format == "json":
        return generate_json_output(arguments, dependents_data, package_descriptions)
    else:
        return generate_plain_output(arguments, dependents_data, package_descriptions)


def generate_json_output(
        arguments: argparse.Namespace,
        dependents_data: List[Dict[str, Any]] | List[str],
        package_descriptions: Dict[str, str] | None
    ) -> str:
    """
    Generate JSON formatted output.

    Args:
        arguments: Parsed command line arguments
        dependents_data: Either a list of package dictionaries (for --all) or a list of strings (for direct only)
        package_descriptions: Dictionary of package descriptions if --describe is used, None otherwise

    Returns:
        JSON formatted string
    """
    if arguments.all:
        output_array = []
        for package_entry in dependents_data:
            package_obj = {"package": package_entry["package"]}
            if arguments.describe and package_descriptions:
                description = package_descriptions.get(package_entry["package"])
                if description:
                    package_obj["description"] = description
            package_obj["dependents"] = package_entry["dependents"]
            if "partial" in package_entry:
                package_obj["partial"] = package_entry["partial"]
            output_array.append(package_obj)
    else:
        output_array = [
            {"package": arguments.package_name, "dependents": dependents_data}
        ]
        if arguments.describe and package_descriptions:
            description = package_descriptions.get(arguments.package_name)
            if description:
                output_array[0]["description"] = description

    return json.dumps(output_array, indent=2)


def generate_plain_output(
        arguments: argparse.Namespace,
        dependents_data: List[Dict[str, Any]] | List[str],
        package_descriptions: Dict[str, str] | None
    ) -> str:
    """
    Generate plain text formatted output.

    Args:
        arguments: Parsed command line arguments
        dependents_data: Either a list of package dictionaries (for --all) or a list of strings (for direct only)
        package_descriptions: Dictionary of package descriptions if --describe is used, None otherwise

    Returns:
        Plain text formatted string
    """
    if arguments.all:
        # Find the root package entry
        root_package_entry = None
        for package_entry in dependents_data:
            if package_entry["package"] == arguments.package_name:
                root_package_entry = package_entry
                break

        if root_package_entry:
            collected_packages = root_package_entry["dependents"]
        else:
            collected_packages = []
    else:
        collected_packages = dependents_data

    if arguments.describe and package_descriptions:
        output_lines = []
        for package in collected_packages:
            description = package_descriptions.get(package)
            if description:
                output_lines.append(f"{package}: {description}")
            else:
                output_lines.append(package)
        return "\n".join(output_lines)
    else:
        return "\n".join(collected_packages)


def write_output(output_data: str, output_file: Path | None) -> None:
    """
    Write output to file or stdout.

    Args:
        output_data: The formatted output data
        output_file: Optional output file path
    """
    logging.debug(f"\n✅ Final output:\n{output_data}\n")
    if output_file:
        output_file.write_text(output_data)
    else:
        print(output_data)


def display_statistics(
        filter_command: str | None,
        metrics: RepoQueryMetrics,
        source_cache: SourcePackageCache,
        filter_cache: FilterCache,
        dependency_cache: DependencyCache
    ) -> None:
    """
    Display detailed statistics about the operation.

    Args:
        filter_command: Optional shell command used to filter packages
        metrics: Metrics object containing operation statistics
        source_cache: Cache object containing source package cache statistics
        filter_cache: Cache object containing filter cache statistics
    """
    stats = metrics.get_stats()
    print("\n📊 FINAL STATISTICS:", file=sys.stderr)
    print(f"   Total dnf repoquery calls: {stats['total_calls']}", file=sys.stderr)
    print("   Calls by type:", file=sys.stderr)
    for call_type, count in stats["calls_by_type"].items():
        print(f"     {call_type}: {count}", file=sys.stderr)

    if filter_command:
        print(f"   Filter command calls: {stats['filter_calls']}", file=sys.stderr)
        print(f"   Filter command failures: {stats['filter_failures']}", file=sys.stderr)

    source_cache_stats = source_cache.get_stats()
    print(f"   Source package cache size: {source_cache_stats['cache_size']}", file=sys.stderr)
    print(f"   Source package cache hits (found): {source_cache_stats['found_count']}", file=sys.stderr)
    print(f"   Source package cache hits (not found): {source_cache_stats['not_found_count']}", file=sys.stderr)

    if source_cache_stats["cached_packages"]:
        print("   Cached source packages:", file=sys.stderr)
        for package in source_cache_stats["cached_packages"]:
            result = source_cache.get(package)
            if result is None:
                status = "not found"
            else:
                status = f"→ {result}"
            print(f"     {package}: {status}", file=sys.stderr)

    filter_cache_stats = filter_cache.get_stats()
    print(f"   Filter cache size: {filter_cache_stats['cache_size']}", file=sys.stderr)
    print(f"   Filter cache hits (passed): {filter_cache_stats['passed_count']}", file=sys.stderr)
    print(f"   Filter cache hits (failed): {filter_cache_stats['failed_count']}", file=sys.stderr)

    if filter_cache_stats["cached_packages"]:
        print("   Cached filter results:", file=sys.stderr)
        for package in filter_cache_stats["cached_packages"]:
            result = filter_cache.get(package)
            status = "pass" if result else "fail"
            print(f"     {package}: {status}", file=sys.stderr)

    dependency_cache_stats = dependency_cache.get_stats()
    print(f"   Dependency cache size: {dependency_cache_stats['cache_size']}", file=sys.stderr)
    print(f"   Dependency cache total dependents: {dependency_cache_stats['total_dependents']}", file=sys.stderr)
    print(f"   Dependency cache complete results: {dependency_cache_stats['complete_count']}", file=sys.stderr)
    print(f"   Dependency cache partial results: {dependency_cache_stats['partial_count']}", file=sys.stderr)

    if dependency_cache_stats["cached_packages"]:
        print("   Cached dependency results:", file=sys.stderr)
        for package in dependency_cache_stats["cached_packages"]:
            dependents = dependency_cache.get(package)
            if dependents is not None:
                entry = dependency_cache._cache[package]
                partial_info = " (partial)" if entry["partial"] else " (complete)"
                print(f"     {package}: {len(dependents)} dependents{partial_info}", file=sys.stderr)


def main() -> None:
    """
    Main entry point for the package dependents finder.

    Parses command line arguments, sets up repositories, and finds package dependents
    according to the specified options. Outputs results in the requested format.

    Raises:
        SystemExit: On argument validation errors or RepoQueryError
    """
    arguments = parse_command_line_arguments()

    set_up_logging(arguments.verbose, arguments.log_file)

    log_operation(
        arguments.package_name,
        arguments.all,
        arguments.source_packages,
        arguments.max_results,
        arguments.filter_command,
        arguments.output_file
    )

    repositories = set_up_repositories_and_cache(
        arguments.base_url,
        arguments.repository_names,
        arguments.arch,
        arguments.no_refresh,
        arguments.verbose
    )

    metrics = RepoQueryMetrics()
    source_cache = SourcePackageCache()
    filter_cache = FilterCache()
    dependency_cache = DependencyCache()

    try:
        if arguments.all:
            dependents_graph = build_dependents_graph(
                arguments.package_name,
                repositories,
                show_source_packages=arguments.source_packages,
                source_cache=source_cache,
                metrics=metrics,
                filter_cache=filter_cache,
                dependency_cache=dependency_cache,
                max_results=arguments.max_results,
                verbose=arguments.verbose,
                keep_cycles=arguments.show_cycles,
                filter_command=arguments.filter_command,
                allow_missing=arguments.allow_missing,
            )

            dependents_data = []
            for package, entry in dependents_graph.items():
                package_entry = {
                    "package": package,
                    "dependents": entry["dependents"],
                    "partial": entry["partial"]
                }
                dependents_data.append(package_entry)
        else:
            dependents_data = build_dependents_list(
                arguments.package_name,
                repositories,
                show_source_packages=arguments.source_packages,
                source_cache=source_cache,
                metrics=metrics,
                filter_cache=filter_cache,
                dependency_cache=dependency_cache,
                max_results=arguments.max_results,
                verbose=arguments.verbose,
                keep_cycles=arguments.show_cycles,
                filter_command=arguments.filter_command,
                allow_missing=arguments.allow_missing,
            )

        package_descriptions = None
        if arguments.describe:
            logging.debug("🔄 Fetching package descriptions...")
            package_descriptions = collect_package_descriptions(
                arguments, repositories, metrics, dependents_data
            )

        output_data = generate_output(arguments, dependents_data, package_descriptions)
        write_output(output_data, arguments.output_file)

        if arguments.stats:
            display_statistics(arguments.filter_command, metrics, source_cache, filter_cache, dependency_cache)

    except RepoQueryError as error:
        logging.error("%s", error)
        sys.exit(error.exit_code)
    except NoDependentsFoundError as error:
        if arguments.allow_missing:
            logging.info("%s (continuing with empty results due to --allow-missing)", error)
            if arguments.all:
                dependents_data = [{"package": arguments.package_name, "dependents": [], "partial": False}]
            else:
                dependents_data = []
        else:
            logging.error("%s", error)
            sys.exit(error.exit_code)
    except PackageNotFoundError as error:
        if arguments.allow_missing:
            logging.info("%s (continuing with empty results due to --allow-missing)", error)
            if arguments.all:
                dependents_data = [{"package": arguments.package_name, "dependents": [], "partial": False}]
            else:
                dependents_data = []
        else:
            logging.error("%s", f"Could not query dependents for {arguments.package_name} because repositories are incomplete (at least the {error.package_name} package is missing)")
            sys.exit(error.exit_code)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
