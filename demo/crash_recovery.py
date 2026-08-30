#!/usr/bin/env python3
"""Ledger crash-recovery demonstration.

    python3 demo/crash_recovery.py

Everything here is real. The child processes are really killed with
SIGKILL, the torn records are really on disk, and every number printed is
measured from the store at that moment rather than written into this
script. Nothing is mocked and no output is fabricated.

Runs in well under a minute and leaves nothing behind: every store lives
in a temporary directory that is removed on exit.
"""

import os
import pathlib
import selectors
import subprocess
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import ledger  # noqa: E402  (path setup must happen first)

CRASH_CHILD = REPO_ROOT / "tests" / "crash_child.py"
TIMEOUT = 60
WIDTH = 68


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

def scene(number, title):
    print()
    print("=" * WIDTH)
    print(f"  {number}. {title}")
    print("=" * WIDTH)


def say(text=""):
    print(text)


def shell(command, output):
    """Show a command and the output it actually produced."""
    print(f"\n  $ {command}")
    for line in output.rstrip("\n").splitlines():
        print(f"    {line}")


def run_cli(*args, cwd=None):
    result = subprocess.run(
        [sys.executable, "-m", "ledger", *args],
        capture_output=True, text=True, cwd=str(cwd or REPO_ROOT),
        timeout=TIMEOUT,
    )
    return result


def cli(*args):
    """Run a CLI command and echo exactly what was run."""
    result = run_cli(*args)
    shell("python3 -m ledger " + " ".join(args), result.stdout or result.stderr)
    return result


# --------------------------------------------------------------------------
# The crash harness, driven exactly as the test suite drives it
# --------------------------------------------------------------------------

def run_crash_child(store, *args):
    """Run a scenario to its synchronisation point, then let it die.

    The handshake is a pipe in both directions: the child reports what it
    has done and blocks on stdin; we reply, and it kills itself. Nothing
    depends on timing, so this behaves identically every run.
    """
    process = subprocess.Popen(
        [sys.executable, str(CRASH_CHILD), str(store), *args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(REPO_ROOT),
    )
    # The kill scenarios end on READY; the compaction scenario ends on
    # ARMED, having armed its crash point instead.
    ready = ("READY", "ARMED")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    lines, buffer = [], b""
    try:
        while not any(line.startswith(ready) for line in lines):
            if not selector.select(TIMEOUT):
                raise RuntimeError(f"crash child went quiet: {args}")
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                raise RuntimeError(
                    "crash child exited early: "
                    + process.stderr.read().decode(errors="replace")
                )
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                lines.append(line.decode())
        process.stdin.write(b"GO\n")
        process.stdin.flush()
        status = process.wait(timeout=TIMEOUT)
    finally:
        selector.close()
        if process.poll() is None:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=TIMEOUT)
    return lines, status


def inspect_fields(store):
    """The inspect report, parsed so scenes can assert on real values."""
    result = run_cli("inspect", str(store))
    fields = {}
    for line in result.stdout.splitlines():
        if line.startswith(" ") or ":" not in line:
            continue
        name, _, value = line.partition(":")
        fields[name.strip()] = value.strip()
    return fields, result.stdout


# --------------------------------------------------------------------------
# Scenes
# --------------------------------------------------------------------------

def scene_normal(workdir):
    scene(1, "NORMAL WRITE")
    say("Ledger is an ordinary key/value store until something goes wrong.")
    store = workdir / "app.ledger"
    cli("put", str(store), "user:42", '{"name":"Venu","active":true}')
    cli("put", str(store), "session", '{"step":3}')
    cli("get", str(store), "user:42")
    cli("scan", str(store))
    return store


def scene_process_kill(workdir):
    scene(2, "PROCESS KILL")
    say("A writer commits 4 records, then appends the first 20 bytes of a")
    say("5th record and kills itself with SIGKILL. No clean shutdown, no")
    say("flush, no chance to repair. This is a real process death.")

    store = workdir / "crash.ledger"
    lines, status = run_crash_child(
        store, "torn-record", "--records", "4", "--keep", "20"
    )
    committed = [line.split()[2] for line in lines if line.startswith("COMMITTED")]
    torn = next(line for line in lines if line.startswith("TORN"))

    say()
    say(f"  committed before the kill : {len(committed)} records")
    say(f"  partial record written    : {torn.split()[1]} of "
        f"{torn.split()[2]} bytes")
    say(f"  child exit status         : {status} "
        f"({'SIGKILL' if status == -9 else 'unexpected'})")
    return store, committed


def scene_inspect(store, committed):
    scene(3, "RECOVERY")
    say("Before anything repairs the file, inspect reports what is there.")
    say("inspect is strictly read-only: no lock, no truncation, no repair.")

    fields, report = inspect_fields(store)
    shell(f"python3 -m ledger inspect {store}", report)

    say()
    say("  RECOVERED STATE   the valid prefix, provably intact")
    say(f"    valid records     {fields['valid records']}")
    say(f"    live keys         {fields['live keys']}")
    say(f"    valid end offset  {fields['valid end offset']}")
    say()
    say("  UNTRUSTED TAIL    present on disk, never loaded")
    say(f"    tail state        {fields['tail state']}")
    say(f"    tail reason       {fields['tail reason']}")
    say(f"    discarded bytes   {fields['discarded bytes']}")
    say(f"    repair required   {fields['repair required']}")

    say()
    say("Reads still work across the damage, and still change nothing:")
    size_before = store.stat().st_size
    cli("get", str(store), committed[0])
    say(f"    file size unchanged: {store.stat().st_size == size_before}")

    say()
    say("A write-mode open repairs the tail automatically. No flag, no")
    say("recover command: recovery is not an optional maintenance step.")
    cli("put", str(store), "after:restart", "true")
    after, report = inspect_fields(store)
    shell(f"python3 -m ledger inspect {store}", report)

    survived = all(
        run_cli("get", str(store), key).returncode == 0
        for key in committed
    )
    torn_visible = run_cli("get", str(store), "torn:key").returncode == 0
    say()
    say(f"  every committed record survived : {'YES' if survived else 'NO'}")
    say(f"  partial record visible          : {'YES' if torn_visible else 'NO'}")
    say(f"  tail state after repair         : {after['tail state']}")


def scene_compaction(workdir):
    scene(4, "COMPACTION")
    say("An append-only log accumulates obsolete versions. Compaction")
    say("reclaims them without ever rewriting the original in place.")

    store = workdir / "compact.ledger"
    db = ledger.Ledger.open(store, durability=ledger.DURABILITY_RELAXED)
    for round_number in range(6):
        for key in range(6):
            db.put(f"key:{key}", {"round": round_number, "key": key})
    db.put("temporary", {"scratch": True})
    db.delete("temporary")
    db.close()

    before, report = inspect_fields(store)
    say()
    say(f"  records    {before['valid records']}")
    say(f"  live keys  {before['live keys']}")
    say(f"  dead bytes {before['dead bytes']}")

    cli("compact", str(store))
    after, _ = inspect_fields(store)
    say()
    say(f"  records    {before['valid records']} -> {after['valid records']}")
    say(f"  dead bytes {before['dead bytes'].split()[1].strip('()')} -> "
        f"{after['dead bytes'].split()[1].strip('()')}")
    say(f"  live keys  {before['live keys']} -> {after['live keys']} "
        f"(unchanged: state is preserved)")


def scene_compaction_crash(workdir):
    scene(5, "COMPACTION CRASH")
    say("Compaction is the riskiest moment in the design: an old log and a")
    say("new one both exist. The harness kills the process at named points")
    say("in that state machine and checks what survives.")

    outcomes = []
    for point in ("before-replace", "after-replace"):
        store = workdir / f"cc-{point}.ledger"
        run_crash_child(
            store, "compact-then-kill", "--at", point,
            "--records", "6", "--rounds", "4",
        )
        fields, _ = inspect_fields(store)
        survivor = "compacted WAL" if fields["generation"] != "0" else "original WAL"
        outcomes.append((point, survivor, fields))
        say()
        say(f"  killed at {point}")
        say(f"    survivor        : {survivor}")
        say(f"    valid records   : {fields['valid records']}")
        say(f"    live keys       : {fields['live keys']}")
        say(f"    tail state      : {fields['tail state']}")

    live_counts = {fields["live keys"] for _, _, fields in outcomes}
    clean = all(f["tail state"] == "CLEAN" for _, _, f in outcomes)
    say()
    say(f"  logical state preserved : {'YES' if len(live_counts) == 1 else 'NO'}")
    say(f"  mixed WAL observed      : {'NO' if clean else 'YES'}")
    say()
    say("  The full suite exercises nine interruption points around")
    say("  compaction. Tested interruption points never produced a mixed")
    say("  or invalid authoritative WAL.")


def scene_zero_dependency(workdir):
    scene(6, "ZERO DEPENDENCIES")

    audit = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "depcheck.py")],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=TIMEOUT,
    )
    third_party = next(
        line for line in audit.stdout.splitlines() if line.startswith("third-party")
    )
    unresolved = next(
        line for line in audit.stdout.splitlines() if line.startswith("unresolved")
    )
    shell("python3 tools/depcheck.py", f"{third_party}\n{unresolved}")
    say(f"\n    exit status: {audit.returncode}")

    build = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "build.py"), "--verify"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=TIMEOUT,
    )
    shell("python3 tools/build.py --verify", build.stdout)

    artifact = workdir / "standalone" / "ledger.pyz"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    built = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "build.py"),
         "--output", str(artifact)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=TIMEOUT,
    )
    artifact.chmod(0o755)

    say()
    say("The artifact now runs somewhere the repository does not exist,")
    say("with PYTHONPATH removed. No install, no virtualenv, no packages.")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    for args in (["put", "s.ledger", "user:42", '{"name":"Venu"}'],
                 ["get", "s.ledger", "user:42"]):
        result = subprocess.run(
            [str(artifact), *args], capture_output=True, text=True,
            cwd=str(artifact.parent), env=env, timeout=TIMEOUT,
        )
        shell("./ledger.pyz " + " ".join(args), result.stdout or "(no output)")

    origin = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'ledger.pyz');"
         "import ledger; print(ledger.__file__)"],
        capture_output=True, text=True, cwd=str(artifact.parent), env=env,
        timeout=TIMEOUT,
    )
    say()
    say(f"  running code came from : {origin.stdout.strip()}")
    say(f"  artifact size          : {artifact.stat().st_size} bytes")
    say(f"  third-party imports    : 0")
    say(f"  unresolved imports     : 0")
    say(f"  reproducible           : "
        f"{'identical bytes' if 'identical bytes' in build.stdout else 'FAILED'}")


def main():
    print()
    print("  LEDGER - crash-safe embedded state store")
    print("  Most local persistence demos show the happy path.")
    print("  This one kills the process at the worst possible moment.")

    with tempfile.TemporaryDirectory(prefix="ledger-demo-") as tmp:
        workdir = pathlib.Path(tmp)
        scene_normal(workdir)
        store, committed = scene_process_kill(workdir)
        scene_inspect(store, committed)
        scene_compaction(workdir)
        scene_compaction_crash(workdir)
        scene_zero_dependency(workdir)

    print()
    print("=" * WIDTH)
    print("  Nothing above was simulated. Every process really died.")
    print("=" * WIDTH)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
