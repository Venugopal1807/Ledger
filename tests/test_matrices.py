"""Adversarial recovery matrices (DESIGN.md sections 10, 11, 19).

Every expectation in this module comes from the oracle in ``helpers``,
which predicts recovery outcomes by arithmetic over a known history.  The
oracle never calls ``ledger.replay_log``, so these tests cannot pass by
agreeing with the implementation they are testing.

Seeded corruption uses ``random.Random(20260828)``: a fixed seed, recorded
here and in the report, so every case is reproducible.  There is no
nondeterministic randomness in this suite.
"""

import random
import time
import tracemalloc
import unittest

import helpers
import ledger
from helpers import DELETE, PUT

FIXED_SEED = 20260828

HDR = helpers.SPEC_FILE_HEADER_SIZE
REC = helpers.SPEC_RECORD_HEADER_SIZE


# --------------------------------------------------------------------------
# Fixtures: representative histories
# --------------------------------------------------------------------------

FIXTURES = {
    "A_single_put": [(PUT, b"user:1", b'{"n":1}')],
    "B_multiple_puts": [
        (PUT, b"user:1", b'{"n":1}'),
        (PUT, b"user:2", b'{"n":2}'),
        (PUT, b"user:3", b'{"n":3}'),
    ],
    "C_put_update_delete": [
        (PUT, b"session", b'{"step":1}'),
        (PUT, b"session", b'{"step":2}'),
        (DELETE, b"session", b""),
    ],
    "D_many_records": [
        (PUT, b"key%03d" % i, b'{"i":%d}' % i) for i in range(50)
    ],
    "E_large_key_and_value": [
        (PUT, b"k" * ledger.MAX_KEY_BYTES, b'"' + b"v" * 8190 + b'"'),
        (PUT, b"small", b"1"),
    ],
    "F_mixed_put_delete": [
        (PUT, b"a", b"1"),
        (PUT, b"b", b"2"),
        (DELETE, b"a", b""),
        (PUT, b"c", b"3"),
        (DELETE, b"b", b""),
        (PUT, b"a", b"9"),
        (DELETE, b"c", b""),
        (PUT, b"d", b"4"),
    ],
    "G_size_boundaries": [
        # Lengths chosen either side of byte, 255/256 and page-ish
        # boundaries, so framing arithmetic is exercised where an off-by-one
        # in a length field would hide.
        (PUT, b"k", b"1"),
        (PUT, b"k" * 255, b"v" * 255),
        (PUT, b"k" * 256, b"v" * 256),
        (PUT, b"k" * 4095, b"v" * 1023),
        (PUT, b"k" * ledger.MAX_KEY_BYTES, b"v" * 1025),
        (DELETE, b"k" * 255, b""),
    ],
}

# Full every-offset truncation is quadratic in log size, so the two large
# fixtures use a dense set of interesting offsets instead: every boundary
# and its immediate neighbourhood, every header byte, and a stride through
# the payloads. Coverage of the boundaries is complete either way.
FULL_SWEEP = ("A_single_put", "B_multiple_puts", "C_put_update_delete",
              "D_many_records", "F_mixed_put_delete")
SAMPLED_SWEEP = ("E_large_key_and_value", "G_size_boundaries")

# Bit-flip matrices are 8x the byte count, so they run over bounded
# fixtures only.
BIT_FIXTURES = ("A_single_put", "C_put_update_delete", "F_mixed_put_delete")


def interesting_offsets(history, size):
    """Boundary neighbourhoods, whole record headers, and a payload stride."""
    marks = helpers.boundaries(history)
    offsets = set()
    for mark in marks:
        for delta in (-2, -1, 0, 1, 2, REC - 1, REC, REC + 1):
            candidate = mark + delta
            if HDR <= candidate <= size:
                offsets.add(candidate)
    offsets.update(range(HDR, min(size, HDR + 4 * REC) + 1))
    offsets.update(range(HDR, size + 1, 997))
    offsets.add(size)
    return sorted(offsets)


class RecoveryInvariants(unittest.TestCase):
    """Reusable property-style assertions shared by every matrix."""

    def assert_prefix_of_history(self, delivered, history):
        """Delivered records must be an exact prefix of the true history.

        This is the invariant that rules out both resurrection (a record
        from beyond the damage) and invention (a key or value never
        written): the delivered list must match position by position.
        """
        self.assertLessEqual(len(delivered), len(history), "more records than exist")
        self.assertEqual(
            delivered,
            [tuple(entry) for entry in history[: len(delivered)]],
            "delivered records are not a prefix of the history",
        )

    def assert_report_matches(self, report, expected, history, delivered):
        state, reason, valid_records, end_offset = expected
        self.assertEqual(report.tail_state, state, "tail_state")
        self.assertEqual(report.tail_reason, reason, "tail_reason")
        self.assertEqual(report.valid_records, valid_records, "valid_records")
        self.assertEqual(report.valid_end_offset, end_offset, "valid_end_offset")
        self.assertEqual(len(delivered), valid_records, "delivered count")
        self.assertLessEqual(
            report.valid_records, len(history), "valid_records exceeds history"
        )
        self.assertEqual(report.repair_required, state != ledger.TAIL_CLEAN)
        if valid_records:
            self.assertEqual(report.last_valid_seq, valid_records)
            self.assertEqual(
                report.last_valid_offset, helpers.boundaries(history)[valid_records - 1]
            )
        else:
            self.assertEqual(report.last_valid_seq, 0)
            self.assertIsNone(report.last_valid_offset)
        self.assert_prefix_of_history(delivered, history)

    def assert_repair_is_clean_and_idempotent(self, damaged, report, history):
        """Truncating to the repair boundary must yield a clean log, and
        replaying that result again must change nothing."""
        repaired = damaged[: report.valid_end_offset]
        second, delivered = helpers.replay_collect(repaired)
        self.assertEqual(second.tail_state, ledger.TAIL_CLEAN)
        self.assertIsNone(second.tail_reason)
        self.assertFalse(second.repair_required)
        self.assertEqual(second.discarded_bytes, 0)
        self.assertEqual(second.valid_records, report.valid_records)
        self.assertEqual(second.valid_end_offset, report.valid_end_offset)
        self.assert_prefix_of_history(delivered, history)
        third, _ = helpers.replay_collect(repaired[: second.valid_end_offset])
        self.assertEqual(third, second, "repair is not idempotent")

    def assert_safe_under_damage(self, damaged, history):
        """The universal safety invariants, for damage whose exact outcome
        is not predictable (multi-bit corruption may in principle survive a
        checksum). Classification is not asserted; safety is."""
        try:
            report, delivered = helpers.replay_collect(damaged)
        except ledger.FormatError:
            return None  # file header damaged: refusing to open is safe
        except ledger.LedgerError as error:
            self.fail(f"unexpected LedgerError escaped recovery: {error!r}")

        self.assertIn(
            report.tail_state,
            (ledger.TAIL_CLEAN, ledger.TAIL_TORN, ledger.TAIL_CORRUPT),
        )
        if report.tail_state == ledger.TAIL_CLEAN:
            self.assertIsNone(report.tail_reason)
        elif report.tail_state == ledger.TAIL_TORN:
            self.assertIn(report.tail_reason, ledger.TORN_REASONS)
        else:
            self.assertIn(report.tail_reason, ledger.CORRUPT_REASONS)

        self.assertGreaterEqual(report.valid_end_offset, HDR)
        self.assertLessEqual(report.valid_end_offset, len(damaged))
        self.assertLessEqual(report.valid_records, len(history))
        self.assert_prefix_of_history(delivered, history)
        self.assert_repair_is_clean_and_idempotent(damaged, report, history)
        return report


class TestOracleIndependence(unittest.TestCase):
    """The oracle is only trustworthy if it still describes this format."""

    def test_spec_sizes_match_the_implementation(self):
        self.assertEqual(helpers.SPEC_FILE_HEADER_SIZE, ledger.FILE_HEADER_SIZE)
        self.assertEqual(helpers.SPEC_RECORD_HEADER_SIZE, ledger.RECORD_HEADER_SIZE)

    def test_minimum_record_size_guarantees_forward_progress(self):
        """Every record advances the scan by at least 33 bytes, so the
        reader's loop terminates in at most file_size/33 iterations. This is
        why 'recovery never hangs' is a structural property, not a hope."""
        self.assertEqual(
            helpers.SPEC_MIN_RECORD_SIZE, helpers.SPEC_RECORD_HEADER_SIZE + 1
        )
        smallest = ledger.encode_record(DELETE, 1, b"k")
        self.assertEqual(len(smallest), helpers.SPEC_MIN_RECORD_SIZE)
        for history in FIXTURES.values():
            marks = helpers.boundaries(history)
            for before, after in zip(marks, marks[1:]):
                self.assertGreaterEqual(after - before, helpers.SPEC_MIN_RECORD_SIZE)

    def test_oracle_boundaries_match_a_real_parse(self):
        """Cross-check the oracle's arithmetic against decoded headers once,
        so a wrong oracle cannot quietly excuse a wrong reader."""
        for name, history in FIXTURES.items():
            with self.subTest(name):
                log = helpers.build_log(history)
                marks = helpers.boundaries(history)
                self.assertEqual(marks[-1], len(log))
                for index, start in enumerate(marks[:-1]):
                    header = ledger.decode_record_header(log[start : start + REC])
                    self.assertEqual(header.seq, index + 1)
                    self.assertEqual(header.total_size, marks[index + 1] - start)


class TestTruncationMatrix(RecoveryInvariants):
    def _sweep(self, name, history, offsets):
        log = helpers.build_log(history)
        previous_end = None
        for length in offsets:
            with self.subTest(fixture=name, length=length):
                truncated = log[:length]
                report, delivered = helpers.replay_collect(truncated)
                expected = helpers.expected_truncation(history, length)
                self.assert_report_matches(report, expected, history, delivered)
                self.assertEqual(report.file_size, length)
                self.assert_repair_is_clean_and_idempotent(truncated, report, history)
                if previous_end is not None:
                    self.assertGreaterEqual(
                        report.valid_end_offset,
                        previous_end,
                        "stopping offset must not move backwards as the log grows",
                    )
                previous_end = report.valid_end_offset

    def test_full_sweep_every_byte_offset(self):
        for name in FULL_SWEEP:
            history = FIXTURES[name]
            size = helpers.boundaries(history)[-1]
            self._sweep(name, history, range(HDR, size + 1))

    def test_sampled_sweep_large_fixtures(self):
        for name in SAMPLED_SWEEP:
            history = FIXTURES[name]
            size = helpers.boundaries(history)[-1]
            self._sweep(name, history, interesting_offsets(history, size))

    def test_truncated_below_the_file_header_always_raises(self):
        log = helpers.build_log(FIXTURES["B_multiple_puts"])
        for length in range(0, HDR):
            with self.subTest(length=length):
                with self.assertRaises(ledger.FormatError):
                    ledger.replay_log(log[:length])


class TestSingleBitMatrix(RecoveryInvariants):
    def test_every_bit_of_every_record(self):
        for name in BIT_FIXTURES:
            history = FIXTURES[name]
            log = helpers.build_log(history)
            for index in range(HDR, len(log)):
                for bit in range(8):
                    with self.subTest(fixture=name, byte=index, bit=bit):
                        damaged = helpers.flip_bit(log, index, bit)
                        report, delivered = helpers.replay_collect(damaged)
                        expected = helpers.expected_single_bit_flip(history, index)
                        self.assert_report_matches(
                            report, expected, history, delivered
                        )
                        self.assert_repair_is_clean_and_idempotent(
                            damaged, report, history
                        )

    def test_every_bit_of_the_file_header_refuses_to_open(self):
        for name in BIT_FIXTURES:
            log = helpers.build_log(FIXTURES[name])
            for index in range(HDR):
                for bit in range(8):
                    with self.subTest(fixture=name, byte=index, bit=bit):
                        with self.assertRaises(ledger.FormatError):
                            ledger.replay_log(helpers.flip_bit(log, index, bit))

    def test_damage_to_the_last_record_only_loses_that_record(self):
        """The invariant that matters most for the product claim: damage at
        the tail costs the tail, never the committed history before it."""
        for name in BIT_FIXTURES:
            history = FIXTURES[name]
            log = helpers.build_log(history)
            start = helpers.boundaries(history)[-2]
            for index in range(start, len(log)):
                with self.subTest(fixture=name, byte=index):
                    report, delivered = helpers.replay_collect(
                        helpers.flip_bit(log, index, 0)
                    )
                    self.assertEqual(report.valid_records, len(history) - 1)
                    self.assert_prefix_of_history(delivered, history)


class TestSeededCorruption(RecoveryInvariants):
    """Deterministic pseudo-random damage, seeded with FIXED_SEED.

    Multi-bit damage can in principle satisfy a CRC, so these assert the
    universal safety invariants rather than an exact classification.
    """

    CASES_PER_KIND = 250

    def _fixtures(self):
        return [(name, FIXTURES[name]) for name in
                ("B_multiple_puts", "C_put_update_delete", "F_mixed_put_delete",
                 "G_size_boundaries")]

    def test_multiple_bit_flips(self):
        rng = random.Random(FIXED_SEED)
        for name, history in self._fixtures():
            log = helpers.build_log(history)
            for case in range(self.CASES_PER_KIND):
                count = rng.randint(2, 8)
                indexes = [rng.randrange(HDR, len(log)) for _ in range(count)]
                damaged = log
                for index in indexes:
                    damaged = helpers.flip_bit(damaged, index, rng.randrange(8))
                with self.subTest(fixture=name, case=case, flips=count):
                    self.assert_safe_under_damage(damaged, history)

    def test_byte_substitutions(self):
        rng = random.Random(FIXED_SEED + 1)
        for name, history in self._fixtures():
            log = helpers.build_log(history)
            for case in range(self.CASES_PER_KIND):
                damaged = log
                for _ in range(rng.randint(1, 6)):
                    damaged = helpers.substitute(
                        damaged, rng.randrange(HDR, len(log)), rng.randrange(256)
                    )
                with self.subTest(fixture=name, case=case):
                    self.assert_safe_under_damage(damaged, history)

    def test_short_byte_range_damage(self):
        rng = random.Random(FIXED_SEED + 2)
        for name, history in self._fixtures():
            log = helpers.build_log(history)
            for case in range(self.CASES_PER_KIND):
                start = rng.randrange(HDR, len(log))
                length = rng.randint(1, min(64, len(log) - start))
                damaged = helpers.damage_range(log, start, length)
                with self.subTest(fixture=name, case=case, start=start):
                    self.assert_safe_under_damage(damaged, history)

    def test_combined_header_and_payload_damage(self):
        rng = random.Random(FIXED_SEED + 3)
        for name, history in self._fixtures():
            log = helpers.build_log(history)
            marks = helpers.boundaries(history)
            for case in range(self.CASES_PER_KIND):
                index = rng.randrange(len(history))
                start, end = marks[index], marks[index + 1]
                damaged = helpers.flip_bit(
                    log, rng.randrange(start, start + REC), rng.randrange(8)
                )
                if end - start > REC:
                    damaged = helpers.flip_bit(
                        damaged, rng.randrange(start + REC, end), rng.randrange(8)
                    )
                with self.subTest(fixture=name, case=case, record=index):
                    report = self.assert_safe_under_damage(damaged, history)
                    # Damage is confined to one record, so everything before
                    # it must still be recovered.
                    self.assertEqual(report.valid_records, index)

    def test_truncation_combined_with_corruption(self):
        rng = random.Random(FIXED_SEED + 4)
        for name, history in self._fixtures():
            log = helpers.build_log(history)
            for case in range(self.CASES_PER_KIND):
                cut = rng.randrange(HDR, len(log) + 1)
                damaged = log[:cut]
                if len(damaged) > HDR:
                    damaged = helpers.flip_bit(
                        damaged, rng.randrange(HDR, len(damaged)), rng.randrange(8)
                    )
                with self.subTest(fixture=name, case=case, cut=cut):
                    self.assert_safe_under_damage(damaged, history)


class TestResourceSafety(unittest.TestCase):
    def test_absurd_length_fields_do_not_allocate(self):
        """A corrupt length must be rejected by the header checksum before
        it can size anything. Peak allocation stays proportional to the log,
        not to the 4 GiB the field claims."""
        history = FIXTURES["B_multiple_puts"]
        log = helpers.build_log(history)
        target = helpers.boundaries(history)[1]
        for field in (helpers.OFF_KEY_LEN, helpers.OFF_VAL_LEN):
            with self.subTest(field=field):
                damaged = helpers.patch_header(
                    log, target, field, (0xFFFFFFFF).to_bytes(4, "little")
                )
                tracemalloc.start()
                try:
                    report = ledger.replay_log(damaged)
                    _current, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertEqual(report.tail_state, ledger.TAIL_CORRUPT)
                self.assertLess(peak, 1 << 20, "recovery allocated unexpectedly")

    def test_recovery_of_a_large_log_is_linear_and_prompt(self):
        """A coarse hang guard. Forward progress is structural (every record
        advances at least 33 bytes); this catches a pathological regression."""
        history = [(PUT, b"key%05d" % i, b'{"i":%d}' % i) for i in range(20000)]
        log = helpers.build_log(history)
        started = time.perf_counter()
        report = ledger.replay_log(log)
        elapsed = time.perf_counter() - started
        self.assertEqual(report.valid_records, len(history))
        self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
        self.assertLess(elapsed, 5.0, f"replay took {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
