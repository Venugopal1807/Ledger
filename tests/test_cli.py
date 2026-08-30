"""CLI tests, run the way a user runs the CLI.

Every case here spawns `python3 -m ledger ...` as a real subprocess and
checks stdout, stderr and the exit code. Calling the command functions
directly would miss exactly the things a CLI gets wrong: argument parsing,
stream routing, and exit status.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import ledger

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 30


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "state.ledger")
        self.addCleanup(self._tmp.cleanup)

    def run_cli(self, *args):
        """Invoke the CLI as a user would, and return the completed run."""
        return subprocess.run(
            [sys.executable, "-m", "ledger", *args],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=TIMEOUT,
        )

    def ok(self, *args):
        result = self.run_cli(*args)
        self.assertEqual(
            result.returncode, ledger.EXIT_OK,
            f"expected success, got {result.returncode}\n{result.stderr}",
        )
        return result

    def fails(self, code, *args):
        result = self.run_cli(*args)
        self.assertEqual(
            result.returncode, code,
            f"expected exit {code}, got {result.returncode}\n{result.stderr}",
        )
        self.assertEqual(result.stdout, "", "errors must not write to stdout")
        self.assertTrue(result.stderr.strip(), "an error must explain itself")
        self.assertNotIn("Traceback", result.stderr)
        return result

    def read_file(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def seed(self, entries):
        for key, value in entries.items():
            self.ok("put", self.path, key, json.dumps(value))


class TestPut(CliTestCase):
    def test_put_creates_the_store_and_is_silent(self):
        result = self.ok("put", self.path, "k", '{"a":1}')
        self.assertEqual(result.stdout, "", "success is silent")
        self.assertTrue(os.path.exists(self.path))

    def test_put_accepts_every_json_shape(self):
        cases = {
            "obj": '{"name":"Venu","active":true}',
            "num": "42",
            "arr": '["python","storage"]',
            "str": '"hello"',
            "bool": "false",
            "null": "null",
            "nested": '{"a":{"b":[1,2,{"c":null}]}}',
        }
        for key, raw in cases.items():
            with self.subTest(key=key):
                self.ok("put", self.path, key, raw)
                out = self.ok("get", self.path, key).stdout.strip()
                self.assertEqual(json.loads(out), json.loads(raw))

    def test_put_overwrites(self):
        self.ok("put", self.path, "k", "1")
        self.ok("put", self.path, "k", "2")
        self.assertEqual(self.ok("get", self.path, "k").stdout.strip(), "2")

    def test_invalid_json_is_a_usage_error(self):
        for raw in ("{not json", "", "{'single':'quotes'}", "undefined", "[1,"):
            with self.subTest(raw=raw):
                result = self.fails(ledger.EXIT_USAGE, "put", self.path, "k", raw)
                self.assertIn("not valid JSON", result.stderr)

    def test_invalid_json_writes_nothing(self):
        self.ok("put", self.path, "good", "1")
        size = os.path.getsize(self.path)
        self.fails(ledger.EXIT_USAGE, "put", self.path, "k", "{bad")
        self.assertEqual(os.path.getsize(self.path), size)

    def test_empty_key_is_a_usage_error(self):
        self.fails(ledger.EXIT_USAGE, "put", self.path, "", "1")

    def test_nan_is_rejected_as_a_usage_error(self):
        # Python's json accepts NaN on input but it is not interoperable
        # JSON, so the store refuses it.
        self.fails(ledger.EXIT_USAGE, "put", self.path, "k", "NaN")


class TestGet(CliTestCase):
    def test_get_prints_json_on_stdout(self):
        self.ok("put", self.path, "user:42", '{"name":"Venu","active":true}')
        result = self.ok("get", self.path, "user:42")
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout), {"name": "Venu", "active": True}
        )

    def test_get_output_is_one_line_of_valid_json(self):
        self.ok("put", self.path, "k", '{"a":[1,2,3]}')
        stdout = self.ok("get", self.path, "k").stdout
        self.assertEqual(stdout.count("\n"), 1)
        json.loads(stdout)

    def test_missing_key(self):
        self.ok("put", self.path, "present", "1")
        result = self.fails(ledger.EXIT_USAGE, "get", self.path, "absent")
        self.assertIn("key not found", result.stderr)

    def test_stored_null_is_not_a_missing_key(self):
        # A stored null and an absent key both read back as None through the
        # API default, so the CLI must tell them apart.
        self.ok("put", self.path, "nothing", "null")
        self.assertEqual(self.ok("get", self.path, "nothing").stdout.strip(), "null")
        self.fails(ledger.EXIT_USAGE, "get", self.path, "absent")

    def test_get_does_not_create_or_modify_the_store(self):
        self.fails(ledger.EXIT_USAGE, "get", self.path, "k")
        self.assertFalse(os.path.exists(self.path))

    def test_get_does_not_take_the_writer_lock(self):
        self.ok("put", self.path, "k", "1")
        holder = ledger.Ledger.open(self.path)
        self.addCleanup(holder.close)
        self.assertEqual(self.ok("get", self.path, "k").stdout.strip(), "1")


class TestDelete(CliTestCase):
    def test_delete_is_silent_on_success(self):
        self.ok("put", self.path, "k", "1")
        result = self.ok("delete", self.path, "k")
        self.assertEqual(result.stdout, "", "success is silent")
        self.fails(ledger.EXIT_USAGE, "get", self.path, "k")

    def test_delete_missing_key_is_a_usage_error(self):
        self.ok("put", self.path, "other", "1")
        result = self.fails(ledger.EXIT_USAGE, "delete", self.path, "absent")
        self.assertIn("key not found", result.stderr)

    def test_delete_twice_reports_the_second_as_missing(self):
        self.ok("put", self.path, "k", "1")
        self.ok("delete", self.path, "k")
        self.fails(ledger.EXIT_USAGE, "delete", self.path, "k")


class TestScan(CliTestCase):
    ENTRIES = {
        "user:2": {"n": 2},
        "user:1": {"n": 1},
        "session:a": "A",
        "user:10": {"n": 10},
        "zz": [1, 2],
    }

    def test_scan_is_sorted_and_tab_separated(self):
        self.seed(self.ENTRIES)
        lines = self.ok("scan", self.path).stdout.splitlines()
        keys = [line.split("\t", 1)[0] for line in lines]
        self.assertEqual(keys, sorted(self.ENTRIES))
        self.assertEqual(keys, ["session:a", "user:1", "user:10", "user:2", "zz"])

    def test_scan_values_are_valid_json(self):
        self.seed(self.ENTRIES)
        for line in self.ok("scan", self.path).stdout.splitlines():
            key, _, raw = line.partition("\t")
            with self.subTest(key=key):
                self.assertEqual(json.loads(raw), self.ENTRIES[key])

    def test_scan_prefix(self):
        self.seed(self.ENTRIES)
        lines = self.ok("scan", self.path, "--prefix", "user:").stdout.splitlines()
        self.assertEqual(
            [line.split("\t", 1)[0] for line in lines],
            ["user:1", "user:10", "user:2"],
        )

    def test_scan_of_an_empty_store_prints_nothing(self):
        self.ok("put", self.path, "k", "1")
        self.ok("delete", self.path, "k")
        result = self.ok("scan", self.path)
        self.assertEqual(result.stdout, "")

    def test_scan_excludes_deleted_keys(self):
        self.seed(self.ENTRIES)
        self.ok("delete", self.path, "zz")
        stdout = self.ok("scan", self.path).stdout
        self.assertNotIn("zz\t", stdout)

    def test_scan_handles_unicode(self):
        self.ok("put", self.path, "ключ", '"значение"')
        stdout = self.ok("scan", self.path).stdout
        self.assertIn("ключ", stdout)


class TestExitCodes(CliTestCase):
    def test_nonexistent_store(self):
        missing = os.path.join(self.dir, "absent.ledger")
        for args in (("get", missing, "k"), ("scan", missing),
                     ("delete", missing, "k")):
            with self.subTest(command=args[0]):
                result = self.fails(ledger.EXIT_USAGE, *args)
                self.assertIn("no such store", result.stderr)

    def test_nonexistent_directory_for_put(self):
        deep = os.path.join(self.dir, "no", "such", "dir", "s.ledger")
        self.fails(ledger.EXIT_USAGE, "put", deep, "k", "1")

    def test_corrupt_store_exits_two(self):
        with open(self.path, "wb") as handle:
            handle.write(b"NOT A LEDGER FILE" + b"\x00" * 32)
        for args in (("get", self.path, "k"), ("scan", self.path),
                     ("put", self.path, "k", "1")):
            with self.subTest(command=args[0]):
                self.fails(ledger.EXIT_CORRUPT, *args)

    def test_corrupt_store_is_not_destroyed_by_the_cli(self):
        original = b"NOT A LEDGER FILE" + b"\x00" * 32
        with open(self.path, "wb") as handle:
            handle.write(original)
        self.fails(ledger.EXIT_CORRUPT, "get", self.path, "k")
        self.assertEqual(self.read_file(), original)

    def test_locked_store_exits_three(self):
        self.ok("put", self.path, "k", "1")
        holder = ledger.Ledger.open(self.path)
        self.addCleanup(holder.close)
        for args in (("put", self.path, "k", "2"),
                     ("delete", self.path, "k")):
            with self.subTest(command=args[0]):
                result = self.fails(ledger.EXIT_LOCKED, *args)
                self.assertIn("lock", result.stderr.lower())

    def test_write_failure_exits_four(self):
        """A real ENOSPC, produced from outside the process.

        File permissions cannot force this: the suite often runs as root,
        which bypasses them entirely. Pointing the store at /dev/full
        through a symlink gives a genuine ENOSPC on every write, while
        keeping the lock sidecar in the temp directory rather than /dev.
        """
        if not os.path.exists("/dev/full"):
            self.skipTest("/dev/full is not available on this platform")
        full = os.path.join(self.dir, "full.ledger")
        os.symlink("/dev/full", full)
        result = self.fails(ledger.EXIT_IO, "put", full, "k", "1")
        self.assertIn("No space left on device", result.stderr)

    def test_delete_does_not_create_a_missing_store(self):
        missing = os.path.join(self.dir, "absent.ledger")
        self.fails(ledger.EXIT_USAGE, "delete", missing, "k")
        self.assertFalse(
            os.path.exists(missing), "delete must not create the store"
        )

    def test_usage_errors_exit_one_not_two(self):
        # argparse would exit 2, which this CLI reserves for a corrupt store.
        for args in (("put",), ("get", self.path), ("nosuchcommand",),
                     ("put", self.path, "k"), ()):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, ledger.EXIT_USAGE)
                self.assertEqual(result.stdout, "")

    def test_help_exits_zero(self):
        for args in (("--help",), ("put", "--help")):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, ledger.EXIT_OK)
                self.assertTrue(result.stdout.strip())

    def test_exit_codes_are_the_documented_constants(self):
        self.assertEqual(
            (ledger.EXIT_OK, ledger.EXIT_USAGE, ledger.EXIT_CORRUPT,
             ledger.EXIT_LOCKED, ledger.EXIT_IO),
            (0, 1, 2, 3, 4),
        )


class TestStreamDiscipline(CliTestCase):
    def test_success_output_goes_only_to_stdout(self):
        self.ok("put", self.path, "k", "1")
        for args in (("get", self.path, "k"), ("scan", self.path)):
            with self.subTest(command=args[0]):
                result = self.ok(*args)
                self.assertEqual(result.stderr, "", "success must not warn")

    def test_errors_never_produce_a_traceback(self):
        with open(self.path, "wb") as handle:
            handle.write(b"garbage" * 8)
        for args in (("get", self.path, "k"), ("scan", self.path),
                     ("put", self.path, "k", "1"),
                     ("get", os.path.join(self.dir, "absent"), "k")):
            with self.subTest(command=args):
                result = self.run_cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(
                    len(result.stderr.strip().splitlines()), 1,
                    "an expected error is one concise line",
                )

    def test_scan_output_is_pipeable(self):
        self.seed({"a": 1, "b": 2})
        result = self.ok("scan", self.path)
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertNotIn("\x1b[", result.stdout, "no terminal escape codes")


class TestEntryPoints(CliTestCase):
    def test_module_and_script_entry_points_agree(self):
        self.ok("put", self.path, "k", '{"v":1}')
        module = self.ok("get", self.path, "k").stdout
        script = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "ledger.py"),
             "get", self.path, "k"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=TIMEOUT,
        )
        self.assertEqual(script.returncode, 0, script.stderr)
        self.assertEqual(script.stdout, module)


if __name__ == "__main__":
    unittest.main()
