"""Compaction correctness (DESIGN.md section 14).

The central assertion throughout is the logical-state identity:

    state(before compaction) == state(after compaction)
                             == state(after reopen)

compared as exact key/value dictionaries, never as sizes or counts.
"""

import json
import os
import tempfile
import unittest

import helpers
import ledger


class CompactionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "state.ledger")
        self.temp_path = self.path + ".compact"
        self.addCleanup(self._tmp.cleanup)

    def open(self, **kwargs):
        db = ledger.Ledger.open(self.path, **kwargs)
        self.addCleanup(db.close)
        return db

    def read_file(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def replay_state(self):
        """Reconstruct the logical state from the file alone."""
        rebuilt = {}

        def apply(offset, header, key, value):
            name = key.decode("utf-8")
            if header.op == ledger.OP_PUT:
                rebuilt[name] = json.loads(value)
            else:
                rebuilt.pop(name, None)

        ledger.replay_log(self.read_file(), apply=apply)
        return rebuilt

    def assert_compaction_preserves_state(self, db):
        """Compact, then assert the state is identical in the live handle,
        in the file, and after a full reopen."""
        before = dict(db.scan())
        self.assertEqual(before, self.replay_state(), "precondition")

        result = db.compact()

        self.assertEqual(dict(db.scan()), before, "live handle changed")
        self.assertEqual(dict(db.scan()), self.replay_state(), "file changed")
        self.assertEqual(result.records_after, len(before))
        db.close()

        reopened = self.open()
        self.assertEqual(dict(reopened.scan()), before, "state lost on reopen")
        return result, before


class TestCompactionCorrectness(CompactionTestCase):
    def test_design_document_example(self):
        # PUT A=v1, PUT B=v1, PUT A=v2, DELETE B, PUT C=v1
        #   compacts to PUT A=v2, PUT C=v1
        db = self.open()
        db.put("A", "v1")
        db.put("B", "v1")
        db.put("A", "v2")
        db.delete("B")
        db.put("C", "v1")
        result, before = self.assert_compaction_preserves_state(db)
        self.assertEqual(before, {"A": "v2", "C": "v1"})
        self.assertEqual(result.records_before, 5)
        self.assertEqual(result.records_after, 2)
        self.assertNotIn("B", self.replay_state(), "deleted key resurrected")

    def test_empty_store(self):
        db = self.open()
        result, before = self.assert_compaction_preserves_state(db)
        self.assertEqual(before, {})
        self.assertEqual(result.records_after, 0)
        self.assertEqual(os.path.getsize(self.path), ledger.FILE_HEADER_SIZE)

    def test_single_key(self):
        db = self.open()
        db.put("only", {"v": 1})
        _result, before = self.assert_compaction_preserves_state(db)
        self.assertEqual(before, {"only": {"v": 1}})

    def test_repeated_updates_of_one_key(self):
        db = self.open()
        for i in range(50):
            db.put("k", {"i": i})
        result, before = self.assert_compaction_preserves_state(db)
        self.assertEqual(before, {"k": {"i": 49}})
        self.assertEqual(result.records_before, 50)
        self.assertEqual(result.records_after, 1)
        self.assertGreater(result.bytes_reclaimed, 0)

    def test_many_obsolete_versions(self):
        db = self.open()
        for round_number in range(20):
            for key in range(10):
                db.put(f"key:{key}", {"round": round_number})
        result, before = self.assert_compaction_preserves_state(db)
        self.assertEqual(len(before), 10)
        self.assertEqual(result.records_before, 200)
        self.assertEqual(result.records_after, 10)

    def test_deleted_keys_are_dropped_entirely(self):
        db = self.open()
        for i in range(10):
            db.put(f"k{i}", i)
        for i in range(0, 10, 2):
            db.delete(f"k{i}")
        _result, before = self.assert_compaction_preserves_state(db)
        self.assertEqual(sorted(before), ["k1", "k3", "k5", "k7", "k9"])
        # Neither the tombstones nor the records they shadowed remain.
        ops = []
        ledger.replay_log(
            self.read_file(),
            apply=lambda o, h, k, v: ops.append((h.op, k.decode())),
        )
        self.assertTrue(all(op == ledger.OP_PUT for op, _ in ops))
        self.assertEqual(sorted(key for _, key in ops), sorted(before))

    def test_all_keys_deleted(self):
        db = self.open()
        for i in range(5):
            db.put(f"k{i}", i)
        for i in range(5):
            db.delete(f"k{i}")
        _result, before = self.assert_compaction_preserves_state(db)
        self.assertEqual(before, {})
        self.assertEqual(os.path.getsize(self.path), ledger.FILE_HEADER_SIZE)

    def test_mixed_put_and_delete(self):
        db = self.open()
        for i in range(30):
            db.put(f"k{i % 7}", {"i": i})
            if i % 5 == 0:
                db.delete(f"k{i % 3}")
        self.assert_compaction_preserves_state(db)

    def test_unicode_keys_and_values(self):
        db = self.open()
        cases = {
            "ключ": "значение",
            "鍵": {"値": [1, 2]},
            "clé": "valeur",
            "emoji:\U0001f511": {"v": "\U0001f389"},
        }
        for key, value in cases.items():
            db.put(key, value)
            db.put(key, value)  # make each one obsolete once
        _result, before = self.assert_compaction_preserves_state(db)
        self.assertEqual(before, cases)

    def test_large_values(self):
        db = self.open()
        big = "x" * 200_000
        db.put("big", big)
        db.put("big", big + "!")
        db.put("small", 1)
        _result, before = self.assert_compaction_preserves_state(db)
        self.assertEqual(before["big"], big + "!")

    def test_maximum_documented_value_survives_compaction(self):
        db = self.open()
        biggest = "y" * (ledger.MAX_VALUE_BYTES - 2)
        db.put("max", biggest)
        db.put("max", biggest)
        _result, before = self.assert_compaction_preserves_state(db)
        self.assertEqual(before["max"], biggest)

    def test_compaction_when_already_compact(self):
        db = self.open()
        db.put("a", 1)
        db.put("b", 2)
        first = db.compact()
        after_first = self.read_file()
        second = db.compact()
        self.assertEqual(second.records_before, second.records_after)
        self.assertEqual(second.bytes_before, second.bytes_after)
        # Byte-identical apart from the generation counter, because output
        # is deterministic: same live state, same bytes.
        self.assertEqual(
            self.read_file()[ledger.FILE_HEADER_SIZE:],
            after_first[ledger.FILE_HEADER_SIZE:],
        )
        self.assertEqual(second.generation, first.generation + 1)

    def test_compaction_with_no_obsolete_records(self):
        db = self.open()
        for i in range(5):
            db.put(f"k{i}", i)
        result, _before = self.assert_compaction_preserves_state(db)
        self.assertEqual(result.records_before, result.records_after)
        self.assertEqual(result.bytes_reclaimed, 0)

    def test_multiple_compactions(self):
        db = self.open()
        expected = {}
        for round_number in range(5):
            for i in range(4):
                key = f"k{i}"
                db.put(key, {"round": round_number, "i": i})
                expected[key] = {"round": round_number, "i": i}
            db.compact()
            self.assertEqual(dict(db.scan()), expected)
            self.assertEqual(self.replay_state(), expected)
        db.close()
        self.assertEqual(dict(self.open().scan()), expected)

    def test_generation_increments_and_is_persisted(self):
        db = self.open()
        db.put("k", 1)
        self.assertEqual(ledger.replay_log(self.read_file()).generation, 0)
        for expected_generation in (1, 2, 3):
            db.compact()
            self.assertEqual(
                ledger.replay_log(self.read_file()).generation,
                expected_generation,
            )
        db.close()
        self.assertEqual(self.open().recovery_report.generation, 3)

    def test_output_is_deterministic_and_sorted(self):
        db = self.open()
        for key in ("zeta", "alpha", "mu", "beta"):
            db.put(key, key.upper())
        db.compact()
        order = []
        ledger.replay_log(
            self.read_file(),
            apply=lambda o, h, k, v: order.append(k.decode()),
        )
        self.assertEqual(order, sorted(order))


class TestCompactionSequenceNumbers(CompactionTestCase):
    """Sequence numbers restart at 1 in a compacted file.

    This is forced, not chosen: recovery requires strict +1 continuity, and
    compaction drops records, so preserving the original numbers would leave
    gaps that recovery would correctly classify as corruption.
    """

    def test_sequence_restarts_at_one(self):
        db = self.open()
        for i in range(10):
            db.put(f"k{i % 3}", i)
        db.compact()
        seqs = []
        ledger.replay_log(
            self.read_file(), apply=lambda o, h, k, v: seqs.append(h.seq)
        )
        self.assertEqual(seqs, [1, 2, 3])

    def test_put_after_compaction_continues_the_new_basis(self):
        # PUT A=1, PUT A=2, compact, PUT A=3 -> reopen must give A=3.
        db = self.open()
        db.put("A", 1)
        db.put("A", 2)
        db.compact()
        db.put("A", 3)
        db.close()
        reopened = self.open()
        self.assertEqual(reopened.get("A"), 3)
        report = ledger.replay_log(self.read_file())
        self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
        self.assertEqual(report.valid_records, 2)
        self.assertEqual(report.last_valid_seq, 2)

    def test_delete_after_compaction(self):
        db = self.open()
        db.put("a", 1)
        db.put("b", 2)
        db.compact()
        self.assertTrue(db.delete("a"))
        db.close()
        reopened = self.open()
        self.assertEqual(dict(reopened.scan()), {"b": 2})
        self.assertEqual(
            ledger.replay_log(self.read_file()).tail_state, ledger.TAIL_CLEAN
        )

    def test_no_duplicate_or_regressing_sequences_across_compactions(self):
        db = self.open()
        for round_number in range(4):
            db.put(f"k{round_number}", round_number)
            db.put("shared", round_number)
            db.compact()
            db.put("after", round_number)
            seqs = []
            report = ledger.replay_log(
                self.read_file(), apply=lambda o, h, k, v: seqs.append(h.seq)
            )
            with self.subTest(round=round_number):
                self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
                self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
                self.assertEqual(len(seqs), len(set(seqs)))

    def test_writes_after_compaction_are_recoverable_from_a_torn_tail(self):
        db = self.open()
        db.put("a", 1)
        db.put("a", 2)
        db.compact()
        db.put("b", 3)
        db.close()
        record = ledger.encode_record(ledger.OP_PUT, 3, b"never", b"1")
        with open(self.path, "ab") as handle:
            handle.write(record[:20])
        reopened = self.open()
        self.assertEqual(reopened.recovery_report.tail_state, ledger.TAIL_TORN)
        self.assertEqual(dict(reopened.scan()), {"a": 2, "b": 3})


class TestCompactionRecoveryInteraction(CompactionTestCase):
    """The existing recovery guarantees must hold on a compacted file
    exactly as they do on any other log."""

    def _compacted_store(self):
        db = self.open()
        for i in range(4):
            db.put(f"k{i}", {"i": i})
            db.put(f"k{i}", {"i": i, "updated": True})
        db.delete("k0")
        db.compact()
        state = dict(db.scan())
        db.close()
        return state

    def test_torn_tail_after_compaction(self):
        state = self._compacted_store()
        record = ledger.encode_record(ledger.OP_PUT, 4, b"torn", b'{"v":1}')
        with open(self.path, "ab") as handle:
            handle.write(record[:20])
        db = self.open()
        self.assertEqual(db.recovery_report.tail_state, ledger.TAIL_TORN)
        self.assertEqual(dict(db.scan()), state)

    def test_corrupted_header_after_compaction(self):
        state = self._compacted_store()
        data = self.read_file()
        boundary = len(data)
        record = ledger.encode_record(ledger.OP_PUT, 4, b"bad", b'{"v":1}')
        with open(self.path, "ab") as handle:
            handle.write(helpers.flip_bit(record, 4, 0))
        db = self.open()
        self.assertEqual(db.recovery_report.tail_state, ledger.TAIL_CORRUPT)
        self.assertEqual(db.recovery_report.tail_reason, ledger.REASON_HEADER_CRC)
        self.assertEqual(db.recovery_report.valid_end_offset, boundary)
        self.assertEqual(dict(db.scan()), state)

    def test_corrupted_payload_after_compaction(self):
        state = self._compacted_store()
        data = self.read_file()
        boundary = len(data)
        record = ledger.encode_record(ledger.OP_PUT, 4, b"bad", b'{"v":1}')
        damaged = helpers.flip_bit(record, ledger.RECORD_HEADER_SIZE + 1, 0)
        with open(self.path, "ab") as handle:
            handle.write(damaged)
        db = self.open()
        self.assertEqual(db.recovery_report.tail_state, ledger.TAIL_CORRUPT)
        self.assertEqual(db.recovery_report.tail_reason, ledger.REASON_PAYLOAD_CRC)
        self.assertEqual(db.recovery_report.valid_end_offset, boundary)
        self.assertEqual(dict(db.scan()), state)

    def test_compacted_file_survives_the_truncation_matrix(self):
        state = self._compacted_store()
        data = self.read_file()
        for length in range(ledger.FILE_HEADER_SIZE, len(data) + 1):
            with self.subTest(length=length):
                report = ledger.replay_log(data[:length])
                self.assertIn(
                    report.tail_state, (ledger.TAIL_CLEAN, ledger.TAIL_TORN)
                )
                self.assertLessEqual(report.valid_records, len(state))


class TestCompactionFailureHandling(CompactionTestCase):
    def _fail_writes_to_temp_file(self):
        """Make every write to the temp file fail, leaving the original
        log as the only thing on disk that matters."""
        real_open, real_write = os.open, os.write
        temp_fds = set()

        def tracking_open(path, flags, mode=0o777, **kwargs):
            fd = real_open(path, flags, mode, **kwargs)
            if str(path).endswith(".compact"):
                temp_fds.add(fd)
            return fd

        def failing_write(fd, data):
            if fd in temp_fds:
                raise OSError(28, "No space left on device")
            return real_write(fd, data)

        os.open, os.write = tracking_open, failing_write
        self.addCleanup(lambda: (setattr(os, "open", real_open),
                                 setattr(os, "write", real_write)))

    def test_failed_compaction_leaves_the_original_authoritative(self):
        db = self.open()
        for i in range(5):
            db.put(f"k{i}", i)
            db.put(f"k{i}", i * 10)
        before = dict(db.scan())
        bytes_before = os.path.getsize(self.path)

        self._fail_writes_to_temp_file()
        with self.assertRaises(ledger.CompactionError):
            db.compact()

        self.assertEqual(dict(db.scan()), before, "live state changed")
        self.assertEqual(os.path.getsize(self.path), bytes_before)
        self.assertEqual(self.replay_state(), before)

    def test_failed_compaction_removes_the_temporary_file(self):
        db = self.open()
        db.put("k", 1)
        self._fail_writes_to_temp_file()
        with self.assertRaises(ledger.CompactionError):
            db.compact()
        self.assertFalse(os.path.exists(self.temp_path))

    def test_handle_remains_usable_after_a_failed_compaction(self):
        db = self.open()
        db.put("k", 1)
        self._fail_writes_to_temp_file()
        with self.assertRaises(ledger.CompactionError):
            db.compact()
        # A failed compaction changed nothing, so unlike a failed append it
        # must not poison the handle. Writes to the log itself were never
        # patched, only writes to the temp file, so this must still work.
        db.put("k", 2)
        self.assertEqual(db.get("k"), 2)

    def test_stale_temp_file_is_removed_on_open(self):
        db = self.open()
        db.put("k", 1)
        db.close()
        with open(self.temp_path, "wb") as handle:
            handle.write(b"debris from an interrupted compaction")
        reopened = self.open()
        self.assertFalse(
            os.path.exists(self.temp_path),
            "a stale temp file must never be left behind or resumed",
        )
        self.assertEqual(reopened.get("k"), 1)

    def test_stale_temp_file_is_never_resumed(self):
        # Even a *valid* compacted log left as debris must be discarded:
        # its writer stopped at an unknown point.
        db = self.open()
        db.put("real", 1)
        db.close()
        with open(self.temp_path, "wb") as handle:
            handle.write(ledger.encode_file_header(99))
            handle.write(ledger.encode_record(ledger.OP_PUT, 1, b"ghost", b"1"))
        reopened = self.open()
        self.assertEqual(dict(reopened.scan()), {"real": 1})
        self.assertNotIn("ghost", reopened)


class TestCompactionConcurrency(CompactionTestCase):
    def test_compaction_requires_the_writer_lock(self):
        holder = self.open()
        holder.put("k", 1)
        with self.assertRaises(ledger.LockedError):
            ledger.Ledger.open(self.path)

    def test_read_only_handle_cannot_compact(self):
        self.open().put("k", 1)
        reader = ledger.Ledger.open(self.path, mode="r")
        self.addCleanup(reader.close)
        with self.assertRaises(ledger.ReadOnlyError):
            reader.compact()

    def test_closed_handle_cannot_compact(self):
        db = ledger.Ledger.open(self.path)
        db.put("k", 1)
        db.close()
        with self.assertRaises(ledger.ClosedError):
            db.compact()

    def test_reader_holding_the_old_inode_survives_replacement(self):
        writer = self.open()
        for i in range(5):
            writer.put(f"k{i}", i)
            writer.put(f"k{i}", i * 10)
        writer.delete("k0")
        snapshot = dict(writer.scan())

        reader = ledger.Ledger.open(self.path, mode="r")
        self.addCleanup(reader.close)
        self.assertEqual(dict(reader.scan()), snapshot)

        writer.compact()

        # The reader's descriptor now names an unlinked inode. It must keep
        # serving its own coherent snapshot rather than crashing or mixing
        # bytes from the two files.
        self.assertEqual(dict(reader.scan()), snapshot)
        self.assertEqual(reader.get("k1"), 10)
        self.assertIsNone(reader.get("k0"))

        fresh = ledger.Ledger.open(self.path, mode="r")
        self.addCleanup(fresh.close)
        self.assertEqual(dict(fresh.scan()), snapshot)

    def test_writer_continues_after_compaction_on_the_new_inode(self):
        db = self.open()
        db.put("a", 1)
        db.put("a", 2)
        inode_before = os.stat(self.path).st_ino
        db.compact()
        self.assertNotEqual(os.stat(self.path).st_ino, inode_before)
        db.put("b", 3)
        db.close()
        self.assertEqual(dict(self.open().scan()), {"a": 2, "b": 3})


if __name__ == "__main__":
    unittest.main()
