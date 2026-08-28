"""Ledger - a crash-safe embedded state store.

Local application state that survives crashes, with zero runtime
dependencies.  See DESIGN.md for the full engineering design.

This module currently implements Step 1 of the design: the on-disk format
primitives.  The storage engine, recovery reader, compaction and CLI are
not implemented yet.

Layout of a store file (DESIGN.md sections 4-7):

    +----------------------------+  offset 0
    | file header (32 bytes)     |
    +----------------------------+  offset 32
    | record 0                   |
    | record 1                   |
    | ...                        |
    +----------------------------+  EOF

and of a single record:

    +--------------------+---------------+-------------------+
    | header (32 bytes)  | key (key_len) | value (val_len)   |
    +--------------------+---------------+-------------------+
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

__all__ = [
    "LedgerError",
    "FormatError",
    "CorruptRecordError",
    "FileHeader",
    "RecordHeader",
    "crc32",
    "encode_file_header",
    "decode_file_header",
    "encode_record",
    "decode_record_header",
    "verify_payload",
    "split_payload",
]

# --------------------------------------------------------------------------
# Format constants
# --------------------------------------------------------------------------

FILE_MAGIC = b"LEDGERv1"
FORMAT_VERSION = 1
FILE_HEADER_SIZE = 32

RECORD_MAGIC = b"LGR\x1e"
RECORD_VERSION = 1
RECORD_HEADER_SIZE = 32

OP_PUT = 1
OP_DELETE = 2

# Sequence numbers start at 1 and increase by exactly 1 per record within a
# single file generation.  See DESIGN.md section 14 for why they restart
# after compaction.
FIRST_SEQ = 1
MAX_SEQ = 2**64 - 1

MAX_KEY_BYTES = 4096
MAX_VALUE_BYTES = 8 * 1024 * 1024

# Reasons attached to CorruptRecordError.  These are the corruption classes
# detectable from a complete set of bytes; short reads (torn tails) and
# sequence discontinuity are the recovery reader's responsibility, not this
# layer's.
REASON_BAD_MAGIC = "bad_magic"
REASON_BAD_VERSION = "bad_version"
REASON_HEADER_CRC = "header_crc"
REASON_INVALID_OP = "invalid_op"
REASON_INVALID_FLAGS = "invalid_flags"
REASON_INVALID_KEY_LEN = "invalid_key_len"
REASON_INVALID_VAL_LEN = "invalid_val_len"
REASON_PAYLOAD_CRC = "payload_crc"

# The checksum covers every header field except itself, so both headers are
# packed as a 28-byte prefix followed by a bare u32 checksum.  Packing the
# prefix once and checksumming those exact bytes means the value written can
# never be computed from a different copy of the data.
_FILE_PREFIX = struct.Struct("<8sHHI12s")
_RECORD_PREFIX = struct.Struct("<4sBBHQIII")
_U32 = struct.Struct("<I")

_FILE_RESERVED = b"\x00" * 12


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class LedgerError(Exception):
    """Base class for every error raised by Ledger."""


class FormatError(LedgerError):
    """The file is not a Ledger store, or is a version we cannot read."""


class CorruptRecordError(LedgerError):
    """A record's bytes are present but do not validate.

    ``reason`` is one of the ``REASON_*`` constants.  The recovery reader
    turns this into a tail classification; callers of the public API see
    ``CorruptLogError`` instead.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# --------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------


def crc32(data: bytes) -> int:
    """Return the CRC-32 of ``data`` as an unsigned 32-bit int.

    CRC-32 is error detection, not tamper detection: anyone who can edit the
    file can recompute it.  See DESIGN.md section 23.
    """
    # zlib.crc32 is documented to always return an unsigned value in Python 3,
    # so no masking is needed to keep it in u32 range.
    return zlib.crc32(data)


# --------------------------------------------------------------------------
# File header
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileHeader:
    """A decoded file header.

    ``version`` and the reserved bytes are validated during decoding, so a
    ``FileHeader`` that exists is always the current format version.
    """

    version: int
    generation: int


def encode_file_header(generation: int = 0) -> bytes:
    """Encode the 32-byte file header written at offset 0."""
    if not 0 <= generation <= 0xFFFFFFFF:
        raise ValueError(f"generation out of range: {generation}")
    prefix = _FILE_PREFIX.pack(
        FILE_MAGIC, FORMAT_VERSION, 0, generation, _FILE_RESERVED
    )
    return prefix + _U32.pack(crc32(prefix))


def decode_file_header(buf: bytes) -> FileHeader:
    """Decode and validate the file header.

    Raises ``FormatError`` if the bytes are not a readable Ledger header.
    A file header is either entirely present and valid or the store is
    unusable, so there is no torn-header case to distinguish here.
    """
    if len(buf) != FILE_HEADER_SIZE:
        raise FormatError(
            f"file header must be {FILE_HEADER_SIZE} bytes, got {len(buf)}"
        )

    prefix = buf[: _FILE_PREFIX.size]
    (stored_crc,) = _U32.unpack_from(buf, _FILE_PREFIX.size)
    if stored_crc != crc32(prefix):
        raise FormatError("file header checksum mismatch")

    magic, version, flags, generation, reserved = _FILE_PREFIX.unpack(prefix)
    if magic != FILE_MAGIC:
        raise FormatError(f"not a Ledger file: bad magic {magic!r}")
    if version != FORMAT_VERSION:
        raise FormatError(
            f"unsupported format version {version}, expected {FORMAT_VERSION}"
        )
    if flags != 0:
        raise FormatError(f"unsupported file header flags: {flags:#06x}")
    if reserved != _FILE_RESERVED:
        raise FormatError("file header reserved bytes are not zero")

    return FileHeader(version=version, generation=generation)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordHeader:
    """A decoded, fully validated record header.

    The version and flags fields are checked during decoding and are not
    carried here: they have exactly one legal value in format version 1.
    """

    op: int
    seq: int
    key_len: int
    val_len: int
    payload_crc: int

    @property
    def payload_len(self) -> int:
        return self.key_len + self.val_len

    @property
    def total_size(self) -> int:
        """Bytes this record occupies, header included."""
        return RECORD_HEADER_SIZE + self.payload_len


def _check_lengths(op: int, key_len: int, val_len: int):
    """Validate the operation and the two length fields together.

    Returns ``(reason, message)`` describing the first problem found, or
    ``None`` if the shape is valid.  Encode and decode share this so a
    record can never be written in a shape the reader would reject, but
    they raise different exceptions: bad arguments to ``encode_record`` are
    a caller bug, whereas the same bytes read back off disk are corruption.
    """
    if op not in (OP_PUT, OP_DELETE):
        return REASON_INVALID_OP, f"unknown operation {op}"
    if not 1 <= key_len <= MAX_KEY_BYTES:
        return (
            REASON_INVALID_KEY_LEN,
            f"key length {key_len} outside 1..{MAX_KEY_BYTES}",
        )
    if op == OP_DELETE:
        if val_len != 0:
            return (
                REASON_INVALID_VAL_LEN,
                f"delete record carries a {val_len}-byte value",
            )
    # An encoded JSON value is never empty: the shortest is a single
    # character such as ``0``.  A zero-length PUT value is therefore always
    # damage, and rejecting it costs nothing.
    elif not 1 <= val_len <= MAX_VALUE_BYTES:
        return (
            REASON_INVALID_VAL_LEN,
            f"value length {val_len} outside 1..{MAX_VALUE_BYTES}",
        )
    return None


def encode_record(op: int, seq: int, key: bytes, value: bytes = b"") -> bytes:
    """Encode one complete record: header, then key, then value.

    The whole record is returned as a single ``bytes`` object so the engine
    can hand it to one ``os.write`` call.
    """
    if not isinstance(key, (bytes, bytearray)):
        raise TypeError(f"key must be bytes, got {type(key).__name__}")
    if not isinstance(value, (bytes, bytearray)):
        raise TypeError(f"value must be bytes, got {type(value).__name__}")
    if not FIRST_SEQ <= seq <= MAX_SEQ:
        raise ValueError(f"sequence number out of range: {seq}")

    key = bytes(key)
    value = bytes(value)
    problem = _check_lengths(op, len(key), len(value))
    if problem is not None:
        raise ValueError(problem[1])

    payload = key + value
    prefix = _RECORD_PREFIX.pack(
        RECORD_MAGIC,
        RECORD_VERSION,
        op,
        0,
        seq,
        len(key),
        len(value),
        crc32(payload),
    )
    return prefix + _U32.pack(crc32(prefix)) + payload


def decode_record_header(buf: bytes) -> RecordHeader:
    """Decode and fully validate a record header.

    ``buf`` must be exactly ``RECORD_HEADER_SIZE`` bytes; a short read is a
    torn tail, which only the recovery reader can classify, so being handed
    the wrong number of bytes here is a programming error and raises
    ``ValueError`` rather than a corruption signal.

    If this returns, every field is trustworthy - in particular the length
    fields, which are checked only after the header checksum verifies, so a
    corrupt length can never size an allocation.
    """
    if len(buf) != RECORD_HEADER_SIZE:
        raise ValueError(
            f"record header must be {RECORD_HEADER_SIZE} bytes, got {len(buf)}"
        )

    prefix = buf[: _RECORD_PREFIX.size]
    (stored_crc,) = _U32.unpack_from(buf, _RECORD_PREFIX.size)
    if stored_crc != crc32(prefix):
        raise CorruptRecordError(REASON_HEADER_CRC, "record header checksum mismatch")

    magic, version, op, flags, seq, key_len, val_len, payload_crc = (
        _RECORD_PREFIX.unpack(prefix)
    )
    if magic != RECORD_MAGIC:
        raise CorruptRecordError(REASON_BAD_MAGIC, f"bad record magic {magic!r}")
    if version != RECORD_VERSION:
        raise CorruptRecordError(
            REASON_BAD_VERSION, f"unsupported record version {version}"
        )
    if flags != 0:
        raise CorruptRecordError(
            REASON_INVALID_FLAGS, f"unsupported record flags: {flags:#06x}"
        )
    problem = _check_lengths(op, key_len, val_len)
    if problem is not None:
        raise CorruptRecordError(*problem)

    return RecordHeader(
        op=op,
        seq=seq,
        key_len=key_len,
        val_len=val_len,
        payload_crc=payload_crc,
    )


def verify_payload(header: RecordHeader, payload: bytes) -> None:
    """Check a record's payload against the checksum in its header.

    ``payload`` must be exactly ``header.payload_len`` bytes; as with
    ``decode_record_header``, a short payload is a torn tail and belongs to
    the recovery reader.
    """
    if len(payload) != header.payload_len:
        raise ValueError(
            f"payload must be {header.payload_len} bytes, got {len(payload)}"
        )
    if crc32(payload) != header.payload_crc:
        raise CorruptRecordError(REASON_PAYLOAD_CRC, "record payload checksum mismatch")


def split_payload(header: RecordHeader, payload: bytes) -> tuple[bytes, bytes]:
    """Split a verified payload into its key and value halves."""
    if len(payload) != header.payload_len:
        raise ValueError(
            f"payload must be {header.payload_len} bytes, got {len(payload)}"
        )
    return payload[: header.key_len], payload[header.key_len :]
