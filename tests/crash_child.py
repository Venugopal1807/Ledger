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
    python3 tests/crash_child.py PATH verify

Protocol, one line at a time on stdout, each flushed:

    COMMITTED <seq> <key>    one per record the store accepted
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

    verify = subparsers.add_parser("verify")
    verify.add_argument("--mode", choices=("rw", "r"), default="rw")
    verify.set_defaults(run=scenario_verify)

    args = parser.parse_args(argv)
    args.run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
