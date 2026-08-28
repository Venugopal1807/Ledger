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
    python3 tests/crash_child.py PATH verify

Protocol, one line at a time on stdout, each flushed:

    COMMITTED <seq> <key>    one per record the store accepted
    TORN <kept> <total>      bytes of a partial record appended to the file
    READY <detail>           everything is done; waiting for the parent
    <parent writes "GO">     the child then raises SIGKILL on itself

Exit status for a killed child is -SIGKILL; the child never reaches a
normal exit in the kill scenarios, and never closes the store.
"""

import argparse
import json
import os
import signal
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

    verify = subparsers.add_parser("verify")
    verify.add_argument("--mode", choices=("rw", "r"), default="rw")
    verify.set_defaults(run=scenario_verify)

    args = parser.parse_args(argv)
    args.run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
