# Ledger

**A crash-safe embedded state store for local-first applications.**

Local application state that survives crashes, with zero runtime
dependencies.

## The problem

Most local application state is persisted like this:

```python
json.dump(state, open("state.json", "w"))
```

That line destroys everything you had. `open(..., "w")` truncates the file
*first*, then rewrites it. If the process dies between those two moments — a
`SIGKILL`, an OOM kill, a laptop lid closing — the file on disk is empty or
half-written, and the application restarts with nothing.

Ledger replaces that pattern. It appends to a checksummed write-ahead log and
never rewrites in place, so the worst case is losing the single write that was
in flight.

**What Ledger is not.** It is not a replacement for SQLite, PostgreSQL or
Redis, not a distributed database, not a sync engine. The defensible
comparison is narrow: it replaces the fragile `diskcache` / `shelve` /
hand-rolled JSON-file pattern when you need crash-safe local persistence.

## Why Ledger

Each of these exists to protect one property, not to lengthen a feature list.

- **Append-only WAL** — no committed byte is ever rewritten, so a crash can
  only ever damage the tail.
- **Checksummed records** — two CRC-32s per record, one over the header and
  one over the payload. The header checksum is verified *before* any length
  field is trusted, so a corrupt length can never size an allocation.
- **Conservative recovery** — the scan stops at the first invalid record and
  never searches forward. Past a damaged region, a real record cannot be told
  from stale bytes in a reused block, and resurrecting one is how a store
  silently returns a value overwritten long ago.
- **Torn vs corrupt classification** — "the write did not finish" and "these
  bytes changed under us" are different events and are reported differently,
  even though both truncate to the same safe boundary.
- **Atomic compaction** — the old log is never rewritten in place; a validated
  replacement is swapped in with `os.replace`.
- **Sidecar writer locking** — the lock lives beside the data file, because
  compaction replaces the data file's inode and a lock held on it would stop
  excluding anyone at exactly the wrong moment.
- **Strict / relaxed durability** — one honest knob. Strict fsyncs before each
  call returns; relaxed does not, and says so.
- **Zero third-party runtime dependencies** — mechanically enforced, not
  asserted.
- **Deterministic standalone artifact** — one file, byte-reproducible, no
  install.

## Quick start

No installation. Python 3.10+ on Linux or macOS.

```bash
python3 -m ledger put state.ledger user:42 '{"name":"Venu","active":true}'
python3 -m ledger get state.ledger user:42
python3 -m ledger scan state.ledger
python3 -m ledger inspect state.ledger
python3 -m ledger compact state.ledger
```

`VALUE` is JSON. Reads (`get`, `scan`, `inspect`) open read-only and take no
lock, so they never block on a running application.

As a library:

```python
from ledger import Ledger

with Ledger.open("state.ledger") as db:
    db.put("user:42", {"name": "Venu"})
    db.get("user:42")           # -> {'name': 'Venu'}
    db.delete("user:42")        # -> True
    list(db.scan(prefix="user:"))
```

Or as the standalone artifact, with no repository present:

```bash
python3 tools/build.py          # -> dist/ledger.pyz
./dist/ledger.pyz get state.ledger user:42
```

## Crash recovery

This is the point of the project.

```
valid prefix  +  invalid/torn tail  =  recover only what can be proven valid
```

A crash leaves a partial record at the end of the file. Recovery scans
forward, validates framing, lengths and both checksums, and stops at the first
record that fails. Everything before that is provably intact and is recovered;
everything after is discarded. A write-mode open then truncates to that
boundary automatically — recovery is not an optional maintenance step, and
there is deliberately no `ledger recover` command.

Real output from `python3 demo/crash_recovery.py`, where a child process
committed four records, appended 20 bytes of a fifth, and killed itself:

```
  committed before the kill : 4 records
  partial record written    : 20 of 49 bytes
  child exit status         : -9 (SIGKILL)

  $ python3 -m ledger inspect crash.ledger
    file size:        380 B (380 bytes)
    valid records:    4
    live keys:        4
    valid end offset: 360
    tail state:       TORN
    tail reason:      short_header
    discarded bytes:  20
    repair required:  yes

    forensic scan beyond the damage (UNTRUSTED / NOT RECOVERED):
      record markers found:   1
      headers that decode:    0
```

`inspect` is strictly read-only — no lock, no truncation, no repair — so it
can be run safely on a damaged file. Where the tail is damaged it also scans
past it and reports what conservative recovery is choosing to discard, clearly
labelled `UNTRUSTED / NOT RECOVERED`. That reporting can never affect the
recovered state.

Run it yourself:

```bash
python3 demo/crash_recovery.py     # six scenes, ~1.5 seconds
```

## Compaction

An append-only log accumulates obsolete versions and tombstones. Compaction
reclaims them, and the sequence is what makes it safe:

1. The original log is **never** rewritten in place and never truncated.
2. A complete replacement is written **beside it, in the same directory** —
   `os.replace` is atomic only within one filesystem, and a temp file on
   another mount would degrade into copy-then-delete with a window where
   neither file is whole.
3. The replacement is replayed back and validated against the live index
   before anything is swapped.
4. `fsync` on the replacement.
5. `os.replace` — a single kernel operation.
6. `fsync` on the parent directory, because a rename is a directory operation
   that fsyncing the file would not persist.

Sequence numbers restart at 1 in the new file. This is forced, not chosen:
recovery requires strict `+1` continuity, and compaction drops records, so
preserving the originals would leave gaps that recovery would correctly call
corruption.

Readers holding the old inode keep serving their own coherent snapshot until
they reopen. A crash before step 5 recovers the original log; after it, the
compacted one.

## Zero dependencies

```
third-party imports: 0
unresolved imports:  0
```

`ledger.py` imports ten standard-library modules and nothing else:
`__future__`, `argparse`, `dataclasses`, `fcntl`, `json`, `os`, `pathlib`,
`struct`, `sys`, `zlib`.

Three independent proofs, all runnable:

```bash
python3 tools/depcheck.py                          # import inventory, exit 1 on violation
python3 -E -s -S -m unittest discover -s tests     # site-packages off sys.path entirely
python3 -m unittest discover -s tests              # the audit is itself a test
```

The AST audit reads **source**, not the environment: `pip freeze` describes a
machine, whereas the imports in these files describe the project. It is
implemented twice, independently, and a test compares the two verdicts — a
tool agreeing with itself is not evidence.

See [STDLIB.md](STDLIB.md) for the substitution log: twelve subsystems, what
each could have used instead, and what each choice cost.

## Reproducible artifact

```bash
python3 tools/build.py --verify
```

```
build 1: dbc5b0dac7b7314d8eec9da8f247f0d7d8bd3bbf3b9a130fefaca8d74454925e  (14973 bytes)
build 2: dbc5b0dac7b7314d8eec9da8f247f0d7d8bd3bbf3b9a130fefaca8d74454925e  (14973 bytes)
reproducible: identical bytes
```

**`dist/ledger.pyz` — 14,973 bytes**

**SHA-256: `dbc5b0dac7b7314d8eec9da8f247f0d7d8bd3bbf3b9a130fefaca8d74454925e`**

Verified identical when built by CPython 3.10, 3.11, 3.12 and 3.13, from
different directories, and under different umasks. Member order, timestamps,
permissions, `create_system` and compression level are all pinned explicitly;
`SOURCE_DATE_EPOCH` is honoured when set.

## Testing

365 tests, but the count is not the point — what was tested is:

| Area | What was done |
| --- | --- |
| Truncation | Every byte offset of five fixtures; boundary neighbourhoods of two large ones |
| Bit corruption | Every single-bit flip across bounded fixtures, expectations from an independent oracle |
| Seeded corruption | Multi-bit, byte substitution, range damage, truncation+corruption — seed `20260828` |
| Total hostile inputs | **12,734** recovery sub-cases |
| Mutation testing | 12 defects injected into the recovery reader; **12/12 killed** |
| Crash recovery | Real subprocesses, real `SIGKILL`, pipe handshake — no sleeps, no timing |
| Compaction crashes | Nine named interruption points around the state machine |
| Cross-version | CPython 3.10, 3.11, 3.12, 3.13, plus `-O` |
| Isolated interpreter | Full suite under `-E -s -S` |
| Standalone artifact | Full workflow with the repository absent |

The recovery matrices compute their expected results **arithmetically from the
format specification**, never by calling the reader, so the tests cannot pass
by agreeing with the code they test.

```bash
python3 -m unittest discover -s tests
```

## Architecture

```
        CLI / Python API
               |
               v
         Ledger Engine
               |
          +----+----+
          |         |
        Index      WAL
          |         |
          +----+----+
               |
           Recovery
```

The index maps each key to its **serialized value bytes**, not a decoded
object, so a caller mutating a returned value cannot change the store. The WAL
is the single source of truth; the index is never updated until the record is
durable. `DESIGN.md` specifies the byte layout completely.

## Limitations

Stated plainly, because a storage project that is vague here is not
trustworthy.

- **POSIX only.** Locking uses `fcntl.flock`. **Windows is not supported**, and
  no untested Windows path is claimed. `flock` is advisory and unreliable over
  NFS.
- **Single writer.** One writer process at a time, enforced by the lock.
  Multiple readers are fine. No multi-process write coordination.
- **Everything live fits in memory.** The index holds all live values, and
  `open` reads the log to replay it. Sized for application state, not
  larger-than-RAM datasets.
- **No multi-key transactions.** Each record is its own commit unit; a crash
  during a sequence of writes leaves a prefix applied.
- **CRC-32 is not authentication.** It detects accidental corruption. It is not
  cryptographic integrity, not tamper resistance, not a MAC — anyone who can
  write to the file can recompute it.
- **No encryption.** Keys and values are stored as readable JSON.
- **`delete` is not erasure.** Bytes remain until compaction, and then may
  persist in free space and backups.
- **JSON's type model.** Tuples return as lists, non-string dict keys as
  strings; `set`, `bytes`, `datetime`, `NaN` and `Infinity` are rejected at
  `put` rather than silently mangled.
- **Durability is the filesystem's.** `fsync` is only as good as the device
  honouring it. Ledger guarantees the *recovery behaviour* — that the valid
  record prefix is what comes back — not the physics.
- **Not a relational database.** No SQL, no joins, no secondary indexes.

## Documentation

| File | Contents |
| --- | --- |
| [DESIGN.md](DESIGN.md) | Full engineering design: format, recovery algorithm, semantics, 25 sections |
| [STDLIB.md](STDLIB.md) | Standard-library substitution log and trade-offs |

## Build and verification

Every command below exists and passes.

```bash
python3 -m unittest discover -s tests              # full suite
python3 -E -s -S -m unittest discover -s tests     # isolated interpreter
python3 tools/depcheck.py                          # dependency inventory
python3 tools/build.py                             # -> dist/ledger.pyz
python3 tools/build.py --verify                    # reproducibility check
python3 demo/crash_recovery.py                     # crash recovery demo
```
