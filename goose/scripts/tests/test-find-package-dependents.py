
"""
Tests for find-package-dependents.py script.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util
spec = importlib.util.spec_from_file_location("find_package_dependents", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "find-package-dependents.py"))
find_package_dependents = importlib.util.module_from_spec(spec)
spec.loader.exec_module(find_package_dependents)


@contextmanager
def captured_stdout():
    """Context manager to capture stdout output using StringIO."""
    old_stdout = sys.stdout
    captured = StringIO()
    sys.stdout = captured
    try:
        yield captured
    finally:
        sys.stdout = old_stdout


@contextmanager
def captured_stderr():
    """Context manager to capture stderr output using StringIO."""
    old_stderr = sys.stderr
    captured = StringIO()
    sys.stderr = captured
    try:
        yield captured
    finally:
        sys.stderr = old_stderr


class TestFindPackageDependents(unittest.TestCase):
    """🧪 Comprehensive test suite for find-package-dependents.py script.

    Tests all functionality with mocked dnf commands to ensure the script works correctly
    without requiring actual system packages or network access.
    """

    def setUp(self):
        """🎯 Set up test fixtures with realistic package examples."""
        import logging
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(message)s',
            force=True
        )

        self.repositories = {
            "BaseOS": "http://example.com/repo/BaseOS/x86_64/os/",
            "AppStream": "http://example.com/repo/AppStream/x86_64/os/",
            "BaseOS-sources": "http://example.com/repo/BaseOS/source/tree/",
            "AppStream-sources": "http://example.com/repo/AppStream/source/tree/",
        }

        self.metrics = find_package_dependents.RepoQueryMetrics()
        self.source_cache = find_package_dependents.SourcePackageCache()
        self.filter_cache = find_package_dependents.FilterCache()
        self.dependency_cache = find_package_dependents.DependencyCache()


    def test_source_package_cache(self):
        """Test that source package cache correctly stores and retrieves package mappings."""
        cache = find_package_dependents.SourcePackageCache()

        cache.set("podman", "podman-source")
        self.assertEqual(cache.get("podman"), "podman-source")

        cache.set("non-existent-package", None)
        self.assertIsNone(cache.get("non-existent-package"))

        self.assertIsNone(cache.get("missing-package"))

        stats = cache.get_stats()
        self.assertEqual(stats["cache_size"], 2)
        self.assertEqual(stats["found_count"], 1)
        self.assertEqual(stats["not_found_count"], 1)

    def test_filter_cache(self):
        """Test that filter cache correctly stores filter results for packages."""
        cache = find_package_dependents.FilterCache()

        cache.set("bootc", True)
        cache.set("toolbox", False)

        self.assertTrue(cache.get("bootc"))
        self.assertFalse(cache.get("toolbox"))
        self.assertIsNone(cache.get("non-existent"))

        stats = cache.get_stats()
        self.assertEqual(stats["cache_size"], 2)
        self.assertEqual(stats["passed_count"], 1)
        self.assertEqual(stats["failed_count"], 1)

    def test_dependency_cache(self):
        """Test that dependency cache correctly stores package dependency lists."""
        cache = find_package_dependents.DependencyCache()

        dependents = ["bootc", "toolbox", "container-tools"]
        cache.set("podman", dependents)

        cached_dependents = cache.get("podman")
        self.assertEqual(cached_dependents, dependents)
        self.assertIsNone(cache.get("non-existent"))

        stats = cache.get_stats()
        self.assertEqual(stats["cache_size"], 1)
        self.assertEqual(stats["total_dependents"], 3)

    def test_repo_query_metrics(self):
        """Test that repository query metrics correctly track API calls and performance."""
        metrics = find_package_dependents.RepoQueryMetrics()

        metrics.log_call("test_call", "podman")
        metrics.log_call("test_call", "libwayland-server")
        metrics.log_call("different_call", "mutter")

        metrics.log_filter_call("bootc", True)
        metrics.log_filter_call("toolbox", False)

        stats = metrics.get_stats()
        self.assertEqual(stats["total_calls"], 3)
        self.assertEqual(stats["calls_by_type"]["test_call"], 2)
        self.assertEqual(stats["calls_by_type"]["different_call"], 1)
        self.assertEqual(stats["filter_calls"], 2)
        self.assertEqual(stats["filter_failures"], 1)


    def test_derive_repository_id_from_url(self):
        """Test that repository IDs are correctly derived from repository URLs."""
        url = "http://download.devel.redhat.com/rhel-10/nightly/RHEL-10/latest-RHEL-10/compose/BaseOS/x86_64/os/"
        repo_id = find_package_dependents.derive_repository_id_from_url(url)
        self.assertIsInstance(repo_id, str)
        self.assertIn("BaseOS_", repo_id)

        url = "https://custom.repo.com/special/repo/"
        repo_id = find_package_dependents.derive_repository_id_from_url(url)
        self.assertIsInstance(repo_id, str)
        self.assertIn("repo_", repo_id)

        url = "http://example.com/os/"
        repo_id = find_package_dependents.derive_repository_id_from_url(url)
        self.assertIsInstance(repo_id, str)
        self.assertIn("example_com_", repo_id)

    def test_get_signal_name(self):
        """Test that signal names are correctly retrieved from signal numbers."""
        signal_name = find_package_dependents.get_signal_name(9)
        self.assertEqual(signal_name, "SIGKILL")

        signal_name = find_package_dependents.get_signal_name(99999)
        self.assertEqual(signal_name, "SIG99999")

    def test_quote_command(self):
        """Test that command arguments are properly quoted for shell execution."""
        args = ["echo", "hello", "world"]
        result = find_package_dependents.quote_command(args)
        self.assertEqual(result, "echo hello world")

        args = ["echo", "hello world", "with spaces"]
        result = find_package_dependents.quote_command(args)
        self.assertIn("'hello world'", result)
        self.assertIn("'with spaces'", result)

        args = ["echo", "", "test"]
        result = find_package_dependents.quote_command(args)
        self.assertIn("''", result)

    def test_max_result_type(self):
        """Test max result type validation with various inputs."""
        test_cases = [
            ("10", 10, False),
            ("1", 1, False),
            ("100", 100, False),
            ("0", None, True),
            ("-1", None, True),
            ("invalid", None, True),
            ("", None, True),
        ]

        for input_value, expected, should_raise in test_cases:
            with self.subTest(input_value=input_value, expected=expected, should_raise=should_raise):
                if should_raise:
                    with self.assertRaises(argparse.ArgumentTypeError):
                        find_package_dependents.max_result_type(input_value)
                else:
                    result = find_package_dependents.max_result_type(input_value)
                    self.assertEqual(result, expected)


    @patch.object(find_package_dependents, 'run_command')
    def test_dnf_command_execution(self, mock_run_command):
        """Test that DNF commands are properly constructed and executed with repository configuration."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "bootc-1.0.0-1.x86_64\ncontainer-tools-2.0.0-1.x86_64"
        }

        result = find_package_dependents.dnf(
            "repoquery --whatdepends podman",
            self.repositories,
            verbose=True
        )

        mock_run_command.assert_called_once()
        call_args = mock_run_command.call_args[0][0]
        self.assertIn("dnf", call_args)
        self.assertIn("--disablerepo=*", call_args)
        self.assertIn("--cacheonly", call_args)
        self.assertIn("--repofrompath=repo-BaseOS,http://example.com/repo/BaseOS/x86_64/os/", call_args)
        self.assertIn("--enablerepo=repo-BaseOS", call_args)

    @patch.object(find_package_dependents, 'run_command')
    def test_generate_direct_dependents(self, mock_run_command):
        """Test that direct package dependents are correctly identified and returned."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "bootc\ntoolbox\ncontainer-tools"
        }

        dependents = list(find_package_dependents.generate_direct_dependents(
            "podman",
            self.repositories,
            self.metrics,
            self.dependency_cache,
            verbose=True
        ))

        self.assertEqual(len(dependents), 3)
        self.assertIn("bootc", dependents)
        self.assertIn("toolbox", dependents)
        self.assertIn("container-tools", dependents)

        cached_dependents = list(find_package_dependents.generate_direct_dependents(
            "podman",
            self.repositories,
            self.metrics,
            self.dependency_cache,
            verbose=True,
            cache_only=True
        ))

        self.assertEqual(cached_dependents, dependents)

    @patch.object(find_package_dependents, 'run_command')
    def test_query_source_package(self, mock_run_command):
        """Test that source package names are correctly queried from binary package names."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "podman-1.0.0-1.src.rpm"
        }

        source_package = find_package_dependents.query_source_package(
            "podman",
            self.repositories,
            self.metrics,
            self.source_cache,
            verbose=True
        )

        self.assertEqual(source_package, "podman")

        cached_source = find_package_dependents.query_source_package(
            "podman",
            self.repositories,
            self.metrics,
            self.source_cache,
            verbose=True
        )

        self.assertEqual(cached_source, "podman")

    @patch.object(find_package_dependents, 'run_command')
    def test_query_source_not_found(self, mock_run_command):
        """Test that missing packages are properly handled with appropriate error messages."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": ""
        }

        with self.assertRaises(find_package_dependents.PackageNotFoundError):
            find_package_dependents.query_source_package(
                "non-existent-package",
                self.repositories,
                self.metrics,
                self.source_cache,
                verbose=False
            )

    @patch.object(find_package_dependents, 'run_command')
    def test_query_source_allow_missing(self, mock_run_command):
        """Test that missing packages are gracefully handled when allow_missing is enabled."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": ""
        }

        source_package = find_package_dependents.query_source_package(
            "non-existent-package",
            self.repositories,
            self.metrics,
            self.source_cache,
            verbose=False,
            allow_missing=True
        )

        self.assertEqual(source_package, "")

    @patch.object(find_package_dependents, 'run_command')
    def test_query_package_description(self, mock_run_command):
        """Test that package descriptions are correctly retrieved and formatted."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "This is a container engine\nwith multiple lines\nof description"
        }

        description = find_package_dependents.query_package_description(
            "podman",
            self.repositories,
            self.metrics,
            verbose=False
        )

        self.assertEqual(description, "This is a container engine with multiple lines of description")

    @patch.object(find_package_dependents, 'run_command')
    def test_run_filter_command(self, mock_run_command):
        """Test that filter commands are correctly executed on package names."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "filtered output"
        }

        result = find_package_dependents.run_filter_command(
            "bootc",
            "echo $PACKAGE | grep -q bootc",
            self.metrics,
            self.filter_cache,
            verbose=False
        )

        self.assertTrue(result)

        cached_result = find_package_dependents.run_filter_command(
            "bootc",
            "echo $PACKAGE | grep -q bootc",
            self.metrics,
            self.filter_cache,
            verbose=False
        )

        self.assertTrue(cached_result)

    @patch.object(find_package_dependents, 'run_command')
    def test_filter_command_failure(self, mock_run_command):
        """Test that failed filter commands are properly handled."""
        mock_run_command.return_value = {
            "return_code": 1,
            "output": "filter failed"
        }

        result = find_package_dependents.run_filter_command(
            "toolbox",
            "echo $PACKAGE | grep -q nonexistent",
            self.metrics,
            self.filter_cache,
            verbose=False
        )

        self.assertFalse(result)

    def test_filter_command_empty(self):
        """Test that empty filter commands are treated as passing."""
        result = find_package_dependents.run_filter_command(
            "container-tools",
            "",
            self.metrics,
            self.filter_cache,
            verbose=False
        )

        self.assertTrue(result)

    @patch.object(find_package_dependents, 'run_command')
    def test_update_dnf_cache(self, mock_run_command):
        """Test that DNF cache is successfully updated."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "Cache updated successfully"
        }

        find_package_dependents.update_dnf_cache(self.repositories, verbose=False)

        mock_run_command.assert_called_once()

    @patch.object(find_package_dependents, 'run_command')
    def test_dnf_cache_failure(self, mock_run_command):
        """Test that DNF cache update failures are properly handled with exceptions."""
        mock_run_command.side_effect = subprocess.CalledProcessError(
            1, "dnf makecache", "Cache update failed", "Error message"
        )

        with self.assertRaises(find_package_dependents.RepoQueryError):
            find_package_dependents.update_dnf_cache(self.repositories, verbose=False)


    def test_convert_to_source_packages(self):
        """Test that binary packages are correctly converted to their source package equivalents."""
        with patch.object(find_package_dependents, 'query_source_package') as mock_query:
            mock_query.side_effect = lambda pkg, *args, **kwargs: f"{pkg}-source" if pkg != "not-found" else ""

            def binary_packages():
                yield "bootc"
                yield "toolbox"
                yield "not-found"
                yield "container-tools"

            source_packages = list(find_package_dependents.convert_to_source_packages(
                binary_packages(),
                self.repositories,
                self.metrics,
                self.source_cache,
                self.filter_cache,
                max_results=5,
                verbose=False
            ))

            self.assertEqual(len(source_packages), 3)
            self.assertIn("bootc-source", source_packages)
            self.assertIn("toolbox-source", source_packages)
            self.assertIn("container-tools-source", source_packages)

    def test_convert_to_source_with_filter(self):
        """Test that source package conversion respects filter commands."""
        with patch.object(find_package_dependents, 'query_source_package') as mock_query:
            mock_query.side_effect = lambda pkg, *args, **kwargs: f"{pkg}-source"

            with patch.object(find_package_dependents, 'run_filter_command') as mock_filter:
                mock_filter.side_effect = lambda pkg, cmd, *args, **kwargs: pkg != "toolbox-source"

                def binary_packages():
                    yield "bootc"
                    yield "toolbox"
                    yield "container-tools"

                source_packages = list(find_package_dependents.convert_to_source_packages(
                    binary_packages(),
                    self.repositories,
                    self.metrics,
                    self.source_cache,
                    self.filter_cache,
                    filter_command="test filter",
                    verbose=False
                ))

                self.assertEqual(len(source_packages), 2)
                self.assertIn("bootc-source", source_packages)
                self.assertIn("container-tools-source", source_packages)

    def test_build_dependents_list(self):
        """Test that complete lists of package dependents are correctly built."""
        with patch.object(find_package_dependents, 'generate_direct_dependents') as mock_generate:
            mock_generate.return_value = iter(["bootc", "toolbox", "container-tools"])

            with patch.object(find_package_dependents, 'run_filter_command') as mock_filter:
                mock_filter.return_value = True

                dependents = find_package_dependents.build_dependents_list(
                    "podman",
                    self.repositories,
                    show_source_packages=False,
                    source_cache=self.source_cache,
                    metrics=self.metrics,
                    filter_cache=self.filter_cache,
                    dependency_cache=self.dependency_cache,
                    verbose=False
                )

                self.assertEqual(len(dependents), 3)
                self.assertIn("bootc", dependents)
                self.assertIn("toolbox", dependents)
                self.assertIn("container-tools", dependents)

    def test_build_dependents_with_max_results(self):
        """Test that dependents lists respect maximum result limits."""
        with patch.object(find_package_dependents, 'generate_direct_dependents') as mock_generate:
            mock_generate.return_value = iter(["bootc", "toolbox", "container-tools", "skopeo", "buildah"])

            with patch.object(find_package_dependents, 'run_filter_command') as mock_filter:
                mock_filter.return_value = True

                dependents = find_package_dependents.build_dependents_list(
                    "podman",
                    self.repositories,
                    show_source_packages=False,
                    source_cache=self.source_cache,
                    metrics=self.metrics,
                    filter_cache=self.filter_cache,
                    dependency_cache=self.dependency_cache,
                    max_results=3,
                    verbose=False
                )

                self.assertEqual(len(dependents), 3)
                self.assertIn("bootc", dependents)
                self.assertIn("toolbox", dependents)

    def test_build_dependents_no_dependents(self):
        """Test that appropriate errors are raised when no dependents are found."""
        with patch.object(find_package_dependents, 'generate_direct_dependents') as mock_generate:
            mock_generate.return_value = iter([])

            with self.assertRaises(find_package_dependents.NoDependentsFoundError):
                find_package_dependents.build_dependents_list(
                    "podman",
                    self.repositories,
                    show_source_packages=False,
                    source_cache=self.source_cache,
                    metrics=self.metrics,
                    filter_cache=self.filter_cache,
                    dependency_cache=self.dependency_cache,
                    verbose=False
                )

    def test_build_dependents_list_with_source_packages(self):
        """Test that build_dependents_list correctly converts to source packages when show_source_packages=True."""
        with patch.object(find_package_dependents, 'generate_direct_dependents') as mock_generate:
            mock_generate.return_value = iter(["bootc", "toolbox", "container-tools"])

            with patch.object(find_package_dependents, 'convert_to_source_packages') as mock_convert:
                mock_convert.return_value = iter(["bootc-src", "toolbox-src", "container-tools-src"])

                dependents = find_package_dependents.build_dependents_list(
                    "podman",
                    self.repositories,
                    show_source_packages=True,  # This is the key parameter
                    source_cache=self.source_cache,
                    metrics=self.metrics,
                    filter_cache=self.filter_cache,
                    dependency_cache=self.dependency_cache,
                    verbose=False
                )

                self.assertEqual(len(dependents), 3)
                self.assertIn("bootc-src", dependents)
                self.assertIn("toolbox-src", dependents)
                self.assertIn("container-tools-src", dependents)

                mock_convert.assert_called_once()

    def test_build_dependents_list_without_source_packages(self):
        """Test that build_dependents_list returns binary package names when show_source_packages=False."""
        with patch.object(find_package_dependents, 'generate_direct_dependents') as mock_generate:
            mock_generate.return_value = iter(["bootc", "toolbox", "container-tools"])

            with patch.object(find_package_dependents, 'convert_to_source_packages') as mock_convert:
                dependents = find_package_dependents.build_dependents_list(
                    "podman",
                    self.repositories,
                    show_source_packages=False,  # Default behavior
                    source_cache=self.source_cache,
                    metrics=self.metrics,
                    filter_cache=self.filter_cache,
                    dependency_cache=self.dependency_cache,
                    verbose=False
                )

                self.assertEqual(len(dependents), 3)
                self.assertIn("bootc", dependents)
                self.assertIn("toolbox", dependents)
                self.assertIn("container-tools", dependents)

                mock_convert.assert_not_called()

    def test_compute_transitive_closure(self):
        """Test that transitive dependency relationships are correctly computed."""
        dependents_map = {
            "libwayland-server": {"dependents": ["mutter", "weston"], "partial": False},
            "mutter": {"dependents": ["gnome-shell"], "partial": False},
            "weston": {"dependents": ["kwin"], "partial": False},
            "gnome-shell": {"dependents": [], "partial": False},
        }

        result = find_package_dependents.compute_transitive_closure("libwayland-server", dependents_map)

        self.assertIn("libwayland-server", result)
        self.assertIn("mutter", result)
        self.assertIn("weston", result)
        self.assertIn("gnome-shell", result)

        root_dependents = result["libwayland-server"]["dependents"]
        self.assertIn("mutter", root_dependents)
        self.assertIn("weston", root_dependents)
        self.assertIn("gnome-shell", root_dependents)

    def test_transitive_closure_with_filter(self):
        """Test that transitive closure computation respects filter functions."""
        dependents_map = {
            "libwayland-server": {"dependents": ["mutter", "weston"], "partial": False},
            "mutter": {"dependents": ["gnome-shell"], "partial": False},
            "weston": {"dependents": ["kwin"], "partial": False},
            "gnome-shell": {"dependents": [], "partial": False},
        }

        def filter_function(package, dependents):
            if package == "weston":
                return False
            return True

        result = find_package_dependents.compute_transitive_closure("libwayland-server", dependents_map, filter_function=filter_function)

        self.assertIn("libwayland-server", result)
        self.assertIn("mutter", result)
        self.assertIn("weston", result)
        self.assertIn("gnome-shell", result)

        root_dependents = result["libwayland-server"]["dependents"]
        self.assertIn("mutter", root_dependents)
        self.assertNotIn("weston", root_dependents)
        self.assertIn("gnome-shell", root_dependents)

    def test_deep_narrow_dependent_tree(self):
        """Test that deep and narrow dependent trees are correctly handled with proper partial flags."""
        dependents_map = {
            "libwayland-server": {"dependents": ["mutter"], "partial": False},
            "mutter": {"dependents": ["gnome-shell"], "partial": False},
            "gnome-shell": {"dependents": ["gnome-session"], "partial": False},
            "gnome-session": {"dependents": ["gnome-initial-setup"], "partial": False},
            "gnome-initial-setup": {"dependents": ["gnome-control-center"], "partial": False},
            "gnome-control-center": {"dependents": [], "partial": False},
        }

        result = find_package_dependents.compute_transitive_closure("libwayland-server", dependents_map)

        self.assertIn("libwayland-server", result)
        self.assertIn("mutter", result)
        self.assertIn("gnome-shell", result)
        self.assertIn("gnome-session", result)
        self.assertIn("gnome-initial-setup", result)
        self.assertIn("gnome-control-center", result)

        root_dependents = result["libwayland-server"]["dependents"]
        self.assertIn("mutter", root_dependents)
        self.assertIn("gnome-shell", root_dependents)
        self.assertIn("gnome-session", root_dependents)
        self.assertIn("gnome-initial-setup", root_dependents)
        self.assertIn("gnome-control-center", root_dependents)

        for package_data in result.values():
            self.assertFalse(package_data["partial"])

    def test_deep_tree_max_results_scenarios(self):
        """Test various max-results and partial flag scenarios in deep dependency trees."""
        test_scenarios = [
            {
                "name": "basic_max_results_limit",
                "description": "Test max-results limits creating partial results",
                "dependents_map": {
                    "libwayland-server": {"dependents": ["mutter", "weston"], "partial": True},
                    "mutter": {"dependents": ["gnome-shell"], "partial": False},
                    "weston": {"dependents": ["kwin"], "partial": False},
                    "gnome-shell": {"dependents": ["gnome-session"], "partial": False},
                    "kwin": {"dependents": ["plasma-desktop"], "partial": False},
                    "gnome-session": {"dependents": [], "partial": False},
                    "plasma-desktop": {"dependents": [], "partial": False},
                },
                "filter_func": lambda package, dependents: True if package == "libwayland-server" else len(dependents) < 2,
                "expected_root_partial": True,
                "expected_max_root_dependents": 2,
                "expected_non_partial": ["mutter", "weston", "gnome-shell", "kwin"]
            },
            {
                "name": "non_root_package_limits",
                "description": "Test max-results limits on non-root packages",
                "dependents_map": {
                    "libwayland-server": {"dependents": ["mutter", "weston"], "partial": False},
                    "mutter": {"dependents": ["gnome-shell", "gnome-session", "gnome-control-center"], "partial": True},
                    "weston": {"dependents": ["kwin"], "partial": False},
                    "gnome-shell": {"dependents": ["gnome-initial-setup"], "partial": False},
                    "gnome-session": {"dependents": ["gnome-settings-daemon"], "partial": False},
                    "gnome-control-center": {"dependents": [], "partial": False},
                    "kwin": {"dependents": ["plasma-desktop"], "partial": False},
                    "gnome-initial-setup": {"dependents": [], "partial": False},
                    "gnome-settings-daemon": {"dependents": [], "partial": False},
                    "plasma-desktop": {"dependents": [], "partial": False},
                },
                "filter_func": lambda package, dependents: True if package == "mutter" else len(dependents) < 2,
                "expected_root_partial": True,
                "expected_mutter_partial": True,
                "expected_max_mutter_dependents": 2,
                "expected_non_partial": ["weston", "gnome-shell", "kwin"]
            },
            {
                "name": "multiple_partial_branches",
                "description": "Test multiple branches with different partial states",
                "dependents_map": {
                    "libwayland-server": {"dependents": ["mutter", "weston"], "partial": False},
                    "mutter": {"dependents": ["gnome-shell"], "partial": True},
                    "weston": {"dependents": ["kwin"], "partial": False},
                    "gnome-shell": {"dependents": ["gnome-session"], "partial": False},
                    "kwin": {"dependents": ["plasma-desktop"], "partial": False},
                    "gnome-session": {"dependents": [], "partial": False},
                    "plasma-desktop": {"dependents": [], "partial": False},
                },
                "filter_func": None,  # No filter function
                "expected_root_partial": True,
                "expected_mutter_partial": True,
                "expected_non_partial": ["weston", "gnome-shell", "kwin", "gnome-session", "plasma-desktop"]
            },
            {
                "name": "edge_case_single_dependent",
                "description": "Test edge case with single dependent limit",
                "dependents_map": {
                    "libwayland-server": {"dependents": ["mutter"], "partial": True},
                    "mutter": {"dependents": ["gnome-shell"], "partial": False},
                    "gnome-shell": {"dependents": ["gnome-session"], "partial": False},
                    "gnome-session": {"dependents": [], "partial": False},
                },
                "filter_func": lambda package, dependents: True if package == "libwayland-server" else len(dependents) < 1,
                "expected_root_partial": True,
                "expected_exact_root_dependents": 1,
                "expected_root_has_mutter": True,
                "expected_non_partial": ["mutter", "gnome-shell", "gnome-session"]
            },
            {
                "name": "zero_max_results",
                "description": "Test behavior with zero dependents allowed",
                "dependents_map": {
                    "libwayland-server": {"dependents": [], "partial": True},
                    "mutter": {"dependents": ["gnome-shell"], "partial": False},
                    "gnome-shell": {"dependents": [], "partial": False},
                },
                "filter_func": lambda package, dependents: False if package == "libwayland-server" and len(dependents) > 0 else True,
                "expected_root_partial": True,
                "expected_exact_root_dependents": 0,
                "expected_non_partial": ["mutter", "gnome-shell"]
            }
        ]

        for scenario in test_scenarios:
            with self.subTest(scenario=scenario["name"]):
                result = find_package_dependents.compute_transitive_closure(
                    "libwayland-server",
                    scenario["dependents_map"],
                    filter_function=scenario["filter_func"]
                )

                self.assertEqual(result["libwayland-server"]["partial"], scenario["expected_root_partial"])

                root_dependents = result["libwayland-server"]["dependents"]
                if "expected_max_root_dependents" in scenario:
                    self.assertLessEqual(len(root_dependents), scenario["expected_max_root_dependents"])
                if "expected_exact_root_dependents" in scenario:
                    self.assertEqual(len(root_dependents), scenario["expected_exact_root_dependents"])
                if "expected_root_has_mutter" in scenario:
                    self.assertIn("mutter", root_dependents)

                if "expected_mutter_partial" in scenario:
                    self.assertTrue(result["mutter"]["partial"])
                    if "expected_max_mutter_dependents" in scenario:
                        mutter_dependents = result["mutter"]["dependents"]
                        self.assertLessEqual(len(mutter_dependents), scenario["expected_max_mutter_dependents"])

                if "expected_non_partial" in scenario:
                    for package in scenario["expected_non_partial"]:
                        if package in result:
                            self.assertFalse(result[package]["partial"], f"Package {package} should not be partial")

    def test_json_output_with_partial_flags(self):
        """Test JSON output with mixed partial flags and descriptions."""
        test_cases = [
            {
                "name": "mixed_partial_flags_only",
                "describe": False,
                "package_descriptions": None,
                "expected_has_descriptions": False
            },
            {
                "name": "partial_flags_with_descriptions",
                "describe": True,
                "package_descriptions": {
                    "libwayland-server": "Wayland display server library",
                    "mutter": "GNOME window manager",
                    "weston": "Wayland reference compositor",
                    "gnome-shell": "GNOME desktop shell",
                    "kwin": "KDE window manager"
                },
                "expected_has_descriptions": True
            }
        ]

        for case in test_cases:
            with self.subTest(scenario=case["name"]):
                arguments = Mock()
                arguments.all = True
                arguments.package_name = "libwayland-server"
                arguments.describe = case["describe"]

                dependents_data = [
                    {"package": "libwayland-server", "dependents": ["mutter", "weston"], "partial": True},
                    {"package": "mutter", "dependents": ["gnome-shell"], "partial": False},
                    {"package": "weston", "dependents": ["kwin"], "partial": False},
                    {"package": "gnome-shell", "dependents": [], "partial": False},
                    {"package": "kwin", "dependents": [], "partial": False}
                ]

                output = find_package_dependents.generate_json_output(
                    arguments, dependents_data, case["package_descriptions"]
                )

                parsed = json.loads(output)
                self.assertEqual(len(parsed), 5)

                root_package = next(pkg for pkg in parsed if pkg["package"] == "libwayland-server")
                self.assertTrue(root_package["partial"])

                for pkg_name in ["mutter", "weston", "gnome-shell", "kwin"]:
                    pkg = next(p for p in parsed if p["package"] == pkg_name)
                    self.assertFalse(pkg["partial"])

                if case["expected_has_descriptions"]:
                    self.assertEqual(root_package["description"], "Wayland display server library")
                    mutter_pkg = next(p for p in parsed if p["package"] == "mutter")
                    self.assertEqual(mutter_pkg["description"], "GNOME window manager")
                else:
                    self.assertNotIn("description", root_package)

    def test_build_repository_paths(self):
        """Test repository path construction with various input scenarios."""
        test_cases = [
            {
                "name": "standard_repository_names",
                "base_url": "http://example.com/repo",
                "repository_names": "BaseOS,AppStream",
                "arch": "x86_64",
                "should_raise": False,
                "expected_repos": ["BaseOS", "AppStream", "BaseOS-sources", "AppStream-sources"],
                "expected_paths": {
                    "BaseOS": "http://example.com/repo/compose/BaseOS/x86_64/os/",
                    "AppStream": "http://example.com/repo/compose/AppStream/x86_64/os/",
                    "BaseOS-sources": "http://example.com/repo/compose/BaseOS/source/tree/",
                    "AppStream-sources": "http://example.com/repo/compose/AppStream/source/tree/"
                }
            },
            {
                "name": "mixed_names_and_urls",
                "base_url": "http://example.com/repo",
                "repository_names": "BaseOS,https://custom.repo.com/special/",
                "arch": "x86_64",
                "should_raise": False,
                "expected_repos": ["BaseOS"],
                "expected_paths": {
                    "BaseOS": "http://example.com/repo/compose/BaseOS/x86_64/os/"
                },
                "has_custom_repo": True,
                "custom_repo_prefix": "special_",
                "custom_repo_url_start": "https://custom.repo.com/special"
            },
            {
                "name": "empty_repository_names",
                "base_url": "http://example.com/repo",
                "repository_names": "",
                "arch": "x86_64",
                "should_raise": True
            }
        ]

        for case in test_cases:
            with self.subTest(scenario=case["name"]):
                if case["should_raise"]:
                    with self.assertRaises(SystemExit):
                        find_package_dependents.build_repository_paths(
                            case["base_url"], case["repository_names"], case["arch"]
                        )
                else:
                    paths = find_package_dependents.build_repository_paths(
                        case["base_url"], case["repository_names"], case["arch"]
                    )

                    for repo in case["expected_repos"]:
                        self.assertIn(repo, paths)

                    for repo, expected_path in case["expected_paths"].items():
                        self.assertEqual(paths[repo], expected_path)

                    if case.get("has_custom_repo"):
                        custom_key = None
                        for key in paths.keys():
                            if key.startswith(case["custom_repo_prefix"]):
                                custom_key = key
                                break
                        self.assertIsNotNone(custom_key, "Custom repository key not found")
                        self.assertTrue(paths[custom_key].startswith(case["custom_repo_url_start"]))

    def test_generate_json_output(self):
        """Test JSON output generation with and without descriptions."""
        test_cases = [
            {
                "name": "without_descriptions",
                "describe": False,
                "package_descriptions": None,
                "expected_has_description": False
            },
            {
                "name": "with_descriptions",
                "describe": True,
                "package_descriptions": {
                    "podman": "Container engine for managing pods and containers",
                    "bootc": "Bootable container images for Fedora CoreOS",
                    "toolbox": "Tool for developing inside containers"
                },
                "expected_has_description": True
            }
        ]

        for case in test_cases:
            with self.subTest(scenario=case["name"]):
                arguments = Mock()
                arguments.all = False
                arguments.package_name = "podman"
                arguments.describe = case["describe"]

                dependents_data = ["bootc", "toolbox", "container-tools"]

                output = find_package_dependents.generate_json_output(
                    arguments, dependents_data, case["package_descriptions"]
                )

                parsed = json.loads(output)
                self.assertEqual(len(parsed), 1)
                self.assertEqual(parsed[0]["package"], "podman")
                self.assertEqual(parsed[0]["dependents"], ["bootc", "toolbox", "container-tools"])

                if case["expected_has_description"]:
                    self.assertIn("description", parsed[0])
                    self.assertEqual(parsed[0]["description"], "Container engine for managing pods and containers")
                else:
                    self.assertNotIn("description", parsed[0])

    def test_generate_plain_output(self):
        """Test plain text output generation with and without descriptions."""
        test_cases = [
            {
                "name": "without_descriptions",
                "describe": False,
                "dependents_data": ["bootc", "toolbox", "container-tools"],
                "package_descriptions": None,
                "expected_lines": ["bootc", "toolbox", "container-tools"]
            },
            {
                "name": "with_descriptions",
                "describe": True,
                "dependents_data": ["bootc", "toolbox"],
                "package_descriptions": {
                    "bootc": "Bootable container images for Fedora CoreOS",
                    "toolbox": "Tool for developing inside containers"
                },
                "expected_lines": [
                    "bootc: Bootable container images for Fedora CoreOS",
                    "toolbox: Tool for developing inside containers"
                ]
            }
        ]

        for case in test_cases:
            with self.subTest(scenario=case["name"]):
                arguments = Mock()
                arguments.all = False
                arguments.describe = case["describe"]

                output = find_package_dependents.generate_plain_output(
                    arguments, case["dependents_data"], case["package_descriptions"]
                )

                lines = output.split('\n')
                self.assertEqual(len(lines), len(case["expected_lines"]))

                for expected_line in case["expected_lines"]:
                    self.assertIn(expected_line, lines)

    def test_generate_output_formats(self):
        """Test output format selection (JSON vs plain text)."""
        test_cases = [
            {
                "name": "json_format",
                "format": "json",
                "validator": lambda output: json.loads(output) and isinstance(json.loads(output), list)
            },
            {
                "name": "plain_format",
                "format": "plain",
                "validator": lambda output: isinstance(output, str) and "bootc" in output and "toolbox" in output
            }
        ]

        for case in test_cases:
            with self.subTest(format=case["format"]):
                arguments = Mock()
                arguments.format = case["format"]
                arguments.all = False
                arguments.package_name = "podman"
                arguments.describe = False

                dependents_data = ["bootc", "toolbox"]
                package_descriptions = None

                output = find_package_dependents.generate_output(
                    arguments, dependents_data, package_descriptions
                )

                self.assertTrue(case["validator"](output))

    def test_write_output_to_file(self):
        """Test that output is correctly written to files."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_file:
            temp_path = Path(temp_file.name)

        try:
            output_data = "test output\nwith multiple lines"
            find_package_dependents.write_output(output_data, temp_path)

            with open(temp_path, 'r') as f:
                written_data = f.read()

            self.assertEqual(written_data, output_data)
        finally:
            temp_path.unlink(missing_ok=True)

    def test_write_output_to_stdout(self):
        """Test that output is correctly written to standard output."""
        output_data = "test output"

        with captured_stdout() as captured:
            find_package_dependents.write_output(output_data, None)

            output = captured.getvalue()
            self.assertEqual(output, output_data + "\n")

    def test_display_statistics(self):
        """Test that performance statistics are correctly displayed."""
        with captured_stderr() as captured:
            find_package_dependents.display_statistics(
                "test filter",
                self.metrics,
                self.source_cache,
                self.filter_cache,
                self.dependency_cache
            )

            output = captured.getvalue()
            self.assertIn("statistics", output.lower())

    def test_log_operation(self):
        """Test that operation logging works correctly."""
        find_package_dependents.log_operation(
            "podman",
            True,
            True,
            10,
            "test filter",
            Path("/tmp/test")
        )

    def test_set_up_logging(self):
        """Test that logging configuration is correctly set up."""
        find_package_dependents.set_up_logging(True, None)

        with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_file:
            temp_path = Path(temp_file.name)

        try:
            find_package_dependents.set_up_logging(False, temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

    @patch.object(find_package_dependents, 'run_command')
    def test_main_basic_functionality(self, mock_run_command):
        """Test that the main function correctly processes package dependencies and outputs results."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "bootc\ntoolbox\ncontainer-tools"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'podman']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "podman"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = False
                mock_args.source_packages = False
                mock_args.max_results = None
                mock_args.format = "plain"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False
                mock_args.filter_command = None
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_list') as mock_build_list:
                        mock_build_list.return_value = ["bootc", "toolbox", "container-tools"]

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()

                            self.assertIn("bootc", output)
                            self.assertIn("toolbox", output)
                            self.assertIn("container-tools", output)

    @patch.object(find_package_dependents, 'run_command')
    def test_main_json_output(self, mock_run_command):
        """Test that the main function correctly generates JSON output format."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "bootc\ntoolbox"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'podman', '--format', 'json']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "podman"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = False
                mock_args.source_packages = False
                mock_args.max_results = None
                mock_args.format = "json"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False
                mock_args.filter_command = None
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_list') as mock_build_list:
                        mock_build_list.return_value = ["bootc", "toolbox"]

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()
                            parsed = json.loads(output)
                            self.assertIsInstance(parsed, list)
                            self.assertEqual(len(parsed), 1)
                            self.assertEqual(parsed[0]["package"], "podman")
                            self.assertEqual(parsed[0]["dependents"], ["bootc", "toolbox"])

    @patch.object(find_package_dependents, 'run_command')
    def test_main_filter_command(self, mock_run_command):
        """Test that the main function correctly applies filter commands to package dependents."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "bootc\ntoolbox\ncontainer-tools"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'podman', '--filter-command', 'echo $PACKAGE | grep -q bootc']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "podman"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = False
                mock_args.source_packages = False
                mock_args.max_results = None
                mock_args.format = "plain"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False
                mock_args.filter_command = "echo $PACKAGE | grep -q bootc"
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_list') as mock_build_list:
                        mock_build_list.return_value = ["bootc"]

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()
                            self.assertIn("bootc", output)
                            self.assertNotIn("toolbox", output)
                            self.assertNotIn("container-tools", output)

    @patch.object(find_package_dependents, 'run_command')
    def test_main_max_results(self, mock_run_command):
        """Test that the main function correctly limits output to maximum number of results."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "bootc\ntoolbox\ncontainer-tools\nskopeo\nbuildah"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'podman', '--max-results', '2']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "podman"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = False
                mock_args.source_packages = False
                mock_args.max_results = 2
                mock_args.format = "plain"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False
                mock_args.filter_command = None
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_list') as mock_build_list:
                        mock_build_list.return_value = ["bootc", "toolbox"]

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()
                            self.assertIn("bootc", output)
                            self.assertIn("toolbox", output)
                            self.assertNotIn("container-tools", output)
                            self.assertNotIn("skopeo", output)
                            self.assertNotIn("buildah", output)

    @patch.object(find_package_dependents, 'run_command')
    def test_main_allow_missing(self, mock_run_command):
        """Test that the main function gracefully handles missing packages when allow_missing is enabled."""
        mock_run_command.side_effect = subprocess.CalledProcessError(
            1, "dnf repoquery", "Package not found", "Error: No package found"
        )

        with patch('sys.argv', ['find-package-dependents.py', 'non-existent-package', '--allow-missing']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "non-existent-package"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = False
                mock_args.source_packages = False
                mock_args.max_results = None
                mock_args.format = "plain"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False
                mock_args.filter_command = None
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = True
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_list') as mock_build_list:
                        mock_build_list.return_value = []

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()
                            self.assertEqual(output.strip(), "")

    @patch.object(find_package_dependents, 'run_command')
    def test_main_deep_tree_with_max_results(self, mock_run_command):
        """Test that the main function correctly handles deep trees with max-results limits."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "mutter\nweston"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'libwayland-server', '--max-results', '2', '--all', '--format', 'json']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "libwayland-server"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = True
                mock_args.source_packages = False
                mock_args.max_results = 2
                mock_args.format = "json"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False
                mock_args.filter_command = None
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_graph') as mock_build_graph:
                        mock_build_graph.return_value = {
                            "libwayland-server": {
                                "dependents": ["mutter", "weston"],
                                "partial": True
                            },
                            "mutter": {
                                "dependents": ["gnome-shell"],
                                "partial": False
                            },
                            "weston": {
                                "dependents": ["kwin"],
                                "partial": False
                            },
                            "gnome-shell": {
                                "dependents": [],
                                "partial": False
                            },
                            "kwin": {
                                "dependents": [],
                                "partial": False
                            }
                        }

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()
                            parsed = json.loads(output)

                            self.assertEqual(len(parsed), 5)

                            root_package = next(pkg for pkg in parsed if pkg["package"] == "libwayland-server")
                            self.assertTrue(root_package["partial"])
                            self.assertEqual(len(root_package["dependents"]), 2)

                            mutter_package = next(pkg for pkg in parsed if pkg["package"] == "mutter")
                            self.assertFalse(mutter_package["partial"])

                            weston_package = next(pkg for pkg in parsed if pkg["package"] == "weston")
                            self.assertFalse(weston_package["partial"])

    @patch.object(find_package_dependents, 'run_command')
    def test_main_deep_tree_with_filter_and_max_results(self, mock_run_command):
        """Test that the main function correctly combines filter commands with max-results in deep trees."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "mutter\nweston"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'libwayland-server', '--max-results', '1', '--filter-command', 'echo $PACKAGE | grep -q mutter', '--all', '--format', 'json']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "libwayland-server"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = True
                mock_args.source_packages = False
                mock_args.max_results = 1
                mock_args.format = "json"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False
                mock_args.filter_command = "echo $PACKAGE | grep -q mutter"
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_graph') as mock_build_graph:
                        mock_build_graph.return_value = {
                            "libwayland-server": {
                                "dependents": ["mutter"],
                                "partial": True
                            },
                            "mutter": {
                                "dependents": ["gnome-shell"],
                                "partial": False
                            },
                            "gnome-shell": {
                                "dependents": [],
                                "partial": False
                            }
                        }

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()
                            parsed = json.loads(output)

                            self.assertEqual(len(parsed), 3)

                            root_package = next(pkg for pkg in parsed if pkg["package"] == "libwayland-server")
                            self.assertTrue(root_package["partial"])
                            self.assertEqual(len(root_package["dependents"]), 1)
                            self.assertIn("mutter", root_package["dependents"])

                            self.assertNotIn("weston", root_package["dependents"])

    @patch.object(find_package_dependents, 'run_command')
    def test_main_deep_tree_with_descriptions_and_partial_flags(self, mock_run_command):
        """Test that the main function correctly includes descriptions with partial flags in deep trees."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "mutter\nweston"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'libwayland-server', '--max-results', '2', '--all', '--format', 'json', '--describe']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "libwayland-server"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = True
                mock_args.source_packages = False
                mock_args.max_results = 2
                mock_args.format = "json"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False
                mock_args.filter_command = None
                mock_args.describe = True
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_graph') as mock_build_graph:
                        mock_build_graph.return_value = {
                            "libwayland-server": {
                                "dependents": ["mutter", "weston"],
                                "partial": True
                            },
                            "mutter": {
                                "dependents": ["gnome-shell"],
                                "partial": False
                            },
                            "weston": {
                                "dependents": ["kwin"],
                                "partial": False
                            },
                            "gnome-shell": {
                                "dependents": [],
                                "partial": False
                            },
                            "kwin": {
                                "dependents": [],
                                "partial": False
                            }
                        }

                        with patch.object(find_package_dependents, 'collect_package_descriptions') as mock_collect_descriptions:
                            mock_collect_descriptions.return_value = {
                                "libwayland-server": "Wayland display server library",
                                "mutter": "GNOME window manager",
                                "weston": "Wayland reference compositor",
                                "gnome-shell": "GNOME desktop shell",
                                "kwin": "KDE window manager"
                            }

                            with captured_stdout() as captured:
                                find_package_dependents.main()

                                output = captured.getvalue()
                                parsed = json.loads(output)

                                self.assertEqual(len(parsed), 5)

                                root_package = next(pkg for pkg in parsed if pkg["package"] == "libwayland-server")
                                self.assertEqual(root_package["description"], "Wayland display server library")
                                self.assertTrue(root_package["partial"])

                                mutter_package = next(pkg for pkg in parsed if pkg["package"] == "mutter")
                                self.assertEqual(mutter_package["description"], "GNOME window manager")
                                self.assertFalse(mutter_package["partial"])

                                weston_package = next(pkg for pkg in parsed if pkg["package"] == "weston")
                                self.assertEqual(weston_package["description"], "Wayland reference compositor")
                                self.assertFalse(weston_package["partial"])


    @patch.object(find_package_dependents, 'run_command')
    def test_main_with_source_packages_flag(self, mock_run_command):
        """Test that the main function correctly converts binary packages to source packages when --source-packages is used."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "bootc\ntoolbox"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'podman', '--source-packages']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "podman"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = False
                mock_args.source_packages = True  # This is the key flag we're testing
                mock_args.max_results = None
                mock_args.format = "plain"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False
                mock_args.filter_command = None
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_list') as mock_build_list:
                        mock_build_list.return_value = ["bootc-src", "toolbox-src"]

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()
                            self.assertIn("bootc-src", output)
                            self.assertIn("toolbox-src", output)

                        mock_build_list.assert_called_once()
                        call_args = mock_build_list.call_args
                        self.assertEqual(call_args.kwargs['show_source_packages'], True)

    @patch.object(find_package_dependents, 'run_command')
    def test_main_with_show_cycles_flag(self, mock_run_command):
        """Test that the main function correctly includes cycles when --show-cycles is used with --all."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "mutter\nweston"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'libwayland-server', '--all', '--show-cycles', '--format', 'json']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "libwayland-server"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = True
                mock_args.source_packages = False
                mock_args.max_results = None
                mock_args.format = "json"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = True  # This is the key flag we're testing
                mock_args.filter_command = None
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_graph') as mock_build_graph:
                        mock_build_graph.return_value = {
                            "libwayland-server": {
                                "dependents": ["mutter", "libwayland-server"],  # Cycle: depends on itself through mutter
                                "partial": False
                            },
                            "mutter": {
                                "dependents": ["libwayland-server"],  # Creates the cycle
                                "partial": False
                            }
                        }

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()
                            parsed = json.loads(output)

                            self.assertEqual(len(parsed), 2)
                            libwayland_pkg = next(pkg for pkg in parsed if pkg["package"] == "libwayland-server")
                            mutter_pkg = next(pkg for pkg in parsed if pkg["package"] == "mutter")

                            self.assertIn("mutter", libwayland_pkg["dependents"])
                            self.assertIn("libwayland-server", libwayland_pkg["dependents"])  # Shows cycle
                            self.assertIn("libwayland-server", mutter_pkg["dependents"])

                        mock_build_graph.assert_called_once()
                        call_args = mock_build_graph.call_args
                        self.assertEqual(call_args.kwargs['keep_cycles'], True)

    @patch.object(find_package_dependents, 'run_command')
    def test_main_without_show_cycles_flag(self, mock_run_command):
        """Test that the main function correctly excludes cycles when --show-cycles is not used (default behavior)."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "mutter\nweston"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'libwayland-server', '--all', '--format', 'json']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "libwayland-server"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = True
                mock_args.source_packages = False
                mock_args.max_results = None
                mock_args.format = "json"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False  # Default: no cycles
                mock_args.filter_command = None
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_graph') as mock_build_graph:
                        mock_build_graph.return_value = {
                            "libwayland-server": {
                                "dependents": ["mutter", "weston"],  # No cycle back to self
                                "partial": False
                            },
                            "mutter": {
                                "dependents": ["gnome-shell"],  # No cycle back to libwayland-server
                                "partial": False
                            },
                            "weston": {
                                "dependents": ["kwin"],
                                "partial": False
                            },
                            "gnome-shell": {
                                "dependents": [],
                                "partial": False
                            },
                            "kwin": {
                                "dependents": [],
                                "partial": False
                            }
                        }

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()
                            parsed = json.loads(output)

                            self.assertEqual(len(parsed), 5)
                            libwayland_pkg = next(pkg for pkg in parsed if pkg["package"] == "libwayland-server")
                            mutter_pkg = next(pkg for pkg in parsed if pkg["package"] == "mutter")

                            self.assertIn("mutter", libwayland_pkg["dependents"])
                            self.assertIn("weston", libwayland_pkg["dependents"])
                            self.assertNotIn("libwayland-server", libwayland_pkg["dependents"])  # No cycle
                            self.assertIn("gnome-shell", mutter_pkg["dependents"])
                            self.assertNotIn("libwayland-server", mutter_pkg["dependents"])  # No cycle

                        mock_build_graph.assert_called_once()
                        call_args = mock_build_graph.call_args
                        self.assertEqual(call_args.kwargs['keep_cycles'], False)

    @patch.object(find_package_dependents, 'run_command')
    def test_main_source_packages_with_all_flag(self, mock_run_command):
        """Test that --source-packages works correctly with --all flag (transitive dependencies)."""
        mock_run_command.return_value = {
            "return_code": 0,
            "output": "mutter\nweston"
        }

        with patch('sys.argv', ['find-package-dependents.py', 'libwayland-server', '--all', '--source-packages', '--format', 'json']):
            with patch.object(find_package_dependents, 'parse_command_line_arguments') as mock_parse:
                mock_args = Mock()
                mock_args.package_name = "libwayland-server"
                mock_args.base_url = "http://example.com/repo"
                mock_args.repository_names = "BaseOS,AppStream"
                mock_args.arch = "x86_64"
                mock_args.output_file = None
                mock_args.all = True
                mock_args.source_packages = True  # Convert to source packages
                mock_args.max_results = None
                mock_args.format = "json"
                mock_args.verbose = False
                mock_args.no_refresh = True
                mock_args.stats = False
                mock_args.show_cycles = False
                mock_args.filter_command = None
                mock_args.describe = False
                mock_args.log_file = None
                mock_args.allow_missing = False
                mock_parse.return_value = mock_args

                with patch.object(find_package_dependents, 'build_repository_paths') as mock_build_repos:
                    mock_build_repos.return_value = self.repositories

                    with patch.object(find_package_dependents, 'build_dependents_graph') as mock_build_graph:
                        mock_build_graph.return_value = {
                            "libwayland-src": {  # Source package name
                                "dependents": ["mutter-src", "weston-src"],
                                "partial": False
                            },
                            "mutter-src": {
                                "dependents": ["gnome-shell-src"],
                                "partial": False
                            },
                            "weston-src": {
                                "dependents": ["kwin-src"],
                                "partial": False
                            },
                            "gnome-shell-src": {
                                "dependents": [],
                                "partial": False
                            },
                            "kwin-src": {
                                "dependents": [],
                                "partial": False
                            }
                        }

                        with captured_stdout() as captured:
                            find_package_dependents.main()

                            output = captured.getvalue()
                            parsed = json.loads(output)

                            self.assertEqual(len(parsed), 5)
                            package_names = [pkg["package"] for pkg in parsed]
                            self.assertIn("libwayland-src", package_names)
                            self.assertIn("mutter-src", package_names)
                            self.assertIn("weston-src", package_names)
                            self.assertIn("gnome-shell-src", package_names)
                            self.assertIn("kwin-src", package_names)

                        mock_build_graph.assert_called_once()
                        call_args = mock_build_graph.call_args
                        self.assertEqual(call_args.kwargs['show_source_packages'], True)

    def test_build_dependents_graph_with_cycles(self):
        """Test that build_dependents_graph correctly handles cycles when keep_cycles=True."""
        with patch.object(find_package_dependents, 'generate_direct_dependents') as mock_generate:
            def mock_dependents(package_name, *args, **kwargs):
                if package_name == "libwayland-server":
                    return iter(["mutter"])
                elif package_name == "mutter":
                    return iter(["libwayland-server"])  # Creates cycle
                else:
                    return iter([])

            mock_generate.side_effect = mock_dependents

            with patch.object(find_package_dependents, 'run_filter_command') as mock_filter:
                mock_filter.return_value = True  # All packages pass filter

                result = find_package_dependents.build_dependents_graph(
                    root_package="libwayland-server",
                    repository_paths=self.repositories,
                    show_source_packages=False,
                    source_cache=self.source_cache,
                    metrics=self.metrics,
                    filter_cache=self.filter_cache,
                    dependency_cache=self.dependency_cache,
                    max_results=None,
                    keep_cycles=True,  # Allow cycles
                    verbose=False,
                    filter_command=None,
                    allow_missing=False
                )

                self.assertIn("libwayland-server", result)
                self.assertIn("mutter", result)

                self.assertIn("mutter", result["libwayland-server"]["dependents"])
                self.assertIn("libwayland-server", result["mutter"]["dependents"])

    def test_build_dependents_graph_without_cycles(self):
        """Test that build_dependents_graph correctly excludes cycles when keep_cycles=False."""
        with patch.object(find_package_dependents, 'generate_direct_dependents') as mock_generate:
            def mock_dependents(package_name, *args, **kwargs):
                if package_name == "libwayland-server":
                    return iter(["mutter"])
                elif package_name == "mutter":
                    return iter(["libwayland-server"])  # Would create cycle but should be excluded
                else:
                    return iter([])

            mock_generate.side_effect = mock_dependents

            with patch.object(find_package_dependents, 'run_filter_command') as mock_filter:
                mock_filter.return_value = True  # All packages pass filter

                result = find_package_dependents.build_dependents_graph(
                    root_package="libwayland-server",
                    repository_paths=self.repositories,
                    show_source_packages=False,
                    source_cache=self.source_cache,
                    metrics=self.metrics,
                    filter_cache=self.filter_cache,
                    dependency_cache=self.dependency_cache,
                    max_results=None,
                    keep_cycles=False,  # Exclude cycles (default)
                    verbose=False,
                    filter_command=None,
                    allow_missing=False
                )

                self.assertIn("libwayland-server", result)
                self.assertIn("mutter", result)

                self.assertIn("mutter", result["libwayland-server"]["dependents"])
                self.assertNotIn("libwayland-server", result["mutter"]["dependents"])

    @patch.object(find_package_dependents, 'run_command')
    def test_breadth_first_with_max_results_and_filtering(self, mock_run_command):
        """Test that breadth-first traversal works correctly with max_results even when some dependents are filtered out.

        This test verifies the fix for the bug where max_results was incorrectly applied to generate_direct_dependents,
        causing performance issues due to deep traversal when filters would reject some results.
        """
        def mock_dnf_calls(command_args):
            full_command = ' '.join(command_args)
            if "libwayland-server" in full_command:
                return {"return_code": 0, "output": "mutter\nweston\nsway\nkwin"}
            else:
                return {"return_code": 0, "output": ""}

        mock_run_command.side_effect = mock_dnf_calls

        def mock_filter_command(package_name, filter_cmd, *args, **kwargs):
            return package_name in ["mutter", "kwin"]

        with patch.object(find_package_dependents, 'run_filter_command', side_effect=mock_filter_command):
            result = find_package_dependents.build_dependents_graph(
                root_package="libwayland-server",
                repository_paths=self.repositories,
                show_source_packages=False,
                source_cache=self.source_cache,
                metrics=self.metrics,
                filter_cache=self.filter_cache,
                dependency_cache=self.dependency_cache,
                max_results=1,  # Very small limit to test breadth-first behavior
                keep_cycles=False,
                verbose=False,
                filter_command="test filter",  # Enable filtering
                allow_missing=False
            )

            self.assertIn("libwayland-server", result)
            root_dependents = result["libwayland-server"]["dependents"]

            self.assertGreaterEqual(len(root_dependents), 1)

            valid_dependents = [dep for dep in root_dependents if dep in ["mutter", "kwin"]]
            self.assertGreaterEqual(len(valid_dependents), 1)

            self.assertNotIn("sway", root_dependents)
            self.assertNotIn("weston", root_dependents)

            self.assertTrue(result["libwayland-server"]["partial"])


    @patch.object(find_package_dependents, 'run_command')
    def test_breadth_first_multi_level_traversal(self, mock_run_command):
        """Test breadth-first traversal works correctly across multiple dependency levels with filtering.
        """
        def mock_dnf_calls(command_args):
            full_command = ' '.join(command_args)
            if "glibc" in full_command:
                return {"return_code": 0, "output": "systemd\nbash\npodman\nbuildah\nkernel"}
            elif "podman" in full_command:
                return {"return_code": 0, "output": "cockpit-podman\ntoolbox"}
            elif "buildah" in full_command:
                return {"return_code": 0, "output": "container-tools\nskopeo"}
            else:
                return {"return_code": 0, "output": ""}

        mock_run_command.side_effect = mock_dnf_calls

        def mock_filter_command(package_name, filter_cmd, *args, **kwargs):
            return package_name in ["podman", "buildah", "cockpit-podman", "toolbox", "container-tools", "skopeo"]

        with patch.object(find_package_dependents, 'run_filter_command', side_effect=mock_filter_command):
            result = find_package_dependents.build_dependents_graph(
                root_package="glibc",
                repository_paths=self.repositories,
                show_source_packages=False,
                source_cache=self.source_cache,
                metrics=self.metrics,
                filter_cache=self.filter_cache,
                dependency_cache=self.dependency_cache,
                max_results=4,
                keep_cycles=False,
                verbose=False,
                filter_command="test filter",
                allow_missing=False
            )

            self.assertIn("glibc", result)
            self.assertIn("podman", result)
            self.assertIn("buildah", result)

            podman_children = result["podman"]["dependents"]
            self.assertIn("cockpit-podman", podman_children)
            self.assertIn("toolbox", podman_children)

            root_dependents = result["glibc"]["dependents"]
            self.assertNotIn("systemd", root_dependents)
            self.assertNotIn("bash", root_dependents)
            self.assertNotIn("kernel", root_dependents)


def print_header():
    print("─" * 70)
    print("find-package-dependents.py test suite")
    print("─" * 70)
    print()

def print_footer(passed, total, duration):
    print()
    print("─" * 70)
    if passed == total:
        print(f"All tests passed! ({passed}/{total})")
    else:
        print(f"Some tests failed! ({passed}/{total})")
        print("Please check the failing tests above.")
    print(f"Total time: {duration:.2f} seconds")
    print("─" * 70)

def run_tests():
    start_time = time.time()

    test_methods = []
    for attr_name in dir(TestFindPackageDependents):
        if attr_name.startswith('test_'):
            method = getattr(TestFindPackageDependents, attr_name)
            if hasattr(method, '__doc__') and method.__doc__:
                doc = method.__doc__.strip()
                test_methods.append((attr_name, doc))

    test_methods.sort(key=lambda x: x[0])

    print_header()

    total_tests = len(test_methods)
    passed_tests = 0
    failed_tests = 0
    error_tests = 0

    for i, (method_name, description) in enumerate(test_methods, 1):
        suite = unittest.TestSuite()
        test_instance = TestFindPackageDependents(method_name)
        suite.addTest(test_instance)

        with captured_stdout() as test_stdout, captured_stderr() as test_stderr:
            runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0)
            result = runner.run(suite)

            stdout_output = test_stdout.getvalue()
            stderr_output = test_stderr.getvalue()

        if result.failures:
            print(f"{i}. {description} ... fail")
            failed_tests += 1
            error_msg = result.failures[0][1]
            lines = error_msg.split('\n')
            error_lines = [line for line in lines[:5] if line.strip()]
            if error_lines:
                print("        ┌─────────────────────────────────────────────────────────────")
                for line in error_lines:
                    print(f"        │ {line}")
                if len(lines) > 5:
                    print(f"        │ ... ({len(lines) - 5} more lines)")
                print("        └─────────────────────────────────────────────────────────────")
        elif result.errors:
            print(f"{i}. {description} ... error")
            error_tests += 1
            error_msg = result.errors[0][1]
            lines = error_msg.split('\n')
            error_lines = [line for line in lines[:5] if line.strip()]
            if error_lines:
                print("        ┌─────────────────────────────────────────────────────────────")
                for line in error_lines:
                    print(f"        │ {line}")
                if len(lines) > 5:
                    print(f"        │ ... ({len(lines) - 5} more lines)")
                print("        └─────────────────────────────────────────────────────────────")
        else:
            print(f"{i}. {description} ... ok")
            passed_tests += 1

        all_output = []
        if stdout_output.strip():
            all_output.extend(stdout_output.rstrip('\n').split('\n'))
        if stderr_output.strip():
            all_output.extend(stderr_output.rstrip('\n').split('\n'))

        while all_output and not all_output[0].strip():
            all_output.pop(0)

        if all_output:
            print("        ┌─────────────────────────────────────────────────────────────")
            for line in all_output:
                print(f"        │ {line}")
            print("        └─────────────────────────────────────────────────────────────")

    duration = time.time() - start_time
    print(f"\nRan {total_tests} tests in {duration:.3f}s")

    success = (failed_tests == 0 and error_tests == 0)

    print_footer(passed_tests, total_tests, duration)

    return 0 if success else 1


if __name__ == '__main__':
    exit_code = run_tests()
    sys.exit(exit_code)
