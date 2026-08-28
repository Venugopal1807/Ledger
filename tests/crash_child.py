"""Standalone crash scenarios, run as a child process and killed for real.

This script exists so that fault injection stays entirely out of ledger.py.
There is no crash hook, no environment-variable switch and no injectable
write function anywhere in the library: the seam lives here, in a separate
process that kills itself with SIGKILL.  The code path the tests exercise is
the code path that ships.

Synchronisation with the parent is a pipe handshake, never a timer.  The
child announces what it has done, then blocks reading stdin until the parent
acknowledges, then kills itself.  Nothing depends on timing.

    python3 tests/crash_child.py PATH commit-then-kill --records 5
    python3 tests/crash_child.py PATH torn-record --records 3 --keep 20
    python3 tests/crash_child.py PATH compact-then-kill --at before-replace
    python3 tests/crash_child.py PATH verify

Protocol, one line at a time on stdout, each flushed:

    COMMITTED <seq> <key>    one per record the store accepted
    TORN <kept> <total>      bytes of a partial record appended to the file
    ARMED <point>            a compaction crash point is armed
    READY <detail>           everything is done; waiting for the parent
    <parent writes "GO">     the child then raises SIGKILL on itself

Exit status for a killed child is -SIGKILL; the child never reaches a
normal exit in the kill scenarios, and never closes the store.
"""

import argparse
import json
import os
import signal
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger  # noqa: E402  (path setup must happen first)


def emit(line):
    """Send one line to the parent and make sure it has actually left."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def wait_for_parent():
    """Block until the parent acknowledges.  This is the synchronisation
    point: the child is provably still alive and idle when it returns."""
    line = sys.stdin.readline()
    if line.strip() != "GO":
        raise SystemExit(f"expected GO from parent, got {line!r}")


def die_now():
    """Terminate uncatchably: no finally blocks, no atexit, no flush, and
    no chance for Ledger to close the store or release its lock."""
    os.kill(os.getpid(), signal.SIGKILL)
    raise SystemExit("SIGKILL did not take effect")  # unreachable


def record_key(index):
    return f"committed:{index:04d}"


def record_value(index):
    return {"n": index, "payload": "x" * 16}


def scenario_commit_then_kill(args):
    """Mode A: write N records through the public API, then die abruptly.

    The store is deliberately left open.  Whatever survives is what the
    write path and the operating system actually put on disk, not what a
    tidy shutdown flushed.
    """
    db = ledger.Ledger.open(args.path, durability=args.durability)
    for index in range(1, args.records + 1):
        key = record_key(index)
        db.put(key, record_value(index))
        emit(f"COMMITTED {index} {key}")
    emit(f"READY committed={args.records} durability={args.durability}")
    wait_for_parent()
    die_now()


# The partial record Mode B appends.  Fixed so the parent can compute the
# interesting truncation points (32 + key length, total - 1) without having
# to parse anything back out of the file.
TORN_KEY = b"torn:key"
TORN_VALUE = b'{"v":999}'
TORN_RECORD_SIZE = ledger.RECORD_HEADER_SIZE + len(TORN_KEY) + len(TORN_VALUE)


def scenario_torn_record(args):
    """Mode B: commit N records, then append a partial record and die.

    A power failure part-way through a write leaves a prefix of a record at
    the end of the file.  Appending the first K bytes ourselves produces
    exactly that on-disk shape, deterministically, at a byte offset we
    choose - rather than racing a signal against a write() the kernel will
    usually finish anyway.

    The bytes go straight to the file rather than through Ledger, because
    that is the point: no writer would ever emit a partial record, and the
    library has no code path that could be asked to.  The store is left
    open and unrepaired.
    """
    db = ledger.Ledger.open(args.path, durability=args.durability)
    for index in range(1, args.records + 1):
        key = record_key(index)
        db.put(key, record_value(index))
        emit(f"COMMITTED {index} {key}")

    with open(args.path, "rb") as handle:
        next_seq = ledger.replay_log(handle.read()).last_valid_seq + 1
    record = ledger.encode_record(ledger.OP_PUT, next_seq, TORN_KEY, TORN_VALUE)
    if not 1 <= args.keep <= len(record):
        raise SystemExit(f"--keep must be 1..{len(record)}, got {args.keep}")

    fragment = record[: args.keep]
    if args.garbage:
        # Fill the record out to its full length with bytes that are not the
        # ones the checksum covers, which is how a torn write landing in a
        # reused disk block presents: complete-looking, but wrong.
        fragment += b"\xa5" * (len(record) - args.keep)

    fd = os.open(args.path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, fragment)
        os.fsync(fd)
    finally:
        os.close(fd)

    emit(f"TORN {len(fragment)} {len(record)}")
    emit(f"READY committed={args.records} keep={args.keep} garbage={args.garbage}")
    wait_for_parent()
    die_now()


def scenario_verify(args):
    """Open the store in this fresh process and report what it holds.

    Used by the parent to confirm recovery from an interpreter that has
    never seen the writer, and by the demo to show the state after a crash.
    """
    db = ledger.Ledger.open(args.path, mode=args.mode)
    report = db.recovery_report
    state = {
        "keys": len(db),
        "entries": dict(db.scan()),
        "tail_state": report.tail_state,
        "tail_reason": report.tail_reason,
        "valid_records": report.valid_records,
        "last_valid_seq": report.last_valid_seq,
        "valid_end_offset": report.valid_end_offset,
        "file_size": report.file_size,
        "repair_required": report.repair_required,
    }
    db.close()
    emit("STATE " + json.dumps(state, sort_keys=True))


# ---------------------------------------------------------------------------
# Compaction crash points
# ---------------------------------------------------------------------------
#
# The crash points are armed by wrapping os functions *in this child
# process*.  ledger.py is untouched and has no idea a test exists: it calls
# os.open, os.write, os.fsync and os.replace exactly as it always does, and
# this script decides that one of those calls is the last thing the process
# will ever do.  Patching the syscall layer in a throwaway process is not
# the same as putting a hook in the library.

COMPACT_CRASH_POINTS = (
    "before-temp-create",     # 1. temp file does not exist yet
    "after-temp-create",      # 2. temp file exists, empty
    "during-temp-write",      # 3. temp file partially written
    "before-temp-fsync",      # 4. temp fully written, not yet durable
    "after-temp-fsync",       # 5. temp complete and durable
    "before-replace",         # 6. old log still authoritative
    "after-replace",          # 7. new log authoritative, rename maybe not durable
    "before-dir-fsync",       # 8. same instant, named from the other side
    "after-dir-fsync",        # 9. fully committed
)


def _is_directory(fd):
    return stat.S_ISDIR(os.fstat(fd).st_mode)


def arm_compaction_crash(point, temp_suffix=".compact"):
    """Wrap the os calls compaction makes so one of them kills us.

    Nothing here is timing based.  Each point is a specific, named position
    in the compaction state machine, reached deterministically.
    """
    if point not in COMPACT_CRASH_POINTS:
        raise SystemExit(f"unknown crash point {point!r}")

    real_open, real_write = os.open, os.write
    real_fsync, real_replace = os.fsync, os.replace
    real_close = os.close
    temp_fds = set()
    writes = {"count": 0}

    def patched_open(path, flags, mode=0o777, **kwargs):
        is_temp = str(path).endswith(temp_suffix)
        if is_temp and point == "before-temp-create":
            die_now()
        fd = real_open(path, flags, mode, **kwargs)
        if is_temp:
            temp_fds.add(fd)
            if point == "after-temp-create":
                die_now()
        return fd

    def patched_write(fd, data):
        if fd in temp_fds:
            writes["count"] += 1
            # Die on the second chunk, so the temp file is left genuinely
            # half written rather than empty.
            if point == "during-temp-write" and writes["count"] == 2:
                die_now()
        return real_write(fd, data)

    def patched_close(fd):
        # Descriptor numbers are recycled the moment they are closed, so a
        # closed temp fd must leave the set or the next open - the parent
        # directory, as it happens - inherits its identity.
        temp_fds.discard(fd)
        return real_close(fd)

    def patched_fsync(fd):
        # Ask what the descriptor actually is before trusting bookkeeping:
        # a directory is never the temp file, whatever the numbers say.
        if _is_directory(fd):
            if point == "before-dir-fsync":
                die_now()
            result = real_fsync(fd)
            if point == "after-dir-fsync":
                die_now()
            return result
        if fd in temp_fds:
            if point == "before-temp-fsync":
                die_now()
            result = real_fsync(fd)
            if point == "after-temp-fsync":
                die_now()
            return result
        return real_fsync(fd)

    def patched_replace(src, dst, **kwargs):
        if point == "before-replace":
            die_now()
        result = real_replace(src, dst, **kwargs)
        if point == "after-replace":
            die_now()
        return result

    os.open, os.write, os.fsync, os.replace, os.close = (
        patched_open, patched_write, patched_fsync, patched_replace,
        patched_close,
    )


def scenario_compact_then_kill(args):
    """Write a history with obsolete records, then die inside compaction.

    Whichever point is armed, reopening must find either the whole original
    log or the whole compacted one - never a mixture, never missing state,
    never a resurrected deleted key.
    """
    db = ledger.Ledger.open(args.path, durability=args.durability)
    for round_number in range(args.rounds):
        for index in range(1, args.records + 1):
            db.put(record_key(index), {
                "n": index,
                "round": round_number,
                "pad": "x" * args.value_bytes,
            })
    for index in args.delete:
        db.delete(record_key(index))
    for index in range(1, args.records + 1):
        key = record_key(index)
        if index not in args.delete:
            emit(f"COMMITTED {index} {key}")

    # The during-temp-write point needs the compacted output to span more
    # than one buffered write. That is driven by how much live data there
    # is (--value-bytes), never by reaching into Ledger's internals.
    arm_compaction_crash(args.at)
    emit(f"ARMED {args.at}")
    wait_for_parent()
    db.compact()
    # Only reachable if the armed point was never hit, which is a bug in
    # the harness rather than in Ledger.
    raise SystemExit(f"crash point {args.at!r} was never reached")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    subparsers = parser.add_subparsers(dest="scenario", required=True)

    kill = subparsers.add_parser("commit-then-kill")
    kill.add_argument("--records", type=int, default=5)
    kill.add_argument(
        "--durability",
        choices=(ledger.DURABILITY_STRICT, ledger.DURABILITY_RELAXED),
        default=ledger.DURABILITY_STRICT,
    )
    kill.set_defaults(run=scenario_commit_then_kill)

    torn = subparsers.add_parser("torn-record")
    torn.add_argument("--records", type=int, default=3)
    torn.add_argument("--keep", type=int, required=True)
    torn.add_argument("--garbage", action="store_true")
    torn.add_argument(
        "--durability",
        choices=(ledger.DURABILITY_STRICT, ledger.DURABILITY_RELAXED),
        default=ledger.DURABILITY_STRICT,
    )
    torn.set_defaults(run=scenario_torn_record)

    compact = subparsers.add_parser("compact-then-kill")
    compact.add_argument("--records", type=int, default=6)
    compact.add_argument("--rounds", type=int, default=4)
    compact.add_argument("--delete", type=int, nargs="*", default=[2, 5])
    compact.add_argument("--value-bytes", type=int, default=16)
    compact.add_argument("--at", required=True, choices=COMPACT_CRASH_POINTS)
    compact.add_argument(
        "--durability",
        choices=(ledger.DURABILITY_STRICT, ledger.DURABILITY_RELAXED),
        default=ledger.DURABILITY_STRICT,
    )
    compact.set_defaults(run=scenario_compact_then_kill)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--mode", choices=("rw", "r"), default="rw")
    verify.set_defaults(run=scenario_verify)

    args = parser.parse_args(argv)
    args.run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
