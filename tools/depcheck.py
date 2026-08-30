#!/usr/bin/env python3
"""Print this repository's complete import inventory.

Specific to Ledger, not a general Python analyser. It answers one
question - what does this source tree import, and where does each of those
come from - so the zero-dependency claim can be read rather than trusted.

    python3 tools/depcheck.py            # human-readable inventory
    python3 tools/depcheck.py --json     # machine-readable, same data

Exit status is 0 when every import is standard library or project-local,
and 1 when anything is third-party or unresolved.

This reads source. What is installed in the environment is a different
question, and not the one that matters: `pip list` describes a machine,
whereas the imports in these files describe the project.

Written independently of tests/test_no_dependencies.py on purpose. The two
implementations cross-check each other; a bug in either is caught by the
other disagreeing.
"""

import argparse
import ast
import importlib.util
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Directories that hold generated or vendored content rather than sources.
EXCLUDED = frozenset({
    ".git", "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    "build", "dist", "node_modules", ".eggs",
})

STDLIB = "stdlib"
LOCAL = "project-local"
THIRD_PARTY = "third-party"
UNRESOLVED = "unresolved"

CATEGORIES = (STDLIB, LOCAL, THIRD_PARTY, UNRESOLVED)
FAILING = (THIRD_PARTY, UNRESOLVED)

# What each file is for. Only the production role ships.
ROLES = (
    ("production", lambda relative: relative.parent == pathlib.Path(".")),
    ("tools", lambda relative: relative.parts[0] == "tools"),
    ("tests", lambda relative: relative.parts[0] == "tests"),
    ("demo", lambda relative: relative.parts[0] == "demo"),
)


def role_of(relative):
    for name, matches in ROLES:
        if matches(relative):
            return name
    return "other"


def source_files(root):
    return sorted(
        path for path in root.rglob("*.py")
        if not EXCLUDED.intersection(path.relative_to(root).parts)
    )


def local_names(paths):
    """Module names this repository itself provides."""
    return {
        path.parent.name if path.name == "__init__.py" else path.stem
        for path in paths
    }


def top_level_imports(path):
    """(module, line) for every import, including ones inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.partition(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative: names a package inside this tree by construction.
                index = min(node.level - 1, len(path.parents) - 1)
                yield path.parents[index].name, node.lineno
            elif node.module:
                yield node.module.partition(".")[0], node.lineno


def categorise(module, local):
    if module in local:
        return LOCAL
    if module in sys.stdlib_module_names:
        return STDLIB
    try:
        found = importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        found = False
    return THIRD_PARTY if found else UNRESOLVED


def inventory(root):
    """Everything the tool knows, as plain sorted data."""
    paths = source_files(root)
    local = local_names(paths)
    records = []
    for path in paths:
        relative = path.relative_to(root)
        for module, line in top_level_imports(path):
            records.append({
                "module": module,
                "file": relative.as_posix(),
                "line": line,
                "role": role_of(relative),
                "category": categorise(module, local),
            })
    records.sort(key=lambda record: (record["file"], record["module"],
                                     record["line"]))
    return {
        "files": [path.relative_to(root).as_posix() for path in paths],
        "imports": records,
    }


def render(data):
    """Deterministic text: sorted files, sorted modules, stable layout."""
    lines = ["Ledger import inventory", "=" * 23, ""]
    records = data["imports"]

    for category in CATEGORIES:
        modules = sorted({r["module"] for r in records
                          if r["category"] == category})
        lines.append(f"{category} ({len(modules)})")
        if not modules:
            lines.append("  none")
        for module in modules:
            users = sorted({r["file"] for r in records
                            if r["category"] == category
                            and r["module"] == module})
            lines.append(f"  {module:<16}{', '.join(users)}")
        lines.append("")

    lines.append("by file")
    for path in data["files"]:
        modules = sorted({r["module"] for r in records if r["file"] == path})
        role = next((r["role"] for r in records if r["file"] == path), "other")
        lines.append(f"  {path:<30}[{role}] {', '.join(modules) or '-'}")
    lines.append("")

    violations = [r for r in records if r["category"] in FAILING]
    if violations:
        lines.append("VIOLATIONS")
        for record in violations:
            lines.append(
                f"  {record['category'].upper()} IMPORT:\n"
                f"    module: {record['module']}\n"
                f"    file: {record['file']}\n"
                f"    line: {record['line']}"
            )
    else:
        lines.append("no third-party or unresolved imports")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="emit the inventory as JSON")
    parser.add_argument("--root", default=str(REPO_ROOT),
                        help="repository root to scan")
    args = parser.parse_args(argv)

    data = inventory(pathlib.Path(args.root).resolve())
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(render(data))
    return 1 if any(r["category"] in FAILING for r in data["imports"]) else 0


if __name__ == "__main__":
    sys.exit(main())
