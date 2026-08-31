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
        keys = [json.loads(line.split("\t", 1)[0]) for line in lines]
        self.assertEqual(keys, sorted(self.ENTRIES))
        self.assertEqual(keys, ["session:a", "user:1", "user:10", "user:2", "zz"])

    def test_scan_values_are_valid_json(self):
        self.seed(self.ENTRIES)
        for line in self.ok("scan", self.path).stdout.splitlines():
            raw_key, _, raw = line.partition("\t")
            key = json.loads(raw_key)
            with self.subTest(key=key):
                self.assertEqual(json.loads(raw), self.ENTRIES[key])

    def test_scan_prefix(self):
        self.seed(self.ENTRIES)
        lines = self.ok("scan", self.path, "--prefix", "user:").stdout.splitlines()
        self.assertEqual(
            [json.loads(line.split("\t", 1)[0]) for line in lines],
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
        self.assertNotIn('"zz"\t', stdout)

    def test_scan_handles_unicode(self):
        self.ok("put", self.path, "ключ", '"значение"')
        stdout = self.ok("scan", self.path).stdout
        self.assertIn("ключ", stdout)

    def test_scan_output_is_lossless_for_awkward_keys(self):
        """A raw key containing a tab would be indistinguishable from the
        separator, and one containing a newline would silently become two
        output lines. Both columns are JSON, so neither can happen."""
        awkward = ["plain", "has\ttab", "has\nnewline", 'quote"inside',
                   "back\\slash", "ключ", "trailing "]
        for key in awkward:
            self.ok("put", self.path, key, '"v"')
        lines = self.ok("scan", self.path).stdout.splitlines()
        self.assertEqual(
            len(lines), len(awkward), "one line per key, whatever it contains"
        )
        recovered = []
        for line in lines:
            raw_key, _, raw_value = line.partition("\t")
            recovered.append(json.loads(raw_key))
            self.assertEqual(json.loads(raw_value), "v")
        self.assertEqual(sorted(recovered), sorted(awkward))


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


class TestInspect(CliTestCase):
    def fields(self, stdout):
        """Parse the report into a dict, ignoring the forensic section."""
        parsed = {}
        for line in stdout.splitlines():
            if not line or line.startswith(" ") or ":" not in line:
                continue
            name, _, value = line.partition(":")
            parsed[name.strip()] = value.strip()
        return parsed

    def make_store(self):
        self.ok("put", self.path, "a", '{"v":1}')
        self.ok("put", self.path, "a", '{"v":2}')
        self.ok("put", self.path, "b", '"x"')

    def append(self, payload):
        with open(self.path, "ab") as handle:
            handle.write(payload)

    def snapshot(self):
        """Everything inspect must leave untouched."""
        return (
            sorted(os.listdir(self.dir)),
            self.read_file(),
            os.stat(self.path).st_mtime_ns,
        )

    def test_inspect_clean_store(self):
        self.make_store()
        report = self.fields(self.ok("inspect", self.path).stdout)
        self.assertEqual(report["tail state"], "CLEAN")
        self.assertEqual(report["tail reason"], "-")
        self.assertEqual(report["valid records"], "3")
        self.assertEqual(report["live keys"], "2")
        self.assertEqual(report["generation"], "0")
        self.assertEqual(report["discarded bytes"], "0")
        self.assertEqual(report["repair required"], "no")
        self.assertEqual(
            report["valid end offset"], str(os.path.getsize(self.path))
        )

    def test_inspect_reports_reclaimable_dead_bytes(self):
        self.make_store()  # one obsolete version of "a"
        report = self.fields(self.ok("inspect", self.path).stdout)
        self.assertIn("reclaimable", report["dead bytes"])
        self.assertNotEqual(report["dead bytes"].split()[0], "0")

    def test_inspect_empty_store(self):
        self.ok("put", self.path, "k", "1")
        self.ok("delete", self.path, "k")
        report = self.fields(self.ok("inspect", self.path).stdout)
        self.assertEqual(report["live keys"], "0")
        self.assertEqual(report["tail state"], "CLEAN")

    def test_inspect_torn_store(self):
        self.make_store()
        record = ledger.encode_record(ledger.OP_PUT, 4, b"torn", b"1")
        self.append(record[:20])
        report = self.fields(self.ok("inspect", self.path).stdout)
        self.assertEqual(report["tail state"], "TORN")
        self.assertEqual(report["tail reason"], ledger.REASON_SHORT_HEADER)
        self.assertEqual(report["discarded bytes"], "20")
        self.assertEqual(report["repair required"], "yes")
        self.assertEqual(report["valid records"], "3")

    def test_inspect_corrupt_store(self):
        self.make_store()
        record = ledger.encode_record(ledger.OP_PUT, 4, b"bad", b"1")
        self.append(bytes([record[4] ^ 0x01]).join([record[:4], record[5:]]))
        report = self.fields(self.ok("inspect", self.path).stdout)
        self.assertEqual(report["tail state"], "CORRUPT")
        self.assertEqual(report["tail reason"], ledger.REASON_HEADER_CRC)
        self.assertEqual(report["repair required"], "yes")

    def test_inspect_never_modifies_the_store(self):
        """The guarantee that makes inspect safe to run on a damaged file."""
        self.make_store()
        record = ledger.encode_record(ledger.OP_PUT, 4, b"torn", b"1")
        self.append(record[:20])
        before = self.snapshot()
        for _ in range(3):
            self.ok("inspect", self.path)
        self.assertEqual(self.snapshot(), before, "inspect changed the store")

    def test_inspect_does_not_repair_a_torn_tail(self):
        self.make_store()
        record = ledger.encode_record(ledger.OP_PUT, 4, b"torn", b"1")
        self.append(record[:20])
        size = os.path.getsize(self.path)
        self.ok("inspect", self.path)
        self.assertEqual(os.path.getsize(self.path), size, "inspect truncated")
        self.assertEqual(
            ledger.replay_log(self.read_file()).tail_state, ledger.TAIL_TORN,
            "the tail must still be there for a writer to repair",
        )

    def test_inspect_creates_no_lock_file(self):
        self.make_store()
        os.remove(self.path + ".lock")
        self.ok("inspect", self.path)
        self.assertFalse(os.path.exists(self.path + ".lock"))

    def test_inspect_works_while_a_writer_holds_the_lock(self):
        self.make_store()
        holder = ledger.Ledger.open(self.path)
        self.addCleanup(holder.close)
        report = self.fields(self.ok("inspect", self.path).stdout)
        self.assertEqual(report["tail state"], "CLEAN")

    def test_inspect_does_not_compact(self):
        self.make_store()
        before = self.read_file()
        self.ok("inspect", self.path)
        self.assertEqual(self.read_file(), before)

    def test_inspect_of_a_missing_store(self):
        missing = os.path.join(self.dir, "absent.ledger")
        self.fails(ledger.EXIT_USAGE, "inspect", missing)

    def test_inspect_of_a_non_ledger_file(self):
        with open(self.path, "wb") as handle:
            handle.write(b"NOT A LEDGER FILE" + b"\x00" * 32)
        self.fails(ledger.EXIT_CORRUPT, "inspect", self.path)


class TestForensicScan(CliTestCase):
    """Beyond-the-damage scanning is informational only, and must be
    unmistakably labelled as such."""

    def build_with_valid_records_after_damage(self):
        """Three valid records, the second corrupted, so the third is
        intact on disk yet unreachable by recovery."""
        db = ledger.Ledger.open(self.path)
        for index in range(1, 4):
            db.put(f"k{index}", {"n": index})
        db.close()
        data = self.read_file()
        offsets = [ledger.FILE_HEADER_SIZE]
        for index in range(1, 4):
            header = ledger.decode_record_header(
                data[offsets[-1]:offsets[-1] + ledger.RECORD_HEADER_SIZE]
            )
            offsets.append(offsets[-1] + header.total_size)
        target = offsets[1] + ledger.RECORD_HEADER_SIZE  # record 2's payload
        damaged = bytearray(data)
        damaged[target] ^= 0xFF
        with open(self.path, "wb") as handle:
            handle.write(bytes(damaged))
        return offsets

    def test_intact_records_after_damage_are_reported_but_not_recovered(self):
        self.build_with_valid_records_after_damage()
        stdout = self.ok("inspect", self.path).stdout
        self.assertIn("UNTRUSTED / NOT RECOVERED", stdout)
        self.assertIn("record markers found:", stdout)
        self.assertIn("headers that decode:", stdout)
        # Records 2 and 3 both still carry a decodable header.
        self.assertIn("headers that decode:    2", stdout)
        # But recovery keeps only record 1.
        report = ledger.replay_log(self.read_file())
        self.assertEqual(report.valid_records, 1)

    def test_forensic_scan_does_not_change_recovery(self):
        self.build_with_valid_records_after_damage()
        before = ledger.replay_log(self.read_file())
        self.ok("inspect", self.path)
        after = ledger.replay_log(self.read_file())
        self.assertEqual(before, after, "inspect altered the replay result")

    def test_forensic_output_is_absent_on_a_clean_store(self):
        self.ok("put", self.path, "k", "1")
        stdout = self.ok("inspect", self.path).stdout
        self.assertNotIn("UNTRUSTED", stdout)
        self.assertNotIn("forensic", stdout)

    def test_forensic_scan_is_a_pure_function_of_the_bytes(self):
        self.build_with_valid_records_after_damage()
        data = self.read_file()
        report = ledger.replay_log(data)
        first = ledger.forensic_scan(data, report.valid_end_offset)
        self.assertEqual(
            first, ledger.forensic_scan(data, report.valid_end_offset)
        )
        self.assertEqual(self.read_file(), data, "scanning mutated the data")


class TestCompact(CliTestCase):
    def build_wasteful_store(self):
        """A history with obsolete versions and a tombstone."""
        for round_number in range(1, 5):
            for index in range(3):
                self.ok("put", self.path, f"key:{index}",
                        json.dumps({"round": round_number}))
        self.ok("put", self.path, "doomed", "1")
        self.ok("delete", self.path, "doomed")
        return {f"key:{i}": {"round": 4} for i in range(3)}

    def state(self):
        lines = self.ok("scan", self.path).stdout.splitlines()
        return {
            json.loads(line.partition("\t")[0]):
                json.loads(line.partition("\t")[2])
            for line in lines
        }

    def test_compact_reports_before_and_after(self):
        self.build_wasteful_store()
        result = self.ok("compact", self.path)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^records: \d+ -> \d+$")
        self.assertRegex(lines[1], r"^size:    .+ -> .+$")
        self.assertEqual(lines[2], "status:  compacted")
        self.assertEqual(result.stderr, "")

    def test_compact_preserves_the_logical_state(self):
        expected = self.build_wasteful_store()
        before = self.state()
        self.assertEqual(before, expected)
        self.ok("compact", self.path)
        self.assertEqual(self.state(), expected, "compaction changed state")

    def test_compact_shrinks_the_store(self):
        self.build_wasteful_store()
        size_before = os.path.getsize(self.path)
        records_before, records_after = (
            int(part) for part in
            self.ok("compact", self.path).stdout.splitlines()[0]
            .removeprefix("records: ").split(" -> ")
        )
        self.assertLess(records_after, records_before)
        self.assertLess(os.path.getsize(self.path), size_before)

    def test_compact_drops_the_tombstone_and_its_record(self):
        self.build_wasteful_store()
        self.ok("compact", self.path)
        ops = []
        ledger.replay_log(
            self.read_file(),
            apply=lambda o, h, k, v: ops.append((h.op, k.decode())),
        )
        self.assertTrue(all(op == ledger.OP_PUT for op, _ in ops))
        self.assertNotIn("doomed", [key for _, key in ops])

    def test_compact_leaves_a_clean_log(self):
        self.build_wasteful_store()
        self.ok("compact", self.path)
        report = ledger.replay_log(self.read_file())
        self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
        self.assertEqual(report.generation, 1)
        self.assertEqual(report.last_valid_seq, report.valid_records)

    def test_compact_is_idempotent(self):
        self.build_wasteful_store()
        self.ok("compact", self.path)
        state = self.state()
        second = self.ok("compact", self.path).stdout.splitlines()[0]
        before, after = second.removeprefix("records: ").split(" -> ")
        self.assertEqual(before, after, "an already compact store shrank")
        self.assertEqual(self.state(), state)

    def test_compact_of_an_empty_store(self):
        self.ok("put", self.path, "k", "1")
        self.ok("delete", self.path, "k")
        self.ok("compact", self.path)
        self.assertEqual(
            os.path.getsize(self.path), ledger.FILE_HEADER_SIZE
        )

    def test_writes_continue_after_compaction(self):
        expected = self.build_wasteful_store()
        self.ok("compact", self.path)
        self.ok("put", self.path, "after", '"ok"')
        expected["after"] = "ok"
        self.assertEqual(self.state(), expected)

    def test_compact_repairs_a_torn_tail_first(self):
        # compact opens read-write, where recovery is automatic.
        expected = self.build_wasteful_store()
        record = ledger.encode_record(ledger.OP_PUT, 99, b"torn", b"1")
        with open(self.path, "ab") as handle:
            handle.write(record[:20])
        self.ok("compact", self.path)
        self.assertEqual(self.state(), expected)
        self.assertEqual(
            ledger.replay_log(self.read_file()).tail_state, ledger.TAIL_CLEAN
        )

    def test_compact_of_a_missing_store_does_not_create_one(self):
        missing = os.path.join(self.dir, "absent.ledger")
        self.fails(ledger.EXIT_USAGE, "compact", missing)
        self.assertFalse(os.path.exists(missing))

    def test_compact_of_a_locked_store_exits_three(self):
        self.ok("put", self.path, "k", "1")
        holder = ledger.Ledger.open(self.path)
        self.addCleanup(holder.close)
        self.fails(ledger.EXIT_LOCKED, "compact", self.path)

    def test_compact_of_a_corrupt_store_exits_two(self):
        with open(self.path, "wb") as handle:
            handle.write(b"NOT A LEDGER FILE" + b"\x00" * 32)
        self.fails(ledger.EXIT_CORRUPT, "compact", self.path)

    def test_compact_leaves_no_temporary_file(self):
        self.build_wasteful_store()
        self.ok("compact", self.path)
        self.assertFalse(os.path.exists(self.path + ".compact"))


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
