"""The zero-dependency claim, enforced as a test rather than asserted.

This audits source code, not the environment. `pip freeze`, `pip list`,
requirements files and `importlib.metadata` all describe what happens to be
installed; only the source says what the project actually imports, and the
source is what ships.

The classifier here is written independently of `tools/depcheck.py`. The
two exist to cross-check each other: if the tool ever misclassifies an
import, this test still fails, because it does not ask the tool anything.
"""

import ast
import importlib.util
import pathlib
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Never walk into these. Keeps the audit fast and stops it reporting on
# bytecode caches, virtual environments or build output.
SKIP_DIRECTORIES = frozenset({
    ".git", "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    "build", "dist", "node_modules", ".eggs",
})

STDLIB = "stdlib"
LOCAL = "project-local"
THIRD_PARTY = "third-party"
UNRESOLVED = "unresolved"


def iter_sources(root):
    """Every .py file in the tree, sorted, excluding generated directories."""
    found = []
    for path in root.rglob("*.py"):
        if SKIP_DIRECTORIES.intersection(path.relative_to(root).parts):
            continue
        found.append(path)
    return sorted(found)


def local_module_names(paths):
    """Module names importable from within this repository.

    A bare .py file provides its stem; a directory with __init__.py
    provides the package name. Both are legitimate imports and must not be
    mistaken for third-party ones.
    """
    names = set()
    for path in paths:
        if path.name == "__init__.py":
            names.add(path.parent.name)
        else:
            names.add(path.stem)
    return names


def imports_in(path):
    """Every imported top-level module name, with its line number.

    ast.walk descends into functions and methods, so an import hidden
    inside a function body is found exactly like a module-level one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import names a package inside this tree by
                # construction, and `from . import x` carries no module at
                # all, so resolve it to the package the file sits in rather
                # than recording an empty name.
                found.append((_relative_package(path, node.level), node.lineno))
            elif node.module:
                found.append((node.module.split(".")[0], node.lineno))
    return found


def _relative_package(path, level):
    """The package a relative import of `level` dots refers to."""
    try:
        return path.parents[level - 1].name
    except IndexError:
        return path.parent.name


def classify(module, local_names):
    """Sort one module name into exactly one of the four categories."""
    if module in local_names:
        return LOCAL
    if module in sys.stdlib_module_names:
        return STDLIB
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        spec = None
    return THIRD_PARTY if spec is not None else UNRESOLVED


def audit(root):
    """Return {category: [(module, path, line)]} for a source tree."""
    paths = iter_sources(root)
    local = local_module_names(paths)
    results = {STDLIB: [], LOCAL: [], THIRD_PARTY: [], UNRESOLVED: []}
    for path in paths:
        for module, line in imports_in(path):
            relative = path.relative_to(root)
            results[classify(module, local)].append((module, str(relative), line))
    return results


def describe(label, findings):
    lines = []
    for module, path, line in sorted(findings):
        lines.append(
            f"\n{label}:\n  module: {module}\n  file: {path}\n  line: {line}"
        )
    return "".join(lines)


class TestRepositoryHasNoDependencies(unittest.TestCase):
    """The audit that actually guards the project."""

    @classmethod
    def setUpClass(cls):
        cls.results = audit(REPO_ROOT)

    def test_no_third_party_imports(self):
        findings = self.results[THIRD_PARTY]
        self.assertEqual(
            findings, [],
            "the project must import nothing outside the standard library"
            + describe("THIRD-PARTY IMPORT", findings),
        )

    def test_no_unresolved_imports(self):
        findings = self.results[UNRESOLVED]
        self.assertEqual(
            findings, [],
            "every import must resolve to the standard library or this repo"
            + describe("UNRESOLVED IMPORT", findings),
        )

    def test_production_module_imports_only_the_standard_library(self):
        """ledger.py is the only file that ships, so it gets its own check."""
        production = REPO_ROOT / "ledger.py"
        self.assertTrue(production.is_file())
        for module, line in imports_in(production):
            with self.subTest(module=module, line=line):
                self.assertEqual(
                    classify(module, set()), STDLIB,
                    f"ledger.py:{line} imports {module!r}, which is not stdlib",
                )

    def test_the_audit_actually_saw_the_sources(self):
        """A classifier that scans nothing would pass every check above."""
        paths = iter_sources(REPO_ROOT)
        names = {path.name for path in paths}
        self.assertIn("ledger.py", names)
        self.assertIn("test_no_dependencies.py", names)
        self.assertGreaterEqual(len(paths), 9)
        self.assertGreater(len(self.results[STDLIB]), 20)

    def test_no_requirements_or_dependency_manifests_exist(self):
        for name in ("requirements.txt", "requirements-dev.txt", "Pipfile",
                     "poetry.lock", "setup.py", "setup.cfg", "environment.yml"):
            with self.subTest(name=name):
                self.assertFalse(
                    (REPO_ROOT / name).exists(),
                    f"{name} would imply dependencies this project does not have",
                )


class TestClassifier(unittest.TestCase):
    """The classifier's own behaviour, on synthetic trees.

    Production sources are never modified to manufacture a failure; every
    negative case is built in a temporary directory.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, relative, body):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def install_fake_package(self, name):
        """Put a module on sys.path that is neither stdlib nor in the tree.

        This is what a third-party package looks like to the classifier,
        without needing one to actually be installed.
        """
        site = tempfile.TemporaryDirectory()
        self.addCleanup(site.cleanup)
        pathlib.Path(site.name, f"{name}.py").write_text("", encoding="utf-8")
        sys.path.insert(0, site.name)
        self.addCleanup(sys.path.remove, site.name)
        importlib.invalidate_caches()

    def test_stdlib_imports(self):
        for module in ("os", "json", "zlib", "struct", "fcntl", "argparse",
                       "dataclasses", "pathlib", "sys", "__future__"):
            with self.subTest(module=module):
                self.assertEqual(classify(module, set()), STDLIB)

    def test_project_local_imports(self):
        self.write("ledger.py", "")
        self.write("tests/helpers.py", "import ledger\n")
        paths = iter_sources(self.root)
        local = local_module_names(paths)
        self.assertEqual(local, {"ledger", "helpers"})
        self.assertEqual(classify("ledger", local), LOCAL)
        self.assertEqual(classify("helpers", local), LOCAL)

    def test_a_package_directory_is_project_local(self):
        self.write("mypkg/__init__.py", "")
        self.write("mypkg/inner.py", "")
        local = local_module_names(iter_sources(self.root))
        self.assertIn("mypkg", local)
        self.assertEqual(classify("mypkg", local), LOCAL)

    def test_third_party_import_is_detected(self):
        self.install_fake_package("pretend_third_party")
        self.write("app.py", "import pretend_third_party\n")
        results = audit(self.root)
        self.assertEqual(
            results[THIRD_PARTY], [("pretend_third_party", "app.py", 1)]
        )
        self.assertEqual(results[UNRESOLVED], [])

    def test_unresolved_import_is_detected(self):
        self.write("app.py", "import nothing_of_the_sort_xyzzy\n")
        results = audit(self.root)
        self.assertEqual(
            results[UNRESOLVED], [("nothing_of_the_sort_xyzzy", "app.py", 1)]
        )
        self.assertEqual(results[THIRD_PARTY], [])

    def test_third_party_and_unresolved_are_distinguished(self):
        self.install_fake_package("installed_but_foreign")
        self.assertEqual(classify("installed_but_foreign", set()), THIRD_PARTY)
        self.assertEqual(classify("never_installed_xyzzy", set()), UNRESOLVED)

    def test_local_name_wins_over_an_installed_package(self):
        # A file in the repo shadows anything installed under the same name.
        self.install_fake_package("shadowed")
        self.write("shadowed.py", "")
        local = local_module_names(iter_sources(self.root))
        self.assertEqual(classify("shadowed", local), LOCAL)

    def test_imports_inside_functions_are_found(self):
        self.write("app.py", "def f():\n    import nothing_of_the_sort_xyzzy\n")
        results = audit(self.root)
        self.assertEqual(
            results[UNRESOLVED], [("nothing_of_the_sort_xyzzy", "app.py", 2)]
        )

    def test_imports_inside_classes_and_conditionals_are_found(self):
        self.write(
            "app.py",
            "import sys\n"
            "class C:\n"
            "    if sys.version_info:\n"
            "        import nothing_of_the_sort_xyzzy\n",
        )
        modules = [module for module, _ in imports_in(self.root / "app.py")]
        self.assertIn("nothing_of_the_sort_xyzzy", modules)

    def test_from_imports_and_dotted_names_use_the_top_level(self):
        self.write(
            "app.py",
            "from os import path\n"
            "import xml.etree.ElementTree\n"
            "from concurrent.futures import ThreadPoolExecutor\n",
        )
        found = imports_in(self.root / "app.py")
        self.assertEqual(
            [module for module, _ in found], ["os", "xml", "concurrent"]
        )

    def test_relative_imports_are_project_local(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/mod.py", "from . import sibling\n")
        self.write("pkg/deep/__init__.py", "")
        self.write("pkg/deep/mod.py", "from .. import other\nfrom .x import y\n")
        results = audit(self.root)
        self.assertEqual(results[THIRD_PARTY], [])
        self.assertEqual(results[UNRESOLVED], [])
        self.assertEqual(
            sorted(module for module, _, _ in results[LOCAL]),
            ["deep", "pkg", "pkg"],
        )

    def test_line_numbers_are_reported_accurately(self):
        self.write(
            "app.py", "import os\n\n\nimport nothing_of_the_sort_xyzzy\n"
        )
        results = audit(self.root)
        self.assertEqual(results[UNRESOLVED][0][2], 4)

    def test_generated_directories_are_skipped(self):
        self.write("app.py", "import os\n")
        for skipped in ("__pycache__", ".git", ".venv", "build", "dist"):
            self.write(f"{skipped}/junk.py", "import definitely_not_real_xyzzy\n")
        self.write("venv/lib/deep/junk.py", "import also_not_real_xyzzy\n")
        results = audit(self.root)
        self.assertEqual(results[THIRD_PARTY], [])
        self.assertEqual(results[UNRESOLVED], [])
        self.assertEqual(
            [path.name for path in iter_sources(self.root)], ["app.py"]
        )

    def test_every_import_lands_in_exactly_one_category(self):
        self.install_fake_package("some_package")
        self.write("local.py", "")
        self.write(
            "app.py",
            "import os\nimport local\nimport some_package\nimport gone_xyzzy\n",
        )
        results = audit(self.root)
        counts = {name: len(items) for name, items in results.items()}
        self.assertEqual(
            counts, {STDLIB: 1, LOCAL: 1, THIRD_PARTY: 1, UNRESOLVED: 1}
        )

    def test_findings_are_reported_in_a_stable_order(self):
        self.write("b.py", "import zzz_xyzzy\nimport aaa_xyzzy\n")
        self.write("a.py", "import mmm_xyzzy\n")
        first = audit(self.root)[UNRESOLVED]
        second = audit(self.root)[UNRESOLVED]
        self.assertEqual(first, second)
        self.assertEqual(sorted(first), sorted(second))

    def test_failure_message_names_module_file_and_line(self):
        message = describe(
            "THIRD-PARTY IMPORT", [("requests", "ledger.py", 12)]
        )
        self.assertIn("THIRD-PARTY IMPORT:", message)
        self.assertIn("module: requests", message)
        self.assertIn("file: ledger.py", message)
        self.assertIn("line: 12", message)


class TestIsolatedImport(unittest.TestCase):
    def test_ledger_imports_with_site_packages_off_the_path(self):
        """The strongest proof: not an inspection of the code, but the
        removal of any possibility. -S drops site-packages entirely, -E
        ignores PYTHONPATH, -s ignores the user site directory."""
        import subprocess

        result = subprocess.run(
            [sys.executable, "-E", "-s", "-S", "-c",
             "import ledger, sys;"
             "bad=[m for m,v in sys.modules.items()"
             " if getattr(v,'__file__',None) and 'site-packages' in v.__file__];"
             "print('site' in sys.modules, bad)"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False []")


if __name__ == "__main__":
    unittest.main()
