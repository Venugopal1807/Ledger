"""Tests for the recovery reader (DESIGN.md sections 10 and 11).

Every fixture is built byte by byte; no writer is involved.  Each test
asserts the whole recovery state - classification, reason, stopping offset,
valid record count, last valid sequence, last valid record offset and
whether repair is required - rather than just the classification, so a test
cannot pass while the reader stops in the wrong place.
"""

import struct
import unittest

import ledger

# Field offsets inside a record header, used to corrupt one field at a time.
OFF_MAGIC = 0
OFF_VERSION = 4
OFF_OP = 5
OFF_FLAGS = 6
OFF_SEQ = 8
OFF_KEY_LEN = 16
OFF_VAL_LEN = 20
OFF_PAYLOAD_CRC = 24
OFF_HEADER_CRC = 28

HDR = ledger.FILE_HEADER_SIZE
REC = ledger.RECORD_HEADER_SIZE


def build_log(records, generation=0):
    """Build a log from ``(op, key, value)`` triples with sequential seqs."""
    parts = [ledger.encode_file_header(generation)]
    for index, (op, key, value) in enumerate(records, start=ledger.FIRST_SEQ):
        parts.append(ledger.encode_record(op, index, key, value))
    return b"".join(parts)


def record_offsets(records):
    """Start offset of each record, plus the offset just past the last."""
    offsets = [HDR]
    for _op, key, value in records:
        offsets.append(offsets[-1] + REC + len(key) + len(value))
    return offsets


def rechecksum(header):
    prefix = header[:OFF_HEADER_CRC]
    return prefix + struct.pack("<I", ledger.crc32(prefix))


def patch_header(log, offset, field, packed, fix_crc=True):
    """Overwrite a header field, repairing the header CRC by default so the
    field's own validation is what fires rather than the checksum."""
    out = bytearray(log)
    out[offset + field : offset + field + len(packed)] = packed
    if fix_crc:
        out[offset : offset + REC] = rechecksum(bytes(out[offset : offset + REC]))
    return bytes(out)


def flip_byte(log, offset):
    out = bytearray(log)
    out[offset] ^= 0xFF
    return bytes(out)


PUT = ledger.OP_PUT
DELETE = ledger.OP_DELETE


class ReportAssertions(unittest.TestCase):
    def assert_report(
        self,
        report,
        *,
        state,
        reason,
        valid_records,
        last_seq,
        last_offset,
        end_offset,
        file_size,
        repair,
    ):
        """Assert the complete recovery state.  Every field is required."""
        self.assertEqual(report.tail_state, state, "tail_state")
        self.assertEqual(report.tail_reason, reason, "tail_reason")
        self.assertEqual(report.valid_records, valid_records, "valid_records")
        self.assertEqual(report.last_valid_seq, last_seq, "last_valid_seq")
        self.assertEqual(report.last_valid_offset, last_offset, "last_valid_offset")
        self.assertEqual(report.valid_end_offset, end_offset, "valid_end_offset")
        self.assertEqual(report.file_size, file_size, "file_size")
        self.assertEqual(report.repair_required, repair, "repair_required")
        self.assertEqual(
            report.discarded_bytes, file_size - end_offset, "discarded_bytes"
        )
        # The reported reason must agree with the classification, for every
        # fixture, so the two reason sets cannot drift from the reader.
        if state == ledger.TAIL_CLEAN:
            self.assertIsNone(report.tail_reason)
        elif state == ledger.TAIL_TORN:
            self.assertIn(report.tail_reason, ledger.TORN_REASONS)
        else:
            self.assertIn(report.tail_reason, ledger.CORRUPT_REASONS)


class TestFileHeaderHandling(ReportAssertions):
    def test_empty_log_is_clean(self):
        log = ledger.encode_file_header(0)
        self.assert_report(
            ledger.replay_log(log),
            state=ledger.TAIL_CLEAN,
            reason=None,
            valid_records=0,
            last_seq=0,
            last_offset=None,
            end_offset=HDR,
            file_size=HDR,
            repair=False,
        )

    def test_generation_is_reported(self):
        for generation in (0, 1, 42, 0xFFFFFFFF):
            log = build_log([(PUT, b"k", b"1")], generation=generation)
            self.assertEqual(ledger.replay_log(log).generation, generation)

    def test_missing_or_short_file_header_is_a_format_error(self):
        log = ledger.encode_file_header(0)
        for length in range(0, HDR):
            with self.subTest(length=length):
                with self.assertRaises(ledger.FormatError):
                    ledger.replay_log(log[:length])

    def test_corrupt_file_header_is_a_format_error_not_a_tail_state(self):
        # A header we cannot trust means there is no valid prefix at all, so
        # this raises rather than reporting a recoverable tail.
        log = build_log([(PUT, b"k", b"1")])
        for offset in range(HDR):
            with self.subTest(offset=offset):
                with self.assertRaises(ledger.FormatError):
                    ledger.replay_log(flip_byte(log, offset))


class TestCleanLogs(ReportAssertions):
    def test_single_record(self):
        records = [(PUT, b"user:42", b'{"name":"Venu"}')]
        log = build_log(records)
        offsets = record_offsets(records)
        self.assert_report(
            ledger.replay_log(log),
            state=ledger.TAIL_CLEAN,
            reason=None,
            valid_records=1,
            last_seq=1,
            last_offset=offsets[0],
            end_offset=offsets[-1],
            file_size=len(log),
            repair=False,
        )

    def test_multiple_records(self):
        records = [(PUT, b"a", b"1"), (PUT, b"b", b"2"), (PUT, b"c", b"3")]
        log = build_log(records)
        offsets = record_offsets(records)
        self.assert_report(
            ledger.replay_log(log),
            state=ledger.TAIL_CLEAN,
            reason=None,
            valid_records=3,
            last_seq=3,
            last_offset=offsets[2],
            end_offset=offsets[-1],
            file_size=len(log),
            repair=False,
        )

    def test_put_then_put_same_key(self):
        records = [(PUT, b"k", b"1"), (PUT, b"k", b"2")]
        log = build_log(records)
        seen = []
        report = ledger.replay_log(log, apply=lambda o, h, k, v: seen.append((k, v)))
        self.assertEqual(seen, [(b"k", b"1"), (b"k", b"2")])
        self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
        self.assertEqual(report.valid_records, 2)

    def test_put_then_delete(self):
        records = [(PUT, b"k", b"1"), (DELETE, b"k", b"")]
        log = build_log(records)
        offsets = record_offsets(records)
        seen = []
        report = ledger.replay_log(
            log, apply=lambda o, h, k, v: seen.append((h.op, k, v))
        )
        self.assertEqual(seen, [(PUT, b"k", b"1"), (DELETE, b"k", b"")])
        self.assert_report(
            report,
            state=ledger.TAIL_CLEAN,
            reason=None,
            valid_records=2,
            last_seq=2,
            last_offset=offsets[1],
            end_offset=offsets[-1],
            file_size=len(log),
            repair=False,
        )

    def test_long_clean_log(self):
        count = 1000
        records = [(PUT, b"key%04d" % i, b"%d" % i) for i in range(count)]
        log = build_log(records)
        offsets = record_offsets(records)
        self.assert_report(
            ledger.replay_log(log),
            state=ledger.TAIL_CLEAN,
            reason=None,
            valid_records=count,
            last_seq=count,
            last_offset=offsets[count - 1],
            end_offset=offsets[-1],
            file_size=len(log),
            repair=False,
        )

    def test_apply_receives_offsets_and_headers_in_order(self):
        records = [(PUT, b"a", b"1"), (DELETE, b"a", b""), (PUT, b"bb", b"22")]
        log = build_log(records)
        offsets = record_offsets(records)
        seen = []
        ledger.replay_log(log, apply=lambda o, h, k, v: seen.append((o, h.seq, k, v)))
        self.assertEqual(
            seen,
            [
                (offsets[0], 1, b"a", b"1"),
                (offsets[1], 2, b"a", b""),
                (offsets[2], 3, b"bb", b"22"),
            ],
        )

    def test_apply_is_optional(self):
        log = build_log([(PUT, b"k", b"1")])
        self.assertEqual(ledger.replay_log(log).valid_records, 1)

    def test_value_containing_record_magic_does_not_confuse_framing(self):
        value = b'"' + ledger.RECORD_MAGIC + b'"'
        records = [(PUT, b"k", value), (PUT, b"k2", b"1")]
        log = build_log(records)
        report = ledger.replay_log(log)
        self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
        self.assertEqual(report.valid_records, 2)


class TestSequenceBoundaries(ReportAssertions):
    """A clean log ending at MAX_SEQ is unconstructible - it would need 2**64
    records - so these pin the arithmetic at the boundary instead."""

    def test_first_record_at_max_seq_is_a_gap_not_an_overflow(self):
        log = ledger.encode_file_header(0) + ledger.encode_record(
            PUT, ledger.MAX_SEQ, b"k", b"1"
        )
        self.assert_report(
            ledger.replay_log(log),
            state=ledger.TAIL_CORRUPT,
            reason=ledger.REASON_SEQ_GAP,
            valid_records=0,
            last_seq=0,
            last_offset=None,
            end_offset=HDR,
            file_size=len(log),
            repair=True,
        )

    def test_large_sequence_numbers_round_trip_through_the_reader(self):
        # Patch a single-record log up to a large seq and back, confirming
        # the comparison is on integers and not a truncated field.
        log = build_log([(PUT, b"k", b"1")])
        for seq in (1, 2**31, 2**32, 2**63, ledger.MAX_SEQ):
            with self.subTest(seq=seq):
                patched = patch_header(log, HDR, OFF_SEQ, struct.pack("<Q", seq))
                report = ledger.replay_log(patched)
                if seq == ledger.FIRST_SEQ:
                    self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
                else:
                    self.assertEqual(report.tail_state, ledger.TAIL_CORRUPT)
                    self.assertEqual(report.tail_reason, ledger.REASON_SEQ_GAP)


class TestTornTails(ReportAssertions):
    RECORDS = [(PUT, b"alpha", b"111"), (DELETE, b"beta", b""), (PUT, b"c", b"3")]

    def test_zero_bytes_of_next_header_is_clean_not_torn(self):
        # EOF exactly at a record boundary is CLEAN by definition: there is
        # no incomplete record present. Nothing is torn about it.
        log = build_log(self.RECORDS)
        offsets = record_offsets(self.RECORDS)
        self.assert_report(
            ledger.replay_log(log),
            state=ledger.TAIL_CLEAN,
            reason=None,
            valid_records=3,
            last_seq=3,
            last_offset=offsets[2],
            end_offset=offsets[3],
            file_size=len(log),
            repair=False,
        )

    def test_one_byte_of_header(self):
        log = build_log(self.RECORDS)
        offsets = record_offsets(self.RECORDS)
        truncated = log[: offsets[2] + 1]
        self.assert_report(
            ledger.replay_log(truncated),
            state=ledger.TAIL_TORN,
            reason=ledger.REASON_SHORT_HEADER,
            valid_records=2,
            last_seq=2,
            last_offset=offsets[1],
            end_offset=offsets[2],
            file_size=len(truncated),
            repair=True,
        )

    def test_every_header_truncation_point(self):
        log = build_log(self.RECORDS)
        offsets = record_offsets(self.RECORDS)
        start = offsets[2]
        for extra in range(1, REC):
            with self.subTest(header_bytes=extra):
                truncated = log[: start + extra]
                self.assert_report(
                    ledger.replay_log(truncated),
                    state=ledger.TAIL_TORN,
                    reason=ledger.REASON_SHORT_HEADER,
                    valid_records=2,
                    last_seq=2,
                    last_offset=offsets[1],
                    end_offset=start,
                    file_size=len(truncated),
                    repair=True,
                )

    def test_every_payload_truncation_point(self):
        log = build_log(self.RECORDS)
        offsets = record_offsets(self.RECORDS)
        start = offsets[2]
        payload_len = offsets[3] - start - REC
        for extra in range(0, payload_len):
            with self.subTest(payload_bytes=extra):
                truncated = log[: start + REC + extra]
                self.assert_report(
                    ledger.replay_log(truncated),
                    state=ledger.TAIL_TORN,
                    reason=ledger.REASON_SHORT_PAYLOAD,
                    valid_records=2,
                    last_seq=2,
                    last_offset=offsets[1],
                    end_offset=start,
                    file_size=len(truncated),
                    repair=True,
                )

    def test_exhaustive_truncation_matrix(self):
        """Truncate at every possible length and assert the reader always
        recovers exactly the largest whole prefix of records that fits."""
        log = build_log(self.RECORDS)
        offsets = record_offsets(self.RECORDS)
        for length in range(HDR, len(log) + 1):
            with self.subTest(length=length):
                truncated = log[:length]
                complete = sum(1 for o in offsets[1:] if o <= length)
                boundary = offsets[complete]
                remainder = length - boundary
                if remainder == 0:
                    state, reason, repair = ledger.TAIL_CLEAN, None, False
                elif remainder < REC:
                    state = ledger.TAIL_TORN
                    reason = ledger.REASON_SHORT_HEADER
                    repair = True
                else:
                    state = ledger.TAIL_TORN
                    reason = ledger.REASON_SHORT_PAYLOAD
                    repair = True
                report = ledger.replay_log(truncated)
                self.assert_report(
                    report,
                    state=state,
                    reason=reason,
                    valid_records=complete,
                    last_seq=complete,
                    last_offset=offsets[complete - 1] if complete else None,
                    end_offset=boundary,
                    file_size=length,
                    repair=repair,
                )

    def test_repairing_then_replaying_is_clean_and_idempotent(self):
        """Truncating to valid_end_offset must yield a clean log with the
        same contents - recovery twice equals recovery once."""
        log = build_log(self.RECORDS)
        for length in range(HDR, len(log) + 1):
            with self.subTest(length=length):
                first = ledger.replay_log(log[:length])
                repaired = log[: first.valid_end_offset]
                second = ledger.replay_log(repaired)
                self.assertEqual(second.tail_state, ledger.TAIL_CLEAN)
                self.assertIsNone(second.tail_reason)
                self.assertFalse(second.repair_required)
                self.assertEqual(second.valid_records, first.valid_records)
                self.assertEqual(second.last_valid_seq, first.last_valid_seq)
                self.assertEqual(second.valid_end_offset, first.valid_end_offset)
                self.assertEqual(second.discarded_bytes, 0)
                third = ledger.replay_log(repaired[: second.valid_end_offset])
                self.assertEqual(third, second)

    def test_torn_record_is_never_delivered_to_apply(self):
        log = build_log(self.RECORDS)
        offsets = record_offsets(self.RECORDS)
        truncated = log[: offsets[2] + REC + 1]
        seen = []
        ledger.replay_log(truncated, apply=lambda o, h, k, v: seen.append(k))
        self.assertEqual(seen, [b"alpha", b"beta"])


class TestCorruptTails(ReportAssertions):
    RECORDS = [(PUT, b"alpha", b"111"), (PUT, b"beta", b"222"), (PUT, b"c", b"3")]

    def setUp(self):
        self.log = build_log(self.RECORDS)
        self.offsets = record_offsets(self.RECORDS)
        # Every corruption below is applied to the second record, so the
        # expected recovery state is always the same.
        self.target = self.offsets[1]

    def assert_stops_at_second_record(self, log, reason):
        self.assert_report(
            ledger.replay_log(log),
            state=ledger.TAIL_CORRUPT,
            reason=reason,
            valid_records=1,
            last_seq=1,
            last_offset=self.offsets[0],
            end_offset=self.target,
            file_size=len(log),
            repair=True,
        )

    def test_header_checksum_corruption(self):
        for field in range(REC):
            with self.subTest(byte=field):
                bad = flip_byte(self.log, self.target + field)
                self.assert_stops_at_second_record(bad, ledger.REASON_HEADER_CRC)

    def test_payload_checksum_corruption(self):
        payload_len = self.offsets[2] - self.target - REC
        for index in range(payload_len):
            with self.subTest(payload_byte=index):
                bad = flip_byte(self.log, self.target + REC + index)
                self.assert_stops_at_second_record(bad, ledger.REASON_PAYLOAD_CRC)

    def test_invalid_magic(self):
        bad = patch_header(self.log, self.target, OFF_MAGIC, b"XXXX")
        self.assert_stops_at_second_record(bad, ledger.REASON_BAD_MAGIC)

    def test_invalid_version(self):
        bad = patch_header(self.log, self.target, OFF_VERSION, bytes([2]))
        self.assert_stops_at_second_record(bad, ledger.REASON_BAD_VERSION)

    def test_invalid_flags(self):
        bad = patch_header(self.log, self.target, OFF_FLAGS, struct.pack("<H", 1))
        self.assert_stops_at_second_record(bad, ledger.REASON_INVALID_FLAGS)

    def test_invalid_operation(self):
        for op in (0, 3, 99, 255):
            with self.subTest(op=op):
                bad = patch_header(self.log, self.target, OFF_OP, bytes([op]))
                self.assert_stops_at_second_record(bad, ledger.REASON_INVALID_OP)

    def test_invalid_key_length(self):
        for key_len in (0, ledger.MAX_KEY_BYTES + 1, 0xFFFFFFFF):
            with self.subTest(key_len=key_len):
                bad = patch_header(
                    self.log, self.target, OFF_KEY_LEN, struct.pack("<I", key_len)
                )
                self.assert_stops_at_second_record(
                    bad, ledger.REASON_INVALID_KEY_LEN
                )

    def test_invalid_value_length(self):
        for val_len in (0, ledger.MAX_VALUE_BYTES + 1, 0xFFFFFFFF):
            with self.subTest(val_len=val_len):
                bad = patch_header(
                    self.log, self.target, OFF_VAL_LEN, struct.pack("<I", val_len)
                )
                self.assert_stops_at_second_record(
                    bad, ledger.REASON_INVALID_VAL_LEN
                )

    def test_absurd_length_does_not_read_past_the_file(self):
        # 0xFFFFFFFF is rejected by the header validation, so it never
        # reaches a slice; the reader stops without touching the payload.
        bad = patch_header(
            self.log, self.target, OFF_VAL_LEN, struct.pack("<I", 0xFFFFFFFF)
        )
        report = ledger.replay_log(bad)
        self.assertEqual(report.tail_reason, ledger.REASON_INVALID_VAL_LEN)
        self.assertEqual(report.valid_end_offset, self.target)

    def test_invalid_delete_shape(self):
        records = [(PUT, b"alpha", b"111"), (DELETE, b"beta", b"")]
        log = build_log(records)
        offsets = record_offsets(records)
        bad = patch_header(log, offsets[1], OFF_VAL_LEN, struct.pack("<I", 3))
        self.assert_report(
            ledger.replay_log(bad),
            state=ledger.TAIL_CORRUPT,
            reason=ledger.REASON_INVALID_VAL_LEN,
            valid_records=1,
            last_seq=1,
            last_offset=offsets[0],
            end_offset=offsets[1],
            file_size=len(bad),
            repair=True,
        )

    def test_sequence_gap(self):
        bad = patch_header(self.log, self.target, OFF_SEQ, struct.pack("<Q", 3))
        self.assert_stops_at_second_record(bad, ledger.REASON_SEQ_GAP)

    def test_sequence_gap_large_jump(self):
        bad = patch_header(
            self.log, self.target, OFF_SEQ, struct.pack("<Q", ledger.MAX_SEQ)
        )
        self.assert_stops_at_second_record(bad, ledger.REASON_SEQ_GAP)

    def test_duplicate_sequence(self):
        bad = patch_header(self.log, self.target, OFF_SEQ, struct.pack("<Q", 1))
        self.assert_stops_at_second_record(bad, ledger.REASON_SEQ_DUPLICATE)

    def test_sequence_regression(self):
        # Third record claims seq 1: expected 3, previous 2, so it is neither
        # the next nor a duplicate of its predecessor.
        bad = patch_header(self.log, self.offsets[2], OFF_SEQ, struct.pack("<Q", 1))
        self.assert_report(
            ledger.replay_log(bad),
            state=ledger.TAIL_CORRUPT,
            reason=ledger.REASON_SEQ_REGRESSION,
            valid_records=2,
            last_seq=2,
            last_offset=self.offsets[1],
            end_offset=self.offsets[2],
            file_size=len(bad),
            repair=True,
        )

    def test_first_record_sequence_must_be_one(self):
        log = build_log([(PUT, b"k", b"1")])
        cases = [
            (0, ledger.REASON_SEQ_REGRESSION),
            (2, ledger.REASON_SEQ_GAP),
            (99, ledger.REASON_SEQ_GAP),
        ]
        for seq, reason in cases:
            with self.subTest(seq=seq):
                bad = patch_header(log, HDR, OFF_SEQ, struct.pack("<Q", seq))
                self.assert_report(
                    ledger.replay_log(bad),
                    state=ledger.TAIL_CORRUPT,
                    reason=reason,
                    valid_records=0,
                    last_seq=0,
                    last_offset=None,
                    end_offset=HDR,
                    file_size=len(bad),
                    repair=True,
                )

    def test_malformed_framing_garbage_appended(self):
        for garbage in (b"\x00" * 64, b"\xff" * 64, b"garbage" * 16):
            with self.subTest(garbage=garbage[:4]):
                bad = self.log + garbage
                report = ledger.replay_log(bad)
                self.assertEqual(report.tail_state, ledger.TAIL_CORRUPT)
                self.assertEqual(report.valid_records, 3)
                self.assertEqual(report.valid_end_offset, self.offsets[3])
                self.assertIn(report.tail_reason, ledger.CORRUPT_REASONS)

    def test_record_shifted_by_one_byte(self):
        # A single inserted byte desynchronises framing for everything after.
        bad = self.log[: self.target] + b"\x00" + self.log[self.target :]
        report = ledger.replay_log(bad)
        self.assertEqual(report.tail_state, ledger.TAIL_CORRUPT)
        self.assertEqual(report.valid_records, 1)
        self.assertEqual(report.valid_end_offset, self.target)


class TestConservativeRecovery(ReportAssertions):
    """The reader must never scan forward past damage, even when perfectly
    valid records demonstrably follow it."""

    RECORDS = [(PUT, b"a", b"1"), (PUT, b"b", b"2"), (PUT, b"c", b"3")]

    def test_valid_records_after_corruption_are_not_resurrected(self):
        log = build_log(self.RECORDS)
        offsets = record_offsets(self.RECORDS)
        bad = flip_byte(log, offsets[1] + REC)  # corrupt record 2's payload
        seen = []
        report = ledger.replay_log(bad, apply=lambda o, h, k, v: seen.append(k))
        self.assert_report(
            report,
            state=ledger.TAIL_CORRUPT,
            reason=ledger.REASON_PAYLOAD_CRC,
            valid_records=1,
            last_seq=1,
            last_offset=offsets[0],
            end_offset=offsets[1],
            file_size=len(bad),
            repair=True,
        )
        # Record 3 is intact on disk and is still discarded.
        self.assertEqual(seen, [b"a"])
        self.assertGreater(report.discarded_bytes, 0)

    def test_intact_trailing_record_is_provably_present(self):
        # Guards the test above: prove record 3 really would have parsed.
        log = build_log(self.RECORDS)
        offsets = record_offsets(self.RECORDS)
        header = ledger.decode_record_header(log[offsets[2] : offsets[2] + REC])
        self.assertEqual(header.seq, 3)
        ledger.verify_payload(header, log[offsets[2] + REC : offsets[3]])


class TestReportModel(unittest.TestCase):
    def test_report_is_immutable(self):
        report = ledger.replay_log(build_log([(PUT, b"k", b"1")]))
        with self.assertRaises(Exception):
            report.valid_records = 99

    def test_states_are_distinct_constants(self):
        states = {ledger.TAIL_CLEAN, ledger.TAIL_TORN, ledger.TAIL_CORRUPT}
        self.assertEqual(len(states), 3)

    def test_reason_sets_are_disjoint_and_complete(self):
        self.assertFalse(ledger.TORN_REASONS & ledger.CORRUPT_REASONS)

    def test_every_reason_a_scan_can_report_is_classified(self):
        """No reason may escape the two sets; a demo or CLI switching on
        them must not meet an unclassified value."""
        known = ledger.TORN_REASONS | ledger.CORRUPT_REASONS
        reasons = {
            value
            for name, value in vars(ledger).items()
            if name.startswith("REASON_")
        }
        self.assertEqual(reasons, known)

    def test_clean_report_has_no_reason(self):
        report = ledger.replay_log(ledger.encode_file_header(0))
        self.assertIsNone(report.tail_reason)
        self.assertFalse(report.repair_required)
        self.assertEqual(report.discarded_bytes, 0)


if __name__ == "__main__":
    unittest.main()
