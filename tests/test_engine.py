"""Engine tests: open, put, get, delete, scan, close (DESIGN.md 3, 8-13, 16).

The central invariant, asserted after almost every mutation, is that the
in-memory index never claims anything the durable log does not hold.  It is
checked by re-reading the file and replaying it independently, then
comparing that reconstruction against what the live handle reports.
"""

import json
import os
import tempfile
import unittest

import helpers
import ledger


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "state.ledger")
        self.addCleanup(self._tmp.cleanup)

    def open(self, **kwargs):
        db = ledger.Ledger.open(self.path, **kwargs)
        self.addCleanup(db.close)
        return db

    def read_file(self, path=None):
        with open(path or self.path, "rb") as handle:
            return handle.read()

    def replay_index(self, path=None):
        """Rebuild the logical state from the WAL, independently of any live
        handle, by replaying the file from scratch."""
        rebuilt = {}

        def apply(offset, header, key, value):
            name = key.decode("utf-8")
            if header.op == ledger.OP_PUT:
                rebuilt[name] = json.loads(value)
            else:
                rebuilt.pop(name, None)

        ledger.replay_log(self.read_file(path), apply=apply)
        return rebuilt

    def assert_index_matches_wal(self, db):
        """The durable log must be at least as authoritative as the index."""
        self.assertEqual(dict(db.scan()), self.replay_index())


class TestRequiredCases(EngineTestCase):
    """The seven core cases from DESIGN.md section 19."""

    def test_1_empty_database(self):
        db = self.open()
        self.assertEqual(len(db), 0)
        self.assertEqual(list(db.scan()), [])
        self.assertIsNone(db.get("nothing"))
        self.assertEqual(os.path.getsize(self.path), ledger.FILE_HEADER_SIZE)
        self.assertEqual(db.recovery_report.tail_state, ledger.TAIL_CLEAN)
        self.assert_index_matches_wal(db)

    def test_2_single_put_get(self):
        db = self.open()
        db.put("user:42", {"name": "Venu"})
        self.assertEqual(db.get("user:42"), {"name": "Venu"})
        self.assert_index_matches_wal(db)

    def test_3_multiple_puts(self):
        db = self.open()
        for i in range(100):
            db.put(f"key:{i:03d}", {"i": i})
        self.assertEqual(len(db), 100)
        self.assertEqual(db.get("key:057"), {"i": 57})
        self.assert_index_matches_wal(db)

    def test_4_update_existing_key(self):
        db = self.open()
        db.put("k", {"v": 1})
        db.put("k", {"v": 2})
        db.put("k", {"v": 3})
        self.assertEqual(db.get("k"), {"v": 3})
        self.assertEqual(len(db), 1)
        # Every version is still on disk; only the last one is live.
        self.assertEqual(ledger.replay_log(self.read_file()).valid_records, 3)
        self.assert_index_matches_wal(db)

    def test_5_delete(self):
        db = self.open()
        db.put("k", 1)
        self.assertTrue(db.delete("k"))
        self.assertIsNone(db.get("k"))
        self.assertNotIn("k", db)
        self.assertEqual(len(db), 0)
        self.assert_index_matches_wal(db)

    def test_5b_delete_missing_key_writes_nothing(self):
        db = self.open()
        db.put("k", 1)
        size = os.path.getsize(self.path)
        self.assertFalse(db.delete("absent"))
        self.assertFalse(db.delete("absent"))
        self.assertEqual(os.path.getsize(self.path), size)

    def test_6_restart_persistence(self):
        db = self.open()
        db.put("a", {"x": 1})
        db.put("b", [1, 2, 3])
        db.delete("a")
        expected = dict(db.scan())
        db.close()

        reopened = self.open()
        self.assertEqual(dict(reopened.scan()), expected)
        self.assertEqual(reopened.get("b"), [1, 2, 3])
        self.assertIsNone(reopened.get("a"))
        self.assert_index_matches_wal(reopened)

    def test_7_multiple_restarts(self):
        for round_number in range(10):
            db = ledger.Ledger.open(self.path)
            db.put(f"round:{round_number}", {"n": round_number})
            if round_number % 3 == 0 and round_number:
                db.delete(f"round:{round_number - 1}")
            snapshot = dict(db.scan())
            db.close()

            check = ledger.Ledger.open(self.path)
            self.assertEqual(dict(check.scan()), snapshot)
            self.assertEqual(dict(check.scan()), self.replay_index())
            check.close()


class TestOpenLifecycle(EngineTestCase):
    def test_open_new_store_writes_only_a_header(self):
        db = self.open()
        self.assertEqual(self.read_file(), ledger.encode_file_header(0))
        self.assertEqual(db.recovery_report.valid_records, 0)

    def test_open_existing_store(self):
        db = self.open()
        db.put("k", 1)
        db.close()
        again = self.open()
        self.assertEqual(again.get("k"), 1)
        self.assertEqual(again.recovery_report.valid_records, 1)

    def test_store_file_is_owner_only(self):
        self.open()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        lock = self.path + ".lock"
        self.assertEqual(os.stat(lock).st_mode & 0o777, 0o600)

    def test_invalid_file_header_refuses_to_open(self):
        with open(self.path, "wb") as handle:
            handle.write(b"NOTALEDGERFILE!!" + b"\x00" * 16)
        with self.assertRaises(ledger.FormatError):
            ledger.Ledger.open(self.path)

    def test_invalid_file_header_does_not_destroy_the_file(self):
        original = b"important data that is not a ledger file" + b"\x00" * 8
        with open(self.path, "wb") as handle:
            handle.write(original)
        with self.assertRaises(ledger.FormatError):
            ledger.Ledger.open(self.path)
        self.assertEqual(self.read_file(), original)

    def test_failed_open_releases_the_lock(self):
        with open(self.path, "wb") as handle:
            handle.write(b"x" * 64)
        with self.assertRaises(ledger.FormatError):
            ledger.Ledger.open(self.path)
        # If the lock had leaked, this second attempt would raise LockedError
        # instead of the same FormatError.
        with self.assertRaises(ledger.FormatError):
            ledger.Ledger.open(self.path)

    def test_invalid_mode_and_durability_rejected(self):
        for kwargs in ({"mode": "w"}, {"mode": "rw+"}, {"durability": "fsync"},
                       {"durability": "none"}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    ledger.Ledger.open(self.path, **kwargs)

    def test_context_manager_closes(self):
        with ledger.Ledger.open(self.path) as db:
            db.put("k", 1)
        with self.assertRaises(ledger.ClosedError):
            db.get("k")

    def test_close_is_idempotent(self):
        db = ledger.Ledger.open(self.path)
        db.close()
        db.close()

    def test_close_reopen_cycle(self):
        for i in range(5):
            db = ledger.Ledger.open(self.path)
            db.put(f"k{i}", i)
            db.close()
        db = self.open()
        self.assertEqual(len(db), 5)


class TestTornTailRecovery(EngineTestCase):
    def _make_torn_store(self, keep_bytes):
        db = ledger.Ledger.open(self.path)
        db.put("committed:1", {"v": 1})
        db.put("committed:2", {"v": 2})
        db.close()
        partial = ledger.encode_record(ledger.OP_PUT, 3, b"never", b'{"v":3}')
        with open(self.path, "ab") as handle:
            handle.write(partial[:keep_bytes])
        return len(partial)

    def test_torn_tail_is_repaired_on_open(self):
        for keep in (1, 16, 31, 32, 33, 40):
            with self.subTest(partial_bytes=keep):
                self.setUp()
                self._make_torn_store(keep)
                db = self.open()
                self.assertEqual(db.recovery_report.tail_state, ledger.TAIL_TORN)
                self.assertEqual(db.get("committed:1"), {"v": 1})
                self.assertEqual(db.get("committed:2"), {"v": 2})
                self.assertIsNone(db.get("never"))
                self.assert_index_matches_wal(db)

    def test_repair_truncates_the_file_on_disk(self):
        self._make_torn_store(20)
        size_before = os.path.getsize(self.path)
        db = self.open()
        size_after = os.path.getsize(self.path)
        self.assertLess(size_after, size_before)
        self.assertEqual(size_after, db.recovery_report.valid_end_offset)
        self.assertEqual(
            ledger.replay_log(self.read_file()).tail_state, ledger.TAIL_CLEAN
        )

    def test_writing_after_recovery_continues_the_sequence(self):
        self._make_torn_store(20)
        db = self.open()
        db.put("after:recovery", {"ok": True})
        db.close()
        report = ledger.replay_log(self.read_file())
        self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
        self.assertEqual(report.valid_records, 3)
        again = self.open()
        self.assertEqual(again.get("after:recovery"), {"ok": True})

    def test_repair_false_refuses_and_leaves_the_file_alone(self):
        self._make_torn_store(20)
        before = self.read_file()
        with self.assertRaises(ledger.CorruptLogError) as caught:
            ledger.Ledger.open(self.path, repair=False)
        self.assertEqual(caught.exception.report.tail_state, ledger.TAIL_TORN)
        self.assertEqual(self.read_file(), before)

    def test_refused_repair_still_releases_the_lock(self):
        self._make_torn_store(20)
        with self.assertRaises(ledger.CorruptLogError):
            ledger.Ledger.open(self.path, repair=False)
        # The lock must not survive the failed open, or the store would be
        # permanently unopenable after one refused repair.
        recovered = self.open()
        self.assertEqual(recovered.get("committed:1"), {"v": 1})

    def test_corrupt_tail_is_salvaged_before_truncation(self):
        db = ledger.Ledger.open(self.path)
        db.put("committed", {"v": 1})
        db.close()
        record = ledger.encode_record(ledger.OP_PUT, 2, b"k", b'{"v":2}')
        with open(self.path, "ab") as handle:
            handle.write(helpers.flip_bit(record, 4, 0))  # break the header CRC
        db = self.open()
        self.assertEqual(db.recovery_report.tail_state, ledger.TAIL_CORRUPT)
        self.assertEqual(db.get("committed"), {"v": 1})
        salvaged = [n for n in os.listdir(self.dir) if ".salvage." in n]
        self.assertEqual(len(salvaged), 1)
        with open(os.path.join(self.dir, salvaged[0]), "rb") as handle:
            self.assertEqual(len(handle.read()), len(record))


class TestDurability(EngineTestCase):
    """Both modes must produce identical on-disk bytes; they differ only in
    whether fsync is called before the call returns."""

    def _count_fsyncs(self, durability):
        calls = []
        real_fsync = os.fsync

        def counting(fd):
            calls.append(fd)
            return real_fsync(fd)

        os.fsync = counting
        try:
            db = ledger.Ledger.open(self.path, durability=durability)
            baseline = len(calls)
            db.put("k", 1)
            db.put("k", 2)
            db.delete("k")
            db.close()
            return len(calls) - baseline
        finally:
            os.fsync = real_fsync

    def test_strict_fsyncs_every_mutation(self):
        self.assertEqual(self._count_fsyncs(ledger.DURABILITY_STRICT), 3)

    def test_relaxed_fsyncs_no_mutation(self):
        self.assertEqual(self._count_fsyncs(ledger.DURABILITY_RELAXED), 0)

    def test_both_modes_write_identical_bytes(self):
        outputs = {}
        for durability in (ledger.DURABILITY_STRICT, ledger.DURABILITY_RELAXED):
            path = os.path.join(self.dir, f"{durability}.ledger")
            db = ledger.Ledger.open(path, durability=durability)
            db.put("a", {"x": 1})
            db.put("b", [1, 2])
            db.delete("a")
            db.close()
            with open(path, "rb") as handle:
                outputs[durability] = handle.read()
        self.assertEqual(
            outputs[ledger.DURABILITY_STRICT], outputs[ledger.DURABILITY_RELAXED]
        )

    def test_relaxed_survives_close_and_reopen(self):
        db = ledger.Ledger.open(self.path, durability=ledger.DURABILITY_RELAXED)
        db.put("k", {"v": 1})
        db.close()
        again = self.open()
        self.assertEqual(again.get("k"), {"v": 1})

    def test_durability_is_reported(self):
        db = self.open(durability=ledger.DURABILITY_RELAXED)
        self.assertEqual(db.durability, "relaxed")


class TestWriteFailureAndPoisoning(EngineTestCase):
    def _fail_next_write(self, db):
        real_write = os.write
        state = {"armed": True}

        def failing(fd, data):
            if state["armed"] and fd == db._fd:
                state["armed"] = False
                raise OSError(28, "No space left on device")
            return real_write(fd, data)

        os.write = failing
        self.addCleanup(lambda: setattr(os, "write", real_write))
        return state

    def test_failed_write_does_not_update_the_index(self):
        db = self.open()
        db.put("good", 1)
        self._fail_next_write(db)
        with self.assertRaises(ledger.WriteError):
            db.put("bad", 2)
        # The index must not claim a value the log never took.
        self.assertEqual(self.replay_index(), {"good": 1})

    def test_failed_write_poisons_the_handle(self):
        db = self.open()
        db.put("good", 1)
        self._fail_next_write(db)
        with self.assertRaises(ledger.WriteError):
            db.put("bad", 2)
        for name, operation in (
            ("put", lambda: db.put("x", 1)),
            ("get", lambda: db.get("good")),
            ("delete", lambda: db.delete("good")),
            ("scan", lambda: list(db.scan())),
            ("len", lambda: len(db)),
        ):
            with self.subTest(operation=name):
                with self.assertRaises(ledger.WriteError):
                    operation()

    def test_reopening_after_a_poisoned_handle_recovers(self):
        db = self.open()
        db.put("good", 1)
        self._fail_next_write(db)
        with self.assertRaises(ledger.WriteError):
            db.put("bad", 2)
        db.close()
        again = self.open()
        self.assertEqual(again.get("good"), 1)
        self.assertIsNone(again.get("bad"))
        self.assert_index_matches_wal(again)

    def test_closing_a_poisoned_handle_releases_the_lock(self):
        db = self.open()
        db.put("good", 1)
        self._fail_next_write(db)
        with self.assertRaises(ledger.WriteError):
            db.put("bad", 2)
        db.close()
        successor = self.open()
        successor.put("after", 2)
        self.assertEqual(successor.get("good"), 1)

    def test_failed_delete_does_not_remove_from_the_index(self):
        db = self.open()
        db.put("k", 1)
        self._fail_next_write(db)
        with self.assertRaises(ledger.WriteError):
            db.delete("k")
        self.assertEqual(self.replay_index(), {"k": 1})


class TestLocking(EngineTestCase):
    def test_second_writer_is_rejected(self):
        first = self.open()
        with self.assertRaises(ledger.LockedError):
            ledger.Ledger.open(self.path)
        first.close()
        # The lock is released on close, so a later writer succeeds.
        second = self.open()
        second.put("k", 1)

    def test_reader_needs_no_lock(self):
        writer = self.open()
        writer.put("k", {"v": 1})
        reader = self.open(mode="r")
        self.assertEqual(reader.get("k"), {"v": 1})

    def test_reader_cannot_mutate(self):
        self.open().put("k", 1)
        reader = ledger.Ledger.open(self.path, mode="r")
        self.addCleanup(reader.close)
        with self.assertRaises(ledger.ReadOnlyError):
            reader.put("k", 2)
        with self.assertRaises(ledger.ReadOnlyError):
            reader.delete("k")

    def test_reader_sees_a_snapshot_not_later_writes(self):
        writer = self.open()
        writer.put("k", 1)
        reader = self.open(mode="r")
        writer.put("k", 2)
        self.assertEqual(reader.get("k"), 1, "reader must not see later writes")
        self.assertEqual(writer.get("k"), 2)

    def test_reader_of_a_torn_log_sees_the_valid_prefix(self):
        # A reader racing a writer routinely catches a partial record. That
        # is a snapshot, not damage, so it must not raise.
        db = ledger.Ledger.open(self.path)
        db.put("committed", {"v": 1})
        db.close()
        partial = ledger.encode_record(ledger.OP_PUT, 2, b"never", b"1")
        with open(self.path, "ab") as handle:
            handle.write(partial[:20])
        size_before = os.path.getsize(self.path)
        reader = self.open(mode="r")
        self.assertEqual(reader.get("committed"), {"v": 1})
        self.assertIsNone(reader.get("never"))
        self.assertEqual(
            os.path.getsize(self.path), size_before, "reader modified the file"
        )

    def test_reader_of_a_corrupt_log_raises(self):
        db = ledger.Ledger.open(self.path)
        db.put("committed", {"v": 1})
        db.close()
        record = ledger.encode_record(ledger.OP_PUT, 2, b"k", b"1")
        with open(self.path, "ab") as handle:
            handle.write(helpers.flip_bit(record, 4, 0))
        with self.assertRaises(ledger.CorruptLogError):
            ledger.Ledger.open(self.path, mode="r")

    def test_read_mode_does_not_create_a_missing_store(self):
        with self.assertRaises(FileNotFoundError):
            ledger.Ledger.open(os.path.join(self.dir, "absent.ledger"), mode="r")


class TestValuesAndKeys(EngineTestCase):
    def test_json_round_trip_types(self):
        db = self.open()
        cases = {
            "dict": {"a": 1, "b": [1, 2], "c": {"d": None}},
            "list": [1, 2.5, "three", True, None],
            "str": "hello",
            "int": 42,
            "negative": -17,
            "float": 3.5,
            "bool": False,
            "null": None,
            "empty_dict": {},
            "empty_list": [],
            "empty_str": "",
        }
        for key, value in cases.items():
            db.put(key, value)
        db.close()
        again = self.open()
        for key, value in cases.items():
            with self.subTest(key):
                self.assertEqual(again.get(key), value)

    def test_unicode_keys_and_values(self):
        db = self.open()
        cases = [
            ("ключ", "значен"),
            ("鍵", "値"),
            ("clé", "valeur"),
            ("emoji:\U0001f511", {"v": "\U0001f389"}),
            ("tab\tkey", "newline\nvalue"),
        ]
        for key, value in cases:
            db.put(key, value)
        db.close()
        again = self.open()
        for key, value in cases:
            with self.subTest(key=key):
                self.assertEqual(again.get(key), value)

    def test_mutating_a_returned_object_cannot_change_the_store(self):
        db = self.open()
        db.put("k", {"items": [1, 2]})
        fetched = db.get("k")
        fetched["items"].append(999)
        fetched["injected"] = True
        self.assertEqual(db.get("k"), {"items": [1, 2]})
        self.assertIsNot(db.get("k"), db.get("k"))

    def test_mutating_the_object_passed_to_put_cannot_change_the_store(self):
        db = self.open()
        value = {"items": [1, 2]}
        db.put("k", value)
        value["items"].append(999)
        self.assertEqual(db.get("k"), {"items": [1, 2]})

    def test_scan_values_are_independent_objects(self):
        db = self.open()
        db.put("k", {"n": [1]})
        for _key, value in db.scan():
            value["n"].append(2)
        self.assertEqual(db.get("k"), {"n": [1]})

    def test_maximum_documented_value_size(self):
        db = self.open()
        # A JSON string encodes to len+2 bytes, so this lands exactly on the
        # documented 8 MiB limit.
        biggest = "x" * (ledger.MAX_VALUE_BYTES - 2)
        db.put("big", biggest)
        db.close()
        again = self.open()
        self.assertEqual(again.get("big"), biggest)

    def test_value_over_the_limit_is_rejected(self):
        db = self.open()
        size_before = os.path.getsize(self.path)
        with self.assertRaises(ValueError):
            db.put("too-big", "x" * (ledger.MAX_VALUE_BYTES + 1))
        self.assertEqual(os.path.getsize(self.path), size_before)

    def test_maximum_key_size(self):
        db = self.open()
        key = "k" * ledger.MAX_KEY_BYTES
        db.put(key, 1)
        db.close()
        self.assertEqual(self.open().get(key), 1)

    def test_invalid_keys_rejected(self):
        db = self.open()
        for key in (b"bytes", 42, None, ("tuple",)):
            with self.subTest(key=key), self.assertRaises(TypeError):
                db.put(key, 1)
        for key in ("", "k" * (ledger.MAX_KEY_BYTES + 1)):
            with self.subTest(key_length=len(key)), self.assertRaises(ValueError):
                db.put(key, 1)

    def test_non_json_values_rejected_without_writing(self):
        db = self.open()
        size_before = os.path.getsize(self.path)
        for value in ({1, 2}, b"bytes", object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(TypeError):
                    db.put("k", value)
        for value in (float("nan"), float("inf"), [float("nan")]):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    db.put("k", value)
        self.assertEqual(os.path.getsize(self.path), size_before)

    def test_get_of_missing_key_returns_default(self):
        db = self.open()
        self.assertIsNone(db.get("absent"))
        self.assertEqual(db.get("absent", default={}), {})
        self.assertEqual(db.get("absent", "fallback"), "fallback")

    def test_stored_none_is_distinguishable_from_missing(self):
        db = self.open()
        db.put("present", None)
        self.assertIsNone(db.get("present"))
        self.assertIn("present", db)
        self.assertNotIn("absent", db)


class TestScan(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.db = self.open()
        for key in ("user:2", "user:1", "session:a", "user:10", "zz"):
            self.db.put(key, key.upper())

    def test_scan_is_sorted_by_key(self):
        self.assertEqual(
            [key for key, _ in self.db.scan()],
            ["session:a", "user:1", "user:10", "user:2", "zz"],
        )

    def test_scan_prefix(self):
        self.assertEqual(
            [key for key, _ in self.db.scan(prefix="user:")],
            ["user:1", "user:10", "user:2"],
        )

    def test_scan_prefix_with_no_matches(self):
        self.assertEqual(list(self.db.scan(prefix="nothing")), [])

    def test_scan_excludes_deleted_keys(self):
        self.db.delete("user:1")
        self.assertNotIn("user:1", [key for key, _ in self.db.scan()])

    def test_scan_snapshot_is_stable_across_mutation(self):
        iterator = self.db.scan()
        first = next(iterator)
        self.db.delete("zz")
        self.db.put("aaa", 1)
        remaining = [key for key, _ in iterator]
        self.assertEqual(first[0], "session:a")
        self.assertIn("zz", remaining, "scan must iterate its own snapshot")
        self.assertNotIn("aaa", remaining)

    def test_scan_rejects_non_string_prefix(self):
        with self.assertRaises(TypeError):
            list(self.db.scan(prefix=b"user:"))


class TestIndexMatchesWalInvariant(EngineTestCase):
    """index == replay(WAL), maintained through an arbitrary workload."""

    def test_invariant_holds_after_every_mutation(self):
        db = self.open()
        operations = [
            ("put", "a", 1), ("put", "b", {"x": [1, 2]}), ("put", "a", 2),
            ("delete", "b", None), ("put", "c", "three"),
            ("delete", "absent", None), ("put", "b", None),
            ("delete", "a", None), ("put", "d", [1, 2, 3]),
            ("put", "c", {"nested": {"deep": True}}), ("delete", "c", None),
        ]
        for kind, key, value in operations:
            if kind == "put":
                db.put(key, value)
            else:
                db.delete(key)
            with self.subTest(op=kind, key=key):
                self.assert_index_matches_wal(db)

    def test_invariant_holds_across_reopen(self):
        db = self.open()
        for i in range(30):
            db.put(f"k{i % 7}", {"i": i})
            if i % 5 == 0:
                db.delete(f"k{i % 3}")
        expected = dict(db.scan())
        db.close()
        again = self.open()
        self.assertEqual(dict(again.scan()), expected)
        self.assertEqual(dict(again.scan()), self.replay_index())

    def test_sequence_numbers_are_contiguous_after_reopen(self):
        db = self.open()
        db.put("a", 1)
        db.put("b", 2)
        db.close()
        again = self.open()
        again.put("c", 3)
        again.close()
        report = ledger.replay_log(self.read_file())
        self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
        self.assertEqual(report.valid_records, 3)
        self.assertEqual(report.last_valid_seq, 3)


if __name__ == "__main__":
    unittest.main()
