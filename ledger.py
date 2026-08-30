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

import argparse
import fcntl
import json
import os
import pathlib
import struct
import sys
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
    "LogReport",
    "replay_log",
    "Ledger",
    "CorruptLogError",
    "LockedError",
    "ReadOnlyError",
    "ClosedError",
    "WriteError",
    "CompactionError",
    "CompactionResult",
    "forensic_scan",
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


# --------------------------------------------------------------------------
# Recovery reader
# --------------------------------------------------------------------------

# Terminal state of a log scan.  These classify how the file ended, not
# whether the data in it is useful.
TAIL_CLEAN = "clean"
TAIL_TORN = "torn"
TAIL_CORRUPT = "corrupt"

# Reasons the format layer cannot produce, because they are visible only
# while scanning a sequence of records rather than one set of bytes.
REASON_SHORT_HEADER = "short_header"
REASON_SHORT_PAYLOAD = "short_payload"
REASON_SEQ_GAP = "seq_gap"
REASON_SEQ_REGRESSION = "seq_regression"
REASON_SEQ_DUPLICATE = "seq_duplicate"

# Every reason a scan can stop on, grouped by the classification it implies.
TORN_REASONS = frozenset({REASON_SHORT_HEADER, REASON_SHORT_PAYLOAD})
CORRUPT_REASONS = frozenset(
    {
        REASON_BAD_MAGIC,
        REASON_BAD_VERSION,
        REASON_HEADER_CRC,
        REASON_INVALID_OP,
        REASON_INVALID_FLAGS,
        REASON_INVALID_KEY_LEN,
        REASON_INVALID_VAL_LEN,
        REASON_PAYLOAD_CRC,
        REASON_SEQ_GAP,
        REASON_SEQ_REGRESSION,
        REASON_SEQ_DUPLICATE,
    }
)


@dataclass(frozen=True)
class LogReport:
    """The result of replaying a log: what was valid, and how it ended.

    This is the structured answer the engine, the future inspect command,
    the tests and the demo all read.  Classification is carried in
    ``tail_state`` and ``tail_reason`` as explicit constants; exception
    messages are never the API for it.
    """

    generation: int
    file_size: int
    valid_records: int
    last_valid_seq: int
    last_valid_offset: "int | None"
    valid_end_offset: int
    tail_state: str
    tail_reason: "str | None"

    @property
    def repair_required(self) -> bool:
        """True if the file ends in anything other than a record boundary.

        When this is True, ``valid_end_offset`` is the offset to truncate
        to.  Whether the truncation actually happens is the engine's policy
        (write mode, and the ``repair`` flag), not a property of the log.
        """
        return self.tail_state != TAIL_CLEAN

    @property
    def discarded_bytes(self) -> int:
        """Bytes after the last valid record; zero for a clean log."""
        return self.file_size - self.valid_end_offset


def replay_log(data: bytes, apply=None) -> LogReport:
    """Replay a log from its file header forward, stopping at the first
    record that is incomplete or fails validation.

    ``apply`` is called as ``apply(offset, header, key, value)`` for each
    valid record, in sequence order.  Passing ``None`` validates without
    materialising anything, which is what a read-only diagnosis wants.

    Recovery is deliberately conservative: once a record fails, the scan
    stops.  It does not search forward for the next plausible record.  After
    a damaged region we cannot distinguish a genuine later record from stale
    bytes left in a reused block, and the sequence continuity that would
    otherwise tell us apart is exactly what has been broken.  Resurrecting
    data we cannot place in history is how a store silently returns a value
    that was overwritten long ago.  Forensic scanning past the damage is a
    job for inspect, where a human makes the call.

    Raises ``FormatError`` if the file header is missing or unreadable; a
    store whose header we cannot trust has no valid prefix to recover.
    """
    file_header = decode_file_header(data[:FILE_HEADER_SIZE])

    file_size = len(data)
    offset = FILE_HEADER_SIZE
    expected_seq = FIRST_SEQ
    previous_seq = 0
    valid_records = 0
    last_valid_offset = None
    last_valid_seq = 0
    tail_state = TAIL_CLEAN
    tail_reason = None

    while True:
        if offset == file_size:
            break

        header_bytes = data[offset : offset + RECORD_HEADER_SIZE]
        if len(header_bytes) < RECORD_HEADER_SIZE:
            tail_state, tail_reason = TAIL_TORN, REASON_SHORT_HEADER
            break

        try:
            # This validates the checksum before any other field, so the
            # length fields below are already known to be in range.
            header = decode_record_header(header_bytes)
        except CorruptRecordError as error:
            tail_state, tail_reason = TAIL_CORRUPT, error.reason
            break

        if header.seq != expected_seq:
            if valid_records and header.seq == previous_seq:
                reason = REASON_SEQ_DUPLICATE
            elif header.seq > expected_seq:
                reason = REASON_SEQ_GAP
            else:
                reason = REASON_SEQ_REGRESSION
            tail_state, tail_reason = TAIL_CORRUPT, reason
            break

        payload_start = offset + RECORD_HEADER_SIZE
        payload_end = payload_start + header.payload_len
        if payload_end > file_size:
            tail_state, tail_reason = TAIL_TORN, REASON_SHORT_PAYLOAD
            break

        payload = data[payload_start:payload_end]
        try:
            verify_payload(header, payload)
        except CorruptRecordError as error:
            tail_state, tail_reason = TAIL_CORRUPT, error.reason
            break

        if apply is not None:
            key, value = split_payload(header, payload)
            apply(offset, header, key, value)

        valid_records += 1
        last_valid_offset = offset
        last_valid_seq = header.seq
        previous_seq = header.seq
        expected_seq = header.seq + 1
        offset = payload_end

    return LogReport(
        generation=file_header.generation,
        file_size=file_size,
        valid_records=valid_records,
        last_valid_seq=last_valid_seq,
        last_valid_offset=last_valid_offset,
        valid_end_offset=offset,
        tail_state=tail_state,
        tail_reason=tail_reason,
    )


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

MODE_READ_WRITE = "rw"
MODE_READ = "r"

# Durability policies.  These are the only two; see DESIGN.md section 9 for
# exactly what each survives.
DURABILITY_STRICT = "strict"
DURABILITY_RELAXED = "relaxed"

_LOCK_SUFFIX = ".lock"
_SALVAGE_SUFFIX = ".salvage."
_COMPACT_SUFFIX = ".compact"

# Records are buffered into chunks of roughly this size while a compacted
# log is written, so a large store costs a few dozen writes rather than one
# per record.
_COMPACT_CHUNK_BYTES = 64 * 1024

# Owner-only: local application state is nobody else's business (DESIGN.md
# section 23).
_FILE_MODE = 0o600


class CorruptLogError(LedgerError):
    """The log ends in damage that was not repaired.

    Carries the ``LogReport`` describing exactly what was found.
    """

    def __init__(self, message: str, report: "LogReport") -> None:
        super().__init__(message)
        self.report = report


class LockedError(LedgerError):
    """Another process holds the writer lock on this store."""


class ReadOnlyError(LedgerError):
    """A mutation was attempted on a handle opened read-only."""


class ClosedError(LedgerError):
    """The handle has been closed."""


class CompactionError(LedgerError):
    """Compaction failed before it replaced anything.

    The original log is untouched and still authoritative, and the handle
    remains usable: nothing was lost and no state changed.
    """


class WriteError(LedgerError):
    """A write or fsync failed; the handle is poisoned.

    The log may now end in a partial record.  Close the handle and reopen:
    recovery is the one path that repairs a torn tail (DESIGN.md section 8).
    """


def _write_all(fd, data) -> None:
    """Write every byte, tolerating a short write from the kernel."""
    written = 0
    while written < len(data):
        written += os.write(fd, data[written:])


def _remove_quietly(path) -> None:
    """Delete a file that may not exist, without caring if it does not."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _fsync_directory(path) -> None:
    """Make a file's creation or truncation durable, not just its contents.

    Renaming and creating are directory operations, so fsync on the file
    alone does not persist them (DESIGN.md section 22).
    """
    fd = os.open(path.parent or ".", os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _acquire_writer_lock(lock_path):
    """Take the exclusive advisory lock on the sidecar.

    The lock lives beside the data file rather than on it because compaction
    replaces the data file's inode, and a lock held on the old inode would
    silently stop excluding anyone (DESIGN.md section 16).
    """
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, _FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(fd)
        raise LockedError(
            f"another process holds the writer lock on {lock_path}"
        ) from error
    return fd


def _save_salvage(path, discarded: bytes) -> "str | None":
    """Preserve bytes about to be discarded from a corrupt tail.

    A corrupt tail is truncated, but never silently: the bytes are copied
    aside first so they can be examined (DESIGN.md section 11).
    """
    for index in range(1000):
        candidate = path.parent / (path.name + _SALVAGE_SUFFIX + str(index))
        try:
            fd = os.open(
                candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE
            )
        except FileExistsError:
            continue
        try:
            os.write(fd, discarded)
            os.fsync(fd)
        finally:
            os.close(fd)
        return str(candidate)
    return None


@dataclass(frozen=True)
class CompactionResult:
    """What one compaction did."""

    records_before: int
    records_after: int
    bytes_before: int
    bytes_after: int
    generation: int

    @property
    def bytes_reclaimed(self) -> int:
        return self.bytes_before - self.bytes_after


class Ledger:
    """A crash-safe embedded key/value store.

    Open one with :meth:`open`; the constructor is internal.

        db = Ledger.open("state.ledger")
        db.put("user:42", {"name": "Venu"})
        db.get("user:42")

    Keys are non-empty strings.  Values are anything ``json.dumps`` accepts,
    which means the usual JSON round-trip caveats apply: tuples come back as
    lists, non-string dict keys come back as strings, and sets, bytes and
    datetimes are rejected outright rather than silently mangled.
    """

    def __init__(self, path, fd, lock_fd, mode, durability, index, next_seq, report):
        self._path = path
        self._fd = fd
        self._lock_fd = lock_fd
        self._mode = mode
        self._durability = durability
        # key -> the exact JSON payload bytes for that key.  Storing encoded
        # bytes rather than decoded objects is deliberate: it makes it
        # impossible for a caller mutating a returned object to change the
        # store's idea of its own state (DESIGN.md section 13).
        self._index = index
        self._next_seq = next_seq
        self._generation = report.generation
        self._records = report.valid_records
        self._report = report
        self._closed = False
        self._poisoned = None

    # -- lifecycle --------------------------------------------------------

    @classmethod
    def open(cls, path, *, mode=MODE_READ_WRITE, durability=DURABILITY_STRICT,
             repair=True):
        """Open a store, recovering it if the log ends in a damaged tail.

        ``mode`` is ``"rw"`` (default; takes the writer lock and repairs a
        damaged tail) or ``"r"`` (takes no lock and never writes to the
        file).  ``durability`` is ``"strict"`` or ``"relaxed"``.  ``repair``
        applies to write mode only: with ``False`` a damaged tail raises
        ``CorruptLogError`` instead of being truncated.
        """
        if mode not in (MODE_READ_WRITE, MODE_READ):
            raise ValueError(f"mode must be 'rw' or 'r', got {mode!r}")
        if durability not in (DURABILITY_STRICT, DURABILITY_RELAXED):
            raise ValueError(
                f"durability must be 'strict' or 'relaxed', got {durability!r}"
            )

        path = pathlib.Path(path)
        writable = mode == MODE_READ_WRITE
        lock_fd = None
        fd = None
        try:
            # The lock is taken before the file is created or read.  Doing it
            # the other way round would let two processes race to initialise
            # the same store, each believing it created it.
            if writable:
                lock_fd = _acquire_writer_lock(
                    path.parent / (path.name + _LOCK_SUFFIX)
                )
                # A leftover temp file is the debris of a compaction that
                # died before its atomic replace. It is never authoritative
                # and is never resumed: resuming would mean trusting a file
                # whose writer stopped at an unknown point, which is the one
                # thing this design refuses to do anywhere.
                _remove_quietly(path.parent / (path.name + _COMPACT_SUFFIX))
                flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC
            else:
                flags = os.O_RDONLY | os.O_CLOEXEC
            fd = os.open(path, flags, _FILE_MODE)

            created = writable and os.fstat(fd).st_size == 0
            if created:
                os.write(fd, encode_file_header(0))
                os.fsync(fd)
                _fsync_directory(path)

            with open(path, "rb") as handle:
                data = handle.read()

            index = {}

            def apply(offset, header, key, value):
                name = key.decode("utf-8")
                if header.op == OP_PUT:
                    index[name] = value
                else:
                    index.pop(name, None)

            report = replay_log(data, apply=apply)
            if report.repair_required:
                cls._handle_damaged_tail(path, fd, data, report, mode, repair)

            return cls(
                path=path,
                fd=fd,
                lock_fd=lock_fd,
                mode=mode,
                durability=durability,
                index=index,
                next_seq=report.last_valid_seq + 1,
                report=report,
            )
        except BaseException:
            # Never leave the lock held by a handle that was never returned.
            if fd is not None:
                os.close(fd)
            if lock_fd is not None:
                os.close(lock_fd)
            raise

    @staticmethod
    def _handle_damaged_tail(path, fd, data, report, mode, repair):
        message = (
            f"{path}: log ends {report.tail_state} ({report.tail_reason}) at "
            f"offset {report.valid_end_offset}; "
            f"{report.discarded_bytes} bytes after the last valid record"
        )
        if mode == MODE_READ:
            # A reader racing a writer routinely sees a torn tail; that is a
            # snapshot, not damage, and read-only handles never write.
            if report.tail_state == TAIL_TORN:
                return
            raise CorruptLogError(message, report)
        if not repair:
            raise CorruptLogError(message, report)

        if report.tail_state == TAIL_CORRUPT:
            _save_salvage(path, data[report.valid_end_offset :])
        os.ftruncate(fd, report.valid_end_offset)
        os.fsync(fd)
        _fsync_directory(path)

    def close(self) -> None:
        """Release the file and the lock.  Idempotent, and never a commit
        point: every mutation committed when its call returned."""
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._fd)
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    # -- guards -----------------------------------------------------------

    def _check_usable(self) -> None:
        if self._closed:
            raise ClosedError(f"{self._path}: handle is closed")
        if self._poisoned is not None:
            raise WriteError(
                f"{self._path}: handle is poisoned by an earlier failed write "
                f"({self._poisoned}); close and reopen to recover"
            )

    def _check_writable(self) -> None:
        self._check_usable()
        if self._mode != MODE_READ_WRITE:
            raise ReadOnlyError(f"{self._path}: handle is read-only")

    # -- write path -------------------------------------------------------

    def _append(self, op, key_bytes, value_bytes) -> None:
        """Encode, append, make durable, and only then return.

        The caller updates the index after this returns, never before, so
        the in-memory state can never claim something the log does not hold.
        """
        record = encode_record(op, self._next_seq, key_bytes, value_bytes)
        try:
            _write_all(self._fd, record)
            if self._durability == DURABILITY_STRICT:
                os.fsync(self._fd)
        except OSError as error:
            # The log may now end in a partial record.  Refuse to touch this
            # handle again rather than appending after unknown bytes; the
            # next open repairs the tail through the ordinary recovery path.
            self._poisoned = error
            raise WriteError(f"{self._path}: append failed: {error}") from error
        self._next_seq += 1
        self._records += 1

    # -- operations -------------------------------------------------------

    def put(self, key, value) -> None:
        """Store ``value`` under ``key``.  Durable when this returns."""
        self._check_writable()
        key_bytes = _encode_key(key)
        value_bytes = _encode_value(value)
        self._append(OP_PUT, key_bytes, value_bytes)
        self._index[key] = value_bytes

    def get(self, key, default=None):
        """Return the value stored under ``key``, or ``default``.

        The returned object is decoded fresh on every call, so mutating it
        cannot affect the store.
        """
        self._check_usable()
        if not isinstance(key, str):
            raise TypeError(f"key must be str, got {type(key).__name__}")
        payload = self._index.get(key)
        if payload is None:
            return default
        return json.loads(payload)

    def delete(self, key) -> bool:
        """Delete ``key``, returning True if it existed.

        Deleting an absent key writes nothing, so repeated deletes cannot
        grow the log.
        """
        self._check_writable()
        key_bytes = _encode_key(key)
        if key not in self._index:
            return False
        self._append(OP_DELETE, key_bytes, b"")
        del self._index[key]
        return True

    def compact(self):
        """Rewrite the log holding only what is needed to reconstruct the
        current state, and atomically replace the old one.

        Every live key is emitted once, as a PUT carrying the exact value
        bytes already in the index.  Deleted keys are emitted as nothing at
        all: a tombstone exists only to shadow an earlier record for the
        same key, and once no earlier record survives in the file there is
        nothing left for it to shadow, so dropping the pair is what makes a
        delete actually reclaim space.

        Sequence numbers restart at 1.  This is not a preference, it is
        required: ``seq`` is a within-file framing invariant checked for
        strict +1 continuity by recovery (DESIGN.md sections 5 and 10).
        Compaction drops records, so preserving the original numbers would
        leave gaps, and a gap is precisely what recovery classifies as
        corruption.  A fresh basis per file generation keeps the invariant
        intact.  The file header's ``generation`` counter is what survives
        compaction, and it is diagnostic only.

        The original log is never rewritten in place and never truncated.
        The new one is built beside it, validated, fsynced and only then
        swapped in, so a crash at any point leaves either the whole old log
        or the whole new one.
        """
        self._check_writable()
        temp_path = self._path.parent / (self._path.name + _COMPACT_SUFFIX)
        bytes_before = os.fstat(self._fd).st_size
        records_before = self._records
        generation = (self._generation + 1) & 0xFFFFFFFF

        # Sorted so the output is deterministic: the same live state always
        # compacts to byte-identical output.
        live = sorted(self._index.items())

        try:
            self._write_compacted(temp_path, live, generation)
            self._verify_compacted(temp_path)
        except BaseException as error:
            _remove_quietly(temp_path)
            if isinstance(error, (OSError, CompactionError)):
                raise CompactionError(
                    f"{self._path}: compaction failed, original log is "
                    f"unchanged and still authoritative: {error}"
                ) from error
            raise

        try:
            # The temp file is in the same directory as the log because
            # os.replace is only atomic within one filesystem; a temp file
            # elsewhere could land on another mount and degrade to a
            # copy-then-delete with a window where neither file is whole.
            os.replace(temp_path, self._path)
        except OSError as error:
            _remove_quietly(temp_path)
            raise CompactionError(
                f"{self._path}: atomic replace failed, original log is "
                f"unchanged and still authoritative: {error}"
            ) from error

        # Past this line the new file is the authoritative log. The rename
        # itself is a directory operation, so fsyncing the file would not
        # persist it; the directory must be fsynced instead. If this fails
        # the store is still coherent - the path names one complete log or
        # the other - but the rename may not survive a power cut.
        _fsync_directory(self._path)

        try:
            old_fd = self._fd
            self._fd = os.open(
                self._path, os.O_RDWR | os.O_APPEND | os.O_CLOEXEC
            )
            os.close(old_fd)
        except OSError as error:
            self._poisoned = error
            raise WriteError(
                f"{self._path}: compaction completed but the log could not "
                f"be reopened: {error}"
            ) from error

        self._generation = generation
        self._records = len(live)
        self._next_seq = len(live) + FIRST_SEQ
        return CompactionResult(
            records_before=records_before,
            records_after=len(live),
            bytes_before=bytes_before,
            bytes_after=os.fstat(self._fd).st_size,
            generation=generation,
        )

    def _write_compacted(self, temp_path, live, generation) -> None:
        """Build the replacement log beside the original and fsync it."""
        # Any leftover temp file is debris from an interrupted compaction,
        # so remove it first; O_EXCL then guarantees we are writing into a
        # file nobody else created.
        _remove_quietly(temp_path)
        fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            _FILE_MODE,
        )
        try:
            buffer = bytearray(encode_file_header(generation))
            seq = FIRST_SEQ
            for key, value_bytes in live:
                # The value bytes are copied straight out of the index, so
                # compaction never re-encodes a value and cannot change one.
                buffer += encode_record(
                    OP_PUT, seq, key.encode("utf-8"), value_bytes
                )
                seq += 1
                if len(buffer) >= _COMPACT_CHUNK_BYTES:
                    _write_all(fd, buffer)
                    buffer.clear()
            if buffer:
                _write_all(fd, buffer)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _verify_compacted(self, temp_path) -> None:
        """Replay the replacement before trusting it.

        Reads the file back and checks that it reconstructs exactly the
        current index.  Counting matches rather than building a second
        dictionary keeps this from doubling the store's memory.
        """
        with open(temp_path, "rb") as handle:
            data = handle.read()

        verified = 0

        def check(offset, header, key, value):
            nonlocal verified
            name = key.decode("utf-8")
            if header.op != OP_PUT or self._index.get(name) != value:
                raise CompactionError(
                    f"compacted log disagrees with the index at key {name!r}"
                )
            verified += 1

        report = replay_log(data, apply=check)
        if report.tail_state != TAIL_CLEAN:
            raise CompactionError(
                f"compacted log does not replay clean: {report.tail_state} "
                f"({report.tail_reason})"
            )
        if verified != len(self._index):
            raise CompactionError(
                f"compacted log holds {verified} records, "
                f"expected {len(self._index)}"
            )

    def scan(self, prefix: str = ""):
        """Yield ``(key, value)`` pairs in key order, optionally filtered.

        The set of keys is snapshotted when ``scan`` is called; values are
        decoded lazily as they are yielded, so a prefix scan does not decode
        the whole store.
        """
        self._check_usable()
        if not isinstance(prefix, str):
            raise TypeError(f"prefix must be str, got {type(prefix).__name__}")
        snapshot = [
            (key, payload)
            for key, payload in sorted(self._index.items())
            if key.startswith(prefix)
        ]
        for key, payload in snapshot:
            yield key, json.loads(payload)

    # -- introspection ----------------------------------------------------

    def __len__(self) -> int:
        self._check_usable()
        return len(self._index)

    def __contains__(self, key) -> bool:
        self._check_usable()
        return isinstance(key, str) and key in self._index

    @property
    def path(self):
        return self._path

    @property
    def durability(self) -> str:
        return self._durability

    @property
    def recovery_report(self) -> LogReport:
        """The report produced when this handle was opened."""
        return self._report

    def __repr__(self) -> str:
        state = "closed" if self._closed else (
            "poisoned" if self._poisoned is not None else self._mode
        )
        return f"<Ledger {str(self._path)!r} {state} keys={len(self._index)}>"


def _encode_key(key) -> bytes:
    if not isinstance(key, str):
        raise TypeError(f"key must be str, got {type(key).__name__}")
    encoded = key.encode("utf-8")
    if not encoded:
        raise ValueError("key must not be empty")
    if len(encoded) > MAX_KEY_BYTES:
        raise ValueError(
            f"key is {len(encoded)} bytes, over the {MAX_KEY_BYTES} byte limit"
        )
    return encoded


def _encode_value(value) -> bytes:
    """Serialize a value to its canonical JSON bytes.

    ``sort_keys`` makes the encoding of a given value deterministic, which
    keeps compaction output and test fixtures byte-stable.  ``allow_nan`` is
    off because NaN and Infinity are not JSON and other readers reject them.
    """
    try:
        text = json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    except ValueError as error:
        raise ValueError(f"value is not representable as JSON: {error}") from error
    except TypeError as error:
        raise TypeError(f"value is not JSON-encodable: {error}") from error
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_VALUE_BYTES:
        raise ValueError(
            f"value encodes to {len(encoded)} bytes, over the "
            f"{MAX_VALUE_BYTES} byte limit"
        )
    return encoded


# --------------------------------------------------------------------------
# Command line interface
# --------------------------------------------------------------------------
#
# A thin skin over the public API: every command opens a store, calls one
# method, and prints the result.  No storage logic lives here.

# Exit codes.  Deliberately few, and stable.
EXIT_OK = 0
EXIT_USAGE = 1      # bad arguments, bad JSON, missing key, missing file
EXIT_CORRUPT = 2    # not a Ledger file, or a corrupt log we refused to open
EXIT_LOCKED = 3     # another process holds the writer lock
EXIT_IO = 4         # write, fsync or compaction failure

# get() returns the caller's default for a missing key, and None is a
# perfectly valid stored value, so distinguishing the two needs a sentinel.
_MISSING = object()


def _fail(message, code):
    print(f"ledger: {message}", file=sys.stderr)
    return code


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 for a usage error, but 2 is this CLI's code for a
    corrupt store.  Usage errors are bad input, so they exit EXIT_USAGE
    like every other bad input."""

    def error(self, message):
        self.print_usage(sys.stderr)
        raise SystemExit(_fail(message, EXIT_USAGE))

    def exit(self, status=0, message=None):
        if message:
            print(message, file=sys.stderr)
        raise SystemExit(EXIT_USAGE if status == 2 else status)


def _cmd_put(args):
    """Read commands never lock; write commands do, and repair on open."""
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError as error:
        return _fail(f"VALUE is not valid JSON: {error}", EXIT_USAGE)
    try:
        with Ledger.open(args.file) as db:
            db.put(args.key, value)
    except (TypeError, ValueError) as error:
        return _fail(str(error), EXIT_USAGE)
    return EXIT_OK


def _cmd_get(args):
    with Ledger.open(args.file, mode=MODE_READ) as db:
        value = db.get(args.key, _MISSING)
    if value is _MISSING:
        return _fail(f"key not found: {args.key}", EXIT_USAGE)
    print(_dump(value))
    return EXIT_OK


def _cmd_delete(args):
    # Opening for write would create the store, so deleting a key from a
    # store that does not exist would silently leave an empty one behind.
    # Only put brings a store into existence.
    if not os.path.exists(args.file):
        raise FileNotFoundError(args.file)
    with Ledger.open(args.file) as db:
        try:
            deleted = db.delete(args.key)
        except (TypeError, ValueError) as error:
            return _fail(str(error), EXIT_USAGE)
    if not deleted:
        return _fail(f"key not found: {args.key}", EXIT_USAGE)
    return EXIT_OK


def _cmd_scan(args):
    with Ledger.open(args.file, mode=MODE_READ) as db:
        for key, value in db.scan(prefix=args.prefix):
            print(f"{key}\t{_dump(value)}")
    return EXIT_OK


def _human_bytes(count):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if count < 1024 or unit == "GiB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{count} B"
        count /= 1024


def forensic_scan(data, start):
    """Look for record-shaped bytes beyond a damaged region.

    Purely informational. This never feeds recovery: it does not change
    the replay result, the repair boundary or the logical state, and
    nothing it finds is ever loaded into a store. Recovery stops at the
    first bad record on purpose (DESIGN.md section 10) because past a
    damaged region a real record cannot be told from stale bytes left in a
    reused block. This function exists so a human can see what that
    decision is discarding, and decide for themselves.

    Returns ``(markers, plausible)``: how many record magics appear after
    ``start``, and how many of those are followed by a header that decodes.
    """
    markers = 0
    plausible = 0
    offset = data.find(RECORD_MAGIC, start)
    while offset != -1:
        markers += 1
        candidate = data[offset : offset + RECORD_HEADER_SIZE]
        if len(candidate) == RECORD_HEADER_SIZE:
            try:
                decode_record_header(candidate)
                plausible += 1
            except CorruptRecordError:
                pass
        offset = data.find(RECORD_MAGIC, offset + 1)
    return markers, plausible


def _cmd_inspect(args):
    """Report on a store without touching it.

    Deliberately does not go through Ledger.open: opening read-write would
    lock and repair, and even a read-only open raises on a corrupt tail
    rather than describing it. Diagnosis has to survive the very damage it
    is meant to describe, so this reads the bytes and replays them through
    the public reader instead.
    """
    with open(args.file, "rb") as handle:
        data = handle.read()

    live = {}

    def apply(offset, header, key, value):
        if header.op == OP_PUT:
            live[key] = len(key) + len(value)
        else:
            live.pop(key, None)

    report = replay_log(data, apply=apply)

    body = max(report.valid_end_offset - FILE_HEADER_SIZE, 0)
    live_bytes = sum(
        RECORD_HEADER_SIZE + payload for payload in live.values()
    )
    dead = max(body - live_bytes, 0)

    print(f"path:             {args.file}")
    print(f"file size:        {_human_bytes(report.file_size)} "
          f"({report.file_size} bytes)")
    print(f"format version:   {FORMAT_VERSION}")
    print(f"generation:       {report.generation}")
    print(f"valid records:    {report.valid_records}")
    print(f"live keys:        {len(live)}")
    print(f"valid end offset: {report.valid_end_offset}")
    print(f"tail state:       {report.tail_state.upper()}")
    print(f"tail reason:      {report.tail_reason or '-'}")
    print(f"discarded bytes:  {report.discarded_bytes}")
    if body:
        print(f"dead bytes:       {dead} ({dead / body:.1%} reclaimable)")
    else:
        print("dead bytes:       0")
    print(f"repair required:  {'yes' if report.repair_required else 'no'}")

    if report.repair_required:
        markers, plausible = forensic_scan(data, report.valid_end_offset)
        if markers:
            print()
            print("forensic scan beyond the damage "
                  "(UNTRUSTED / NOT RECOVERED):")
            print(f"  record markers found:   {markers}")
            print(f"  headers that decode:    {plausible}")
            print("  These bytes are NOT part of the recovered state and "
                  "never will be.")
            print("  Recovery stops at the first bad record because past it "
                  "a real record")
            print("  cannot be told from stale bytes in a reused block.")
    return EXIT_OK


def _cmd_compact(args):
    if not os.path.exists(args.file):
        raise FileNotFoundError(args.file)
    with Ledger.open(args.file) as db:
        result = db.compact()
    print(f"records: {result.records_before} -> {result.records_after}")
    print(f"size:    {_human_bytes(result.bytes_before)} -> "
          f"{_human_bytes(result.bytes_after)}")
    print("status:  compacted")
    return EXIT_OK


def _build_parser():
    parser = _Parser(
        prog="ledger",
        description="Crash-safe embedded state store.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    put = commands.add_parser("put", help="store VALUE (JSON) under KEY")
    put.add_argument("file")
    put.add_argument("key")
    put.add_argument("value", metavar="VALUE", help="a JSON document")
    put.set_defaults(run=_cmd_put)

    get = commands.add_parser("get", help="print the value stored under KEY")
    get.add_argument("file")
    get.add_argument("key")
    get.set_defaults(run=_cmd_get)

    delete = commands.add_parser("delete", help="remove KEY")
    delete.add_argument("file")
    delete.add_argument("key")
    delete.set_defaults(run=_cmd_delete)

    scan = commands.add_parser("scan", help="print every key and value")
    scan.add_argument("file")
    scan.add_argument("--prefix", default="", help="only keys with this prefix")
    scan.set_defaults(run=_cmd_scan)

    inspect = commands.add_parser(
        "inspect", help="report on a store without modifying it"
    )
    inspect.add_argument("file")
    inspect.set_defaults(run=_cmd_inspect)

    compact = commands.add_parser(
        "compact", help="rewrite the log without obsolete records"
    )
    compact.add_argument("file")
    compact.set_defaults(run=_cmd_compact)

    return parser


def main(argv=None):
    """Entry point for ``python3 -m ledger`` and ``python3 ledger.py``.

    Expected failures print one line to stderr and return a fixed exit
    code; none of them produce a traceback.  An unexpected exception is a
    bug in Ledger and is deliberately left to propagate, because a
    stack trace is more use than a swallowed error.
    """
    args = _build_parser().parse_args(argv)
    try:
        return args.run(args)
    except FileNotFoundError:
        return _fail(f"no such store: {args.file}", EXIT_USAGE)
    except IsADirectoryError:
        return _fail(f"not a file: {args.file}", EXIT_USAGE)
    except LockedError as error:
        return _fail(str(error), EXIT_LOCKED)
    except (FormatError, CorruptLogError) as error:
        return _fail(str(error), EXIT_CORRUPT)
    except (WriteError, CompactionError) as error:
        return _fail(str(error), EXIT_IO)
    except OSError as error:
        return _fail(f"{args.file}: {error}", EXIT_IO)


if __name__ == "__main__":
    sys.exit(main())
