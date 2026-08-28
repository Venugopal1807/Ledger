"""Test-only fixture builders, byte surgery, and an independent oracle.

Nothing here is imported by ledger.py; this module exists purely so the
test suite can construct logs, damage them precisely, and predict what
recovery should do.

The oracle is the important part.  Expected recovery results are derived
from the *format specification* by arithmetic over a known history - never
by calling ``ledger.replay_log``, which is the code under test.  If the
reader and the oracle ever disagree, one of them is wrong, and the test
says so instead of agreeing with itself.

To keep that independence real, the sizes below are written out rather than
imported from ledger; ``test_matrices`` asserts they still match.
"""

import struct

import ledger

# Format constants restated from DESIGN.md sections 4-5, deliberately not
# imported, so a change to ledger's constants fails a test instead of
# silently moving the oracle with it.
SPEC_FILE_HEADER_SIZE = 32
SPEC_RECORD_HEADER_SIZE = 32
SPEC_MIN_RECORD_SIZE = 33  # header + at least one key byte

# Record header field offsets, for corrupting exactly one field.
OFF_MAGIC = 0
OFF_VERSION = 4
OFF_OP = 5
OFF_FLAGS = 6
OFF_SEQ = 8
OFF_KEY_LEN = 16
OFF_VAL_LEN = 20
OFF_PAYLOAD_CRC = 24
OFF_HEADER_CRC = 28

PUT = ledger.OP_PUT
DELETE = ledger.OP_DELETE


# --------------------------------------------------------------------------
# Building logs
# --------------------------------------------------------------------------


def build_log(history, generation=0):
    """Serialize a history of ``(op, key, value)`` triples with sequential
    sequence numbers starting at 1."""
    parts = [ledger.encode_file_header(generation)]
    for index, (op, key, value) in enumerate(history, start=ledger.FIRST_SEQ):
        parts.append(ledger.encode_record(op, index, key, value))
    return b"".join(parts)


def boundaries(history):
    """Record start offsets, plus the offset just past the last record.

    Computed from the history by arithmetic on the specified sizes.  This
    never parses the log, so it is a genuine independent prediction of where
    every record boundary lies.
    """
    offsets = [SPEC_FILE_HEADER_SIZE]
    for _op, key, value in history:
        offsets.append(
            offsets[-1] + SPEC_RECORD_HEADER_SIZE + len(key) + len(value)
        )
    return offsets


def record_index_at(history, offset):
    """Index of the record containing ``offset``, or None for the header."""
    if offset < SPEC_FILE_HEADER_SIZE:
        return None
    marks = boundaries(history)
    for index in range(len(history)):
        if marks[index] <= offset < marks[index + 1]:
            return index
    raise AssertionError(f"offset {offset} is past the end of the log")


# --------------------------------------------------------------------------
# Byte surgery
# --------------------------------------------------------------------------


def rechecksum(header):
    prefix = header[:OFF_HEADER_CRC]
    return prefix + struct.pack("<I", ledger.crc32(prefix))


def patch_header(log, offset, field, packed, fix_crc=True):
    """Overwrite a header field, repairing the header CRC by default so the
    field's own validation fires rather than the checksum."""
    out = bytearray(log)
    out[offset + field : offset + field + len(packed)] = packed
    if fix_crc:
        end = offset + SPEC_RECORD_HEADER_SIZE
        out[offset:end] = rechecksum(bytes(out[offset:end]))
    return bytes(out)


def flip_bit(data, index, bit):
    out = bytearray(data)
    out[index] ^= 1 << bit
    return bytes(out)


def flip_byte(data, index):
    out = bytearray(data)
    out[index] ^= 0xFF
    return bytes(out)


def substitute(data, index, value):
    out = bytearray(data)
    out[index] = value
    return bytes(out)


def damage_range(data, start, length, filler=b"\xa5"):
    out = bytearray(data)
    out[start : start + length] = (filler * length)[:length]
    return bytes(out)


# --------------------------------------------------------------------------
# Running the reader
# --------------------------------------------------------------------------


def replay_collect(data):
    """Replay a log, returning ``(report, delivered)`` where delivered is the
    list of ``(op, key, value)`` triples handed to the apply callback."""
    delivered = []
    report = ledger.replay_log(
        data, apply=lambda offset, header, key, value: delivered.append(
            (header.op, key, value)
        )
    )
    return report, delivered


# --------------------------------------------------------------------------
# The oracle: expected results derived from the spec, not from the reader
# --------------------------------------------------------------------------


def expected_truncation(history, length):
    """Predict the recovery outcome for ``build_log(history)[:length]``.

    Pure arithmetic over the record sizes implied by the history.  Returns
    ``(state, reason, valid_records, valid_end_offset)``.
    """
    if length < SPEC_FILE_HEADER_SIZE:
        raise ValueError("a log shorter than the file header cannot be replayed")

    marks = boundaries(history)
    complete = sum(1 for mark in marks[1:] if mark <= length)
    boundary = marks[complete]
    remainder = length - boundary

    if remainder == 0:
        return ledger.TAIL_CLEAN, None, complete, boundary
    if remainder < SPEC_RECORD_HEADER_SIZE:
        return ledger.TAIL_TORN, ledger.REASON_SHORT_HEADER, complete, boundary
    return ledger.TAIL_TORN, ledger.REASON_SHORT_PAYLOAD, complete, boundary


def expected_single_bit_flip(history, offset):
    """Predict the recovery outcome when one bit at ``offset`` is flipped.

    A single-bit error is always caught by CRC-32, so the outcome is exactly
    determined: records before the damaged one are untouched and all
    validate, the damaged record fails, and the scan stops there.  Which of
    the two checksums fires depends only on whether the byte lies in the
    record's header or its payload.

    Returns ``(state, reason, valid_records, valid_end_offset)``, or None if
    the byte is in the file header, where recovery raises instead.
    """
    index = record_index_at(history, offset)
    if index is None:
        return None

    marks = boundaries(history)
    start = marks[index]
    in_header = offset - start < SPEC_RECORD_HEADER_SIZE
    reason = ledger.REASON_HEADER_CRC if in_header else ledger.REASON_PAYLOAD_CRC
    return ledger.TAIL_CORRUPT, reason, index, start
