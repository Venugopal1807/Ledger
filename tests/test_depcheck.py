"""Tests for tools/depcheck.py, the dependency inventory tool.

The tool is exercised as a command, because its exit status and its output
formatting are the parts users and CI depend on.

One test cross-checks the tool against the independent classifier in
test_no_dependencies.py. That check has value precisely because neither
implementation is derived from the other: agreement between two separately
written classifiers is evidence, whereas a tool agreeing with itself is not.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import test_no_dependencies as audit_module

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEPCHECK = REPO_ROOT / "tools" / "depcheck.py"
TIMEOUT = 60


class DepcheckTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, relative, body):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def fake_site(self, *modules):
        """A directory holding importable modules that are not stdlib."""
        site = tempfile.TemporaryDirectory()
        self.addCleanup(site.cleanup)
        for name in modules:
            pathlib.Path(site.name, f"{name}.py").write_text("", encoding="utf-8")
        return site.name

    def run_tool(self, *args, pythonpath=None):
        env = dict(os.environ)
        if pythonpath:
            env["PYTHONPATH"] = pythonpath
        return subprocess.run(
            [sys.executable, str(DEPCHECK), *args],
            capture_output=True, text=True, timeout=TIMEOUT, env=env,
        )

    def scan(self, *args, pythonpath=None):
        return self.run_tool("--root", str(self.root), *args,
                             pythonpath=pythonpath)


class TestRealRepository(DepcheckTestCase):
    def test_repository_passes_with_exit_zero(self):
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no third-party or unresolved imports", result.stdout)
        self.assertIn("third-party (0)", result.stdout)
        self.assertIn("unresolved (0)", result.stdout)

    def test_output_lists_the_production_module_and_its_role(self):
        stdout = self.run_tool().stdout
        self.assertIn("ledger.py", stdout)
        self.assertIn("[production]", stdout)
        self.assertIn("[tests]", stdout)
        self.assertIn("[tools]", stdout)

    def test_output_is_byte_for_byte_deterministic(self):
        first = self.run_tool()
        second = self.run_tool()
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.returncode, second.returncode)

    def test_json_output_is_valid_and_deterministic(self):
        first = self.run_tool("--json")
        second = self.run_tool("--json")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout, second.stdout)
        data = json.loads(first.stdout)
        self.assertIn("ledger.py", data["files"])
        self.assertTrue(data["imports"])
        for record in data["imports"]:
            self.assertEqual(
                set(record), {"module", "file", "line", "role", "category"}
            )

    def test_json_records_are_sorted(self):
        data = json.loads(self.run_tool("--json").stdout)
        keys = [(r["file"], r["module"], r["line"]) for r in data["imports"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(data["files"], sorted(data["files"]))

    def test_production_file_imports_only_stdlib_per_the_tool(self):
        data = json.loads(self.run_tool("--json").stdout)
        production = [r for r in data["imports"] if r["role"] == "production"]
        self.assertTrue(production)
        for record in production:
            with self.subTest(module=record["module"]):
                self.assertEqual(record["category"], "stdlib")

    def test_tool_agrees_with_the_independent_classifier(self):
        """Two separately written classifiers, same answer.

        If they ever disagree, one of them is wrong and this fails - which
        is the entire point of writing them independently.
        """
        data = json.loads(self.run_tool("--json").stdout)
        by_tool = {
            (r["file"], r["module"]): r["category"] for r in data["imports"]
        }
        independent = audit_module.audit(REPO_ROOT)
        by_test = {}
        for category, findings in independent.items():
            for module, path, _line in findings:
                by_test[(pathlib.Path(path).as_posix(), module)] = category
        self.assertEqual(by_tool, by_test)


class TestClassification(DepcheckTestCase):
    def test_stdlib_and_project_local_pass(self):
        self.write("ledger.py", "import os\nimport json\n")
        self.write("tests/helper.py", "import ledger\nimport struct\n")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("project-local (1)", result.stdout)

    def test_third_party_import_fails_with_exit_one(self):
        site = self.fake_site("pretend_package")
        self.write("app.py", "import os\nimport pretend_package\n")
        result = self.scan(pythonpath=site)
        self.assertEqual(result.returncode, 1)
        self.assertIn("THIRD-PARTY IMPORT:", result.stdout)
        self.assertIn("module: pretend_package", result.stdout)
        self.assertIn("file: app.py", result.stdout)
        self.assertIn("line: 2", result.stdout)

    def test_unresolved_import_fails_with_exit_one(self):
        self.write("app.py", "import nothing_of_the_sort_xyzzy\n")
        result = self.scan()
        self.assertEqual(result.returncode, 1)
        self.assertIn("UNRESOLVED IMPORT:", result.stdout)
        self.assertIn("module: nothing_of_the_sort_xyzzy", result.stdout)
        self.assertIn("line: 1", result.stdout)

    def test_third_party_and_unresolved_are_separate_categories(self):
        site = self.fake_site("installed_elsewhere")
        self.write(
            "app.py", "import installed_elsewhere\nimport missing_xyzzy\n"
        )
        data = json.loads(self.scan("--json", pythonpath=site).stdout)
        categories = {r["module"]: r["category"] for r in data["imports"]}
        self.assertEqual(categories["installed_elsewhere"], "third-party")
        self.assertEqual(categories["missing_xyzzy"], "unresolved")

    def test_a_local_module_shadows_an_installed_package(self):
        site = self.fake_site("shadowed")
        self.write("shadowed.py", "")
        self.write("app.py", "import shadowed\n")
        result = self.scan(pythonpath=site)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_package_directory_is_project_local(self):
        self.write("pkg/__init__.py", "")
        self.write("app.py", "import pkg\n")
        self.assertEqual(self.scan().returncode, 0)

    def test_imports_inside_functions_are_caught(self):
        self.write(
            "app.py",
            "def loader():\n    import nothing_of_the_sort_xyzzy\n    return 1\n",
        )
        result = self.scan()
        self.assertEqual(result.returncode, 1)
        self.assertIn("line: 2", result.stdout)

    def test_dotted_and_from_imports_use_the_top_level_name(self):
        self.write(
            "app.py",
            "from os import path\nimport xml.etree.ElementTree\n",
        )
        data = json.loads(self.scan("--json").stdout)
        self.assertEqual(
            sorted(r["module"] for r in data["imports"]), ["os", "xml"]
        )

    def test_relative_imports_do_not_count_as_dependencies(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/mod.py", "from . import other\nfrom .deep import x\n")
        self.assertEqual(self.scan().returncode, 0)


class TestExcludedDirectories(DepcheckTestCase):
    def test_generated_directories_are_not_scanned(self):
        self.write("app.py", "import os\n")
        for excluded in ("__pycache__", ".git", ".venv", "venv", "build",
                         "dist", "node_modules", ".tox"):
            self.write(f"{excluded}/junk.py", "import fake_xyzzy\n")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("fake_xyzzy", result.stdout)

    def test_nested_excluded_directories_are_skipped(self):
        self.write("app.py", "import os\n")
        self.write("src/.venv/lib/pkg/junk.py", "import fake_xyzzy\n")
        self.assertEqual(self.scan().returncode, 0)

    def test_only_python_files_are_read(self):
        self.write("app.py", "import os\n")
        self.write("notes.txt", "import fake_xyzzy\n")
        self.write("README.md", "```python\nimport fake_xyzzy\n```\n")
        result = self.scan()
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("fake_xyzzy", result.stdout)

    def test_empty_tree_passes(self):
        result = self.scan()
        self.assertEqual(result.returncode, 0)
        self.assertIn("stdlib (0)", result.stdout)


class TestOutputStability(DepcheckTestCase):
    @staticmethod
    def section_entries(stdout, heading, next_heading):
        """First token of each indented line in one section of the report."""
        body = stdout.split(heading, 1)[1]
        if next_heading:
            body = body.split(next_heading, 1)[0]
        return [
            line.split()[0] for line in body.splitlines()
            if line.startswith("  ") and line.strip() != "none"
        ]

    def test_modules_and_files_are_sorted_regardless_of_creation_order(self):
        self.write("zebra.py", "import zlib\nimport argparse\n")
        self.write("alpha.py", "import struct\nimport json\n")
        stdout = self.scan().stdout
        modules = self.section_entries(stdout, "stdlib", "project-local")
        self.assertEqual(modules, ["argparse", "json", "struct", "zlib"])
        files = self.section_entries(stdout, "by file", None)
        self.assertEqual(files, ["alpha.py", "zebra.py"])

    def test_repeated_runs_on_the_same_tree_match(self):
        self.write("app.py", "import os\nimport json\n")
        self.assertEqual(self.scan().stdout, self.scan().stdout)


if __name__ == "__main__":
    unittest.main()
