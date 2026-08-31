"""Tests for tools/build.py and the artifact it produces.

Two things are proved here. First, that the build is reproducible: the same
source tree yields byte-identical output from different directories, under
different umasks, at different times. Second, that the artifact actually
stands alone - it runs with the repository absent and nothing installed.

The CLI's behaviour is not re-tested here; test_cli.py already covers it.
These tests only check that the artifact is the same program.
"""

import getpass
import hashlib
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = REPO_ROOT / "tools" / "build.py"
DEPCHECK = REPO_ROOT / "tools" / "depcheck.py"
SHEBANG = b"#!/usr/bin/env python3\n"
TIMEOUT = 120


def load_builder():
    """Import tools/build.py for the few tests that need its helpers."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("ledger_build", BUILD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def build(self, name="ledger.pyz", directory=None, epoch=None):
        """Run the builder as a command, into a directory we choose."""
        target = pathlib.Path(directory or self.dir) / name
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("SOURCE_DATE_EPOCH", "PYTHONPATH")
        }
        if epoch is not None:
            env["SOURCE_DATE_EPOCH"] = str(epoch)
        result = subprocess.run(
            [sys.executable, str(BUILD), "--output", str(target)],
            capture_output=True, text=True, timeout=TIMEOUT, env=env,
            cwd=str(directory or self.dir),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return target

    def zip_of(self, path):
        data = pathlib.Path(path).read_bytes()
        self.assertTrue(data.startswith(SHEBANG))
        return zipfile.ZipFile(io.BytesIO(data[len(SHEBANG):]))


class TestDeterminism(BuildTestCase):
    def test_two_builds_in_different_directories_are_identical(self):
        first_dir = self.dir / "build-a"
        second_dir = self.dir / "build-b"
        first_dir.mkdir()
        second_dir.mkdir()
        first = self.build(directory=first_dir).read_bytes()
        second = self.build(directory=second_dir).read_bytes()

        if first != second:
            self.fail("build is not reproducible:\n"
                      + load_builder().describe_difference(first, second))
        self.assertEqual(
            hashlib.sha256(first).hexdigest(),
            hashlib.sha256(second).hexdigest(),
        )

    def test_builder_verify_mode_passes(self):
        result = subprocess.run(
            [sys.executable, str(BUILD), "--verify"],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reproducible: identical bytes", result.stdout)

    def test_repeated_builds_stay_identical(self):
        digests = set()
        for index in range(3):
            path = self.build(name=f"ledger-{index}.pyz")
            digests.add(hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(len(digests), 1, "builds drifted across runs")

    def test_output_does_not_depend_on_the_umask(self):
        original = os.umask(0o077)
        self.addCleanup(os.umask, original)
        restrictive = self.build(name="restrictive.pyz").read_bytes()
        os.umask(0o000)
        permissive = self.build(name="permissive.pyz").read_bytes()
        self.assertEqual(restrictive, permissive)

    def test_members_are_in_sorted_order(self):
        with self.zip_of(self.build()) as archive:
            names = archive.namelist()
        self.assertEqual(names, sorted(names))

    def test_timestamps_are_fixed_not_wall_clock(self):
        with self.zip_of(self.build()) as archive:
            for info in archive.infolist():
                with self.subTest(member=info.filename):
                    self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))

    def test_permissions_and_platform_are_constant(self):
        with self.zip_of(self.build()) as archive:
            for info in archive.infolist():
                with self.subTest(member=info.filename):
                    self.assertEqual(info.external_attr >> 16, 0o100644)
                    self.assertEqual(info.create_system, 3, "not pinned to Unix")
                    self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)

    def test_difference_report_names_the_differing_field(self):
        """A hash mismatch must explain itself, not just fail."""
        builder = load_builder()
        first = builder.build_bytes(REPO_ROOT, epoch=None)
        second = builder.build_bytes(REPO_ROOT, epoch=1700000000)
        report = builder.describe_difference(first, second)
        self.assertIn("date_time", report)
        self.assertIn("ledger.py", report)


class TestSourceDateEpoch(BuildTestCase):
    """SOURCE_DATE_EPOCH pins the timestamps. Same value, same bytes;
    different value, legitimately different bytes."""

    def test_unset_uses_the_zip_epoch(self):
        with self.zip_of(self.build()) as archive:
            self.assertEqual(
                archive.infolist()[0].date_time, (1980, 1, 1, 0, 0, 0)
            )

    def test_same_epoch_gives_identical_bytes(self):
        first = self.build(name="a.pyz", epoch=1700000000).read_bytes()
        second = self.build(name="b.pyz", epoch=1700000000).read_bytes()
        self.assertEqual(first, second)

    def test_different_epochs_give_different_bytes(self):
        first = self.build(name="a.pyz", epoch=1700000000).read_bytes()
        second = self.build(name="b.pyz", epoch=1800000000).read_bytes()
        self.assertNotEqual(
            first, second, "SOURCE_DATE_EPOCH had no effect on the output"
        )

    def test_epoch_is_reflected_in_member_timestamps(self):
        path = self.build(epoch=1700000000)
        with self.zip_of(path) as archive:
            for info in archive.infolist():
                self.assertEqual(info.date_time[:3], (2023, 11, 14))

    def test_epoch_before_1980_falls_back_to_the_zip_epoch(self):
        # Zip timestamps cannot predate 1980, so an earlier value cannot be
        # honoured; falling back keeps the build deterministic instead of
        # raising.
        with self.zip_of(self.build(epoch=0)) as archive:
            self.assertEqual(
                archive.infolist()[0].date_time, (1980, 1, 1, 0, 0, 0)
            )


class TestArtifactContents(BuildTestCase):
    def test_contains_exactly_the_runtime_files(self):
        with self.zip_of(self.build()) as archive:
            self.assertEqual(
                sorted(archive.namelist()), ["__main__.py", "ledger.py"]
            )

    def test_excludes_tests_tools_docs_and_caches(self):
        with self.zip_of(self.build()) as archive:
            names = archive.namelist()
        for unwanted in ("test", "tools", ".git", "__pycache__", ".pyc",
                         "DESIGN.md", "helpers", "crash_child", "depcheck"):
            with self.subTest(unwanted=unwanted):
                self.assertFalse(
                    [name for name in names if unwanted in name],
                    f"{unwanted!r} leaked into the artifact",
                )

    def test_shebang_is_fixed(self):
        data = self.build().read_bytes()
        self.assertTrue(data.startswith(b"#!/usr/bin/env python3\n"))
        self.assertNotIn(sys.executable.encode(), data[:200])

    def test_artifact_is_executable(self):
        path = self.build()
        self.assertTrue(os.access(path, os.X_OK))
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_no_absolute_paths_or_machine_metadata(self):
        path = self.build()
        raw = path.read_bytes()
        with self.zip_of(path) as archive:
            decompressed = b"".join(
                archive.read(name) for name in archive.namelist()
            )
        needles = [
            str(REPO_ROOT).encode(),
            str(self.dir).encode(),
            b"/home/",
            b"/tmp/",
            os.uname().nodename.encode(),
        ]
        try:
            needles.append(getpass.getuser().encode())
        except (KeyError, OSError):  # pragma: no cover - no login name
            pass
        for needle in needles:
            if not needle:
                continue
            with self.subTest(needle=needle):
                # The member contents are what actually ship, so every needle
                # is checked there. The compressed stream is only checked for
                # needles long enough to be meaningful: a short hostname or
                # login name (this machine's is two characters) appears inside
                # DEFLATE output by chance, which would make this test pass or
                # fail on the entropy of the archive rather than on a leak.
                self.assertNotIn(needle, decompressed, "leaked into a member")
                if len(needle) >= 6:
                    self.assertNotIn(
                        needle, raw, "leaked into the archive bytes"
                    )

    def test_bundled_source_matches_the_repository(self):
        with self.zip_of(self.build()) as archive:
            bundled = archive.read("ledger.py")
        self.assertEqual(bundled, (REPO_ROOT / "ledger.py").read_bytes())

    def test_bundled_files_pass_the_dependency_audit(self):
        extracted = self.dir / "extracted"
        with self.zip_of(self.build()) as archive:
            archive.extractall(extracted)
        result = subprocess.run(
            [sys.executable, str(DEPCHECK), "--root", str(extracted)],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("third-party (0)", result.stdout)
        self.assertIn("unresolved (0)", result.stdout)

    def test_build_does_not_mutate_the_source_tree(self):
        def snapshot():
            return {
                str(path.relative_to(REPO_ROOT)): path.stat().st_mtime_ns
                for path in sorted(REPO_ROOT.rglob("*.py"))
                if ".git" not in path.parts and "__pycache__" not in path.parts
            }

        before = snapshot()
        self.build()
        self.assertEqual(snapshot(), before, "the build touched a source file")


class TestStandaloneExecution(BuildTestCase):
    """The artifact must work with the repository nowhere in sight."""

    def setUp(self):
        super().setUp()
        self.isolated = self.dir / "isolated"
        self.isolated.mkdir()
        staging = self.dir / "staging"
        staging.mkdir()
        artifact = self.build(directory=staging)
        self.artifact = self.isolated / "ledger.pyz"
        shutil.copy2(artifact, self.artifact)
        self.artifact.chmod(0o755)
        self.store = "state.ledger"

    def run_artifact(self, *args, isolate_flags=False):
        """Execute the artifact with the repository unreachable.

        cwd is a temporary directory, PYTHONPATH is removed, and the
        repository is never named on the command line.
        """
        env = {
            key: value for key, value in os.environ.items()
            if key not in ("PYTHONPATH", "PYTHONHOME")
        }
        command = [sys.executable]
        if isolate_flags:
            command += ["-E", "-s", "-S"]
        command += [str(self.artifact), *args]
        return subprocess.run(
            command, capture_output=True, text=True, timeout=TIMEOUT,
            cwd=str(self.isolated), env=env,
        )

    def test_full_workflow_outside_the_repository(self):
        put = self.run_artifact("put", self.store, "user:42", '{"name":"Venu"}')
        self.assertEqual(put.returncode, 0, put.stderr)

        got = self.run_artifact("get", self.store, "user:42")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.strip(), '{"name":"Venu"}')

        self.run_artifact("put", self.store, "user:42", '{"name":"V2"}')
        self.run_artifact("put", self.store, "temp", "1")
        self.run_artifact("delete", self.store, "temp")

        inspected = self.run_artifact("inspect", self.store)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertIn("tail state:       CLEAN", inspected.stdout)

        compacted = self.run_artifact("compact", self.store)
        self.assertEqual(compacted.returncode, 0, compacted.stderr)
        self.assertIn("status:  compacted", compacted.stdout)

        after = self.run_artifact("scan", self.store)
        self.assertEqual(after.stdout.strip(), 'user:42\t{"name":"V2"}')

    def test_runs_under_isolated_interpreter_flags(self):
        put = self.run_artifact(
            "put", self.store, "k", '{"v":1}', isolate_flags=True
        )
        self.assertEqual(put.returncode, 0, put.stderr)
        got = self.run_artifact("get", self.store, "k", isolate_flags=True)
        self.assertEqual(got.stdout.strip(), '{"v":1}')

    def test_executes_via_its_own_shebang(self):
        result = subprocess.run(
            [str(self.artifact), "put", self.store, "k", "1"],
            capture_output=True, text=True, timeout=TIMEOUT,
            cwd=str(self.isolated),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_decoy_module_in_the_working_directory_is_not_imported(self):
        """Proves the running code came from the artifact, not from a
        ledger.py that happens to be lying around."""
        (self.isolated / "ledger.py").write_text(
            'raise RuntimeError("DECOY: the source tree was imported")\n',
            encoding="utf-8",
        )
        result = self.run_artifact("put", self.store, "k", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("DECOY", result.stderr)

    def test_the_imported_module_lives_inside_the_artifact(self):
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, 'ledger.pyz');"
             "import ledger; print(ledger.__file__)"],
            capture_output=True, text=True, timeout=TIMEOUT,
            cwd=str(self.isolated),
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ledger.pyz", result.stdout)
        self.assertNotIn(str(REPO_ROOT), result.stdout)

    def test_error_exit_codes_survive_the_packaging(self):
        missing = self.run_artifact("get", self.store, "absent")
        self.assertEqual(missing.returncode, 1)
        self.run_artifact("put", self.store, "k", "1")
        self.assertEqual(
            self.run_artifact("get", self.store, "absent").returncode, 1
        )
        self.assertEqual(self.run_artifact("--help").returncode, 0)

    def test_recovery_works_from_the_artifact(self):
        """The product's central claim, exercised through the artifact."""
        self.run_artifact("put", self.store, "committed", '{"v":1}')
        store_path = self.isolated / self.store
        with open(store_path, "ab") as handle:
            handle.write(b"\x00" * 20)  # a torn tail

        # inspect sees the damage and changes nothing.
        inspected = self.run_artifact("inspect", self.store)
        self.assertIn("tail state:       TORN", inspected.stdout)
        self.assertIn("repair required:  yes", inspected.stdout)
        damaged_size = store_path.stat().st_size

        # A read serves the committed value across the torn tail, and
        # still does not repair: reads open read-only by design.
        got = self.run_artifact("get", self.store, "committed")
        self.assertEqual(got.stdout.strip(), '{"v":1}')
        self.assertEqual(store_path.stat().st_size, damaged_size)
        self.assertIn(
            "tail state:       TORN",
            self.run_artifact("inspect", self.store).stdout,
        )

        # A write-mode command repairs automatically, and the committed
        # record survives it.
        self.assertEqual(
            self.run_artifact("put", self.store, "after", "2").returncode, 0
        )
        self.assertIn(
            "tail state:       CLEAN",
            self.run_artifact("inspect", self.store).stdout,
        )
        self.assertEqual(
            self.run_artifact("get", self.store, "committed").stdout.strip(),
            '{"v":1}',
        )
        self.assertLess(store_path.stat().st_size, damaged_size + 40)


if __name__ == "__main__":
    unittest.main()
