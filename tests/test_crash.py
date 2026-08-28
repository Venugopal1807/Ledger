"""Crash-recovery tests driven by a real subprocess that really is killed.

The child in ``crash_child.py`` writes through the public Ledger API and
then raises SIGKILL on itself.  Nothing is mocked, nothing is simulated in
process, and ledger.py contains no test hook: the write path under test is
the write path that ships.

Synchronisation is a pipe handshake in both directions.  No test here
sleeps, polls for timing, or races a signal against a write.  The timeouts
that appear below are watchdogs that turn a hang into a clear failure; they
are never how the test decides that the child is ready.
"""

import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest

import crash_child
import ledger

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash_child.py")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Watchdog only.  A healthy child answers immediately; this exists so a bug
# fails the suite in seconds instead of hanging a CI job forever.
WATCHDOG_SECONDS = 30


class ChildFailure(AssertionError):
    pass


class CrashChild:
    """Parent-side driver for one child process.

    Reads whole lines from the child's stdout by buffering raw bytes off the
    pipe.  Reading the raw descriptor rather than a text wrapper matters:
    a buffered reader can pull two lines into memory at once, after which a
    readiness check on the descriptor would report nothing pending while a
    complete line was already sitting in the buffer.
    """

    def __init__(self, path, *args):
        self.args = [sys.executable, CHILD, str(path), *args]
        self.proc = subprocess.Popen(
            self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO_ROOT,
        )
        self._buffer = b""
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.proc.stdout, selectors.EVENT_READ)

    # -- lifecycle --------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.terminate()
        return False

    def terminate(self):
        """Never leave a child behind, on any exit path."""
        self._selector.close()
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.communicate(timeout=WATCHDOG_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.proc.kill()
            self.proc.communicate()

    def _diagnose(self, problem):
        self.proc.kill()
        try:
            _out, err = self.proc.communicate(timeout=WATCHDOG_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            err = b"<child would not die>"
        return ChildFailure(
            f"{problem}\n"
            f"  command : {' '.join(self.args)}\n"
            f"  returncode: {self.proc.returncode}\n"
            f"  buffered stdout: {self._buffer!r}\n"
            f"  stderr:\n{(err or b'').decode(errors='replace')}"
        )

    # -- protocol ---------------------------------------------------------

    def readline(self, what):
        while b"\n" not in self._buffer:
            if not self._selector.select(WATCHDOG_SECONDS):
                raise self._diagnose(f"timed out waiting for {what}")
            chunk = os.read(self.proc.stdout.fileno(), 4096)
            if not chunk:
                raise self._diagnose(f"child exited before sending {what}")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line.decode()

    def expect(self, prefix):
        line = self.readline(prefix)
        if not line.startswith(prefix):
            raise self._diagnose(f"expected a {prefix!r} line, got {line!r}")
        return line

    def release(self):
        """Acknowledge, which is the child's cue to kill itself."""
        self.proc.stdin.write(b"GO\n")
        self.proc.stdin.flush()

    def expect_killed(self):
        try:
            self.proc.wait(timeout=WATCHDOG_SECONDS)
        except subprocess.TimeoutExpired:
            raise self._diagnose("child did not die after being released")
        if self.proc.returncode != -signal.SIGKILL:
            raise self._diagnose(
                f"child exited {self.proc.returncode}, expected -SIGKILL "
                f"({-signal.SIGKILL}); it must not have shut down cleanly"
            )


class CrashTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "state.ledger")
        self.addCleanup(self._tmp.cleanup)

    def child(self, *args):
        child = CrashChild(self.path, *args)
        self.addCleanup(child.terminate)
        return child

    def read_file(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def kill_after_commits(self, records, durability=ledger.DURABILITY_STRICT):
        """Run mode A and return the keys the child said it committed."""
        committed = []
        with self.child(
            "commit-then-kill", "--records", str(records),
            "--durability", durability,
        ) as child:
            for index in range(1, records + 1):
                line = child.expect("COMMITTED")
                _tag, seq, key = line.split()
                self.assertEqual(int(seq), index)
                committed.append(key)
            child.expect("READY")
            child.release()
            child.expect_killed()
        return committed

    def verify_in_fresh_process(self, mode="rw"):
        """Reopen the store in an interpreter that never saw the writer."""
        result = subprocess.run(
            [sys.executable, CHILD, self.path, "verify", "--mode", mode],
            capture_output=True, cwd=REPO_ROOT, timeout=WATCHDOG_SECONDS,
        )
        if result.returncode != 0:
            self.fail(
                f"verifier failed ({result.returncode}):\n"
                f"{result.stderr.decode(errors='replace')}"
            )
        line = result.stdout.decode().strip()
        self.assertTrue(line.startswith("STATE "), line)
        return json.loads(line[len("STATE "):])

    def assert_store_holds_exactly(self, keys):
        """The recovered state must be precisely the committed records: all
        of them, nothing extra, and a log that replays clean."""
        expected = {
            key: crash_child.record_value(int(key.split(":")[1]))
            for key in keys
        }
        state = self.verify_in_fresh_process()
        self.assertEqual(state["entries"], expected)
        self.assertEqual(state["keys"], len(keys))

        report = ledger.replay_log(self.read_file())
        self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
        self.assertIsNone(report.tail_reason)
        self.assertEqual(report.valid_records, len(keys))
        self.assertEqual(report.last_valid_seq, len(keys))
        self.assertEqual(report.valid_end_offset, len(self.read_file()))


class TestProcessInterruption(CrashTestCase):
    """Mode A: SIGKILL after N committed writes.

    What this proves depends on the durability mode, and the two are not the
    same claim:

    * strict  - each put fsynced before returning, so the records are on
      stable media subject to the filesystem and device honouring fsync.
      This is the documented durability guarantee.
    * relaxed - no per-write fsync.  The records survive here because the
      bytes are in the operating system's page cache, which outlives the
      killed process.  That is an *observed process-interruption property*
      of this filesystem and OS, and it is NOT a power-loss guarantee.
    """

    def test_strict_durability_survives_sigkill(self):
        keys = self.kill_after_commits(5, ledger.DURABILITY_STRICT)
        self.assertEqual(len(keys), 5)
        self.assert_store_holds_exactly(keys)

    def test_relaxed_durability_survives_sigkill_observed(self):
        # Observed behaviour on the tested filesystem and OS. Relaxed mode
        # makes no power-loss promise; see the class docstring.
        keys = self.kill_after_commits(5, ledger.DURABILITY_RELAXED)
        self.assertEqual(len(keys), 5)
        self.assert_store_holds_exactly(keys)

    def test_various_record_counts(self):
        for count in (1, 2, 10, 50):
            with self.subTest(records=count):
                self.setUp()
                keys = self.kill_after_commits(count)
                self.assert_store_holds_exactly(keys)

    def test_no_unexpected_records_appear(self):
        keys = self.kill_after_commits(3)
        seen = []
        ledger.replay_log(
            self.read_file(),
            apply=lambda offset, header, key, value: seen.append(key.decode()),
        )
        self.assertEqual(seen, keys)

    def test_killed_writer_releases_its_lock(self):
        # The child never closed the store. The lock must still be gone,
        # released by the kernel when the process died.
        self.kill_after_commits(3)
        db = ledger.Ledger.open(self.path)
        self.addCleanup(db.close)
        self.assertEqual(len(db), 3)

    def test_writes_continue_after_the_crash(self):
        keys = self.kill_after_commits(4)
        db = ledger.Ledger.open(self.path)
        db.put("after:crash", {"ok": True})
        db.close()
        report = ledger.replay_log(self.read_file())
        self.assertEqual(report.tail_state, ledger.TAIL_CLEAN)
        self.assertEqual(report.valid_records, len(keys) + 1)
        self.assertEqual(report.last_valid_seq, len(keys) + 1)
        self.assertEqual(self.verify_in_fresh_process()["keys"], len(keys) + 1)

    def test_repeated_crashes_accumulate_state(self):
        # Each round crashes a fresh writer against the store the previous
        # crash left behind.
        for round_number in range(3):
            with self.subTest(round=round_number):
                db = ledger.Ledger.open(self.path)
                db.put(f"survivor:{round_number}", round_number)
                db.close()
                self.kill_after_commits(2)
                state = self.verify_in_fresh_process()
                self.assertIn(f"survivor:{round_number}", state["entries"])

    def test_reader_can_open_the_crashed_store(self):
        self.kill_after_commits(3)
        state = self.verify_in_fresh_process(mode="r")
        self.assertEqual(state["keys"], 3)
        self.assertEqual(state["tail_state"], ledger.TAIL_CLEAN)


class TestHarnessSafety(CrashTestCase):
    def test_child_reports_sigkill_exit_status(self):
        with self.child("commit-then-kill", "--records", "1") as child:
            child.expect("COMMITTED")
            child.expect("READY")
            child.release()
            child.expect_killed()
            self.assertEqual(child.proc.returncode, -signal.SIGKILL)

    def test_unexpected_child_exit_is_diagnosed_not_hung(self):
        # Point the child at a directory so opening the store fails. The
        # driver must surface the failure quickly with the child's stderr,
        # rather than blocking on a line that will never arrive.
        child = CrashChild(self.dir, "commit-then-kill", "--records", "1")
        self.addCleanup(child.terminate)
        with self.assertRaises(ChildFailure) as caught:
            child.expect("COMMITTED")
        message = str(caught.exception)
        self.assertIn("child exited before sending", message)
        self.assertIn("stderr:", message)

    def test_no_child_is_left_running(self):
        child = self.child("commit-then-kill", "--records", "1")
        child.expect("COMMITTED")
        child.terminate()
        self.assertIsNotNone(child.proc.poll())


if __name__ == "__main__":
    unittest.main()
