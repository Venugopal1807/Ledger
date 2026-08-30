# STDLIB Log

An engineering record of where Ledger uses the Python standard library in
place of a third-party package, and what that cost.

The argument throughout is narrow: **for Ledger's scope and constraints, the
standard library was sufficient.** That is not a claim these choices are
better in general — several would be wrong for a larger system, and where
that is true this document says so.

`ledger.py` is 1,442 lines and imports ten standard-library modules:
`__future__`, `argparse`, `dataclasses`, `fcntl`, `json`, `os`, `pathlib`,
`struct`, `sys`, `zlib`. Nothing else. The whole project — production, tests
and tools — uses 26 standard-library modules and no others.

## Why zero dependencies

Ledger is a crash-safe embedded state store. Its one claim is that committed
data survives a process dying mid-write. Every dependency is something that
can break that claim invisibly: a package that buffers where we expected a
write to land, a version bump that changes fsync behaviour, an install step
that fails on the machine where recovery matters.

A store whose value is *predictability under failure* should be readable end
to end. The constraint matches what the product is for.

---

## Substitution log

### Binary format — `struct`

**Needed:** a fixed 32-byte file header and 32-byte record header, packed
little-endian, with exact control over field widths.

**Used:** `struct.Struct("<8sHHI12s")` and `struct.Struct("<4sBBHQIII")`,
compiled once at import (`ledger.py`).

**Instead of:** a declarative binary-parsing library such as `construct` or
`kaitai_struct`.

**Why sufficient:** Ledger defines its own format and never parses anyone
else's. Those libraries earn their place when you must *describe* formats you
did not design, or generate parsers across languages. Here the format is two
fixed layouts specified in `DESIGN.md`, and `struct` expresses them in three
lines. A schema layer would sit between the design document and the bytes,
which is precisely where a storage bug hides.

**Given up:** no declarative schema, no generated documentation, no
cross-language parser. The format strings and the design document must be
kept in agreement by hand — which is why `tests/test_format.py` asserts the
layout against hard-coded hex rather than re-deriving it from the module's
own format strings.

### Integrity — `zlib.crc32`

**Needed:** detection of accidental corruption in record headers and
payloads, cheap enough to run on every record during recovery.

**Used:** `zlib.crc32`, twice per record — one checksum over the header
fields, one over the key and value.

**Instead of:** a checksum package such as `crcmod` or `fastcrc`, or a
cryptographic hash.

**Why sufficient:** `zlib` is a C implementation already in the interpreter,
and CRC-32 detects every single-bit error and every burst error up to 32
bits. `tests/test_matrices.py` sweeps every single-bit flip across bounded
fixtures and confirms each one is caught.

**What CRC-32 is not.** It is error detection, not authentication. It is
**not** cryptographic integrity, **not** tamper resistance, and **not** a
MAC. Anyone who can modify the file can recompute the checksum, and Ledger
will accept the result. Detecting deliberate modification needs a keyed MAC
and key management, which is out of scope.

That is sufficient because Ledger's objective is recovery from *crashes and
corruption*, not from an adversary with write access: the threat model is a
torn write, a bad sector, a truncated file. SHA-256 would cost eight times the
bytes while still not resisting an attacker, and would imply a security
property the project does not provide.

### Locking — `fcntl.flock`

**Needed:** stop a second process writing to a store that already has a
writer, and fail fast rather than block.

**Used:** `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on a sidecar `<path>.lock`.

**Instead of:** `filelock` or `portalocker`, both of which exist largely to
paper over the Windows/POSIX difference.

**Why sufficient:** the lock is one call. `flock` was chosen over
`fcntl.lockf` deliberately: `lockf` locks are released when *any* descriptor
on the file closes, a known footgun in libraries. The sidecar matters too —
compaction replaces the data file's inode, so a lock held on the data file
would stop excluding anyone at the moment of the swap.

**Given up — a real limitation:** `fcntl` does not exist on Windows, so
**the current locking implementation is POSIX-only, and Ledger does not
support Windows.** This was accepted rather than worked around. Windows needs
a different primitive (`msvcrt.locking`), different semantics for replacing an
open file, and a machine to test on that we do not have. An untested Windows
path would be worse than an honest omission: it would claim a durability
guarantee nobody had verified. `flock` is also advisory — it constrains
cooperating processes, not `echo >> store.ledger` — and unreliable over NFS.

### Atomic replacement — `os.replace`

**Needed:** swap a freshly-written compacted log in for the old one with no
window where the store is unreadable.

**Used:** `os.replace(temp, path)` followed by an `fsync` of the parent
directory, because a rename is a directory operation that fsyncing the file
would not persist.

**Instead of:** `atomicwrites`, or a hand-rolled copy-and-delete.

**Why sufficient:** `os.replace` is atomic within a single filesystem, which
is why compaction writes its temp file in the *same directory* as the log
rather than `/tmp`. A temp file on another mount would degrade the rename
into copy-then-delete, with a window where neither file is whole.

**Evidence:** `tests/test_crash.py` kills a process at nine named points
around compaction. The survivor flips exactly at `os.replace` and nowhere
else — the six points before it recover the original log, the three after
recover the compacted one. Nothing in between is ever observed, because the
rename is a single kernel operation that userspace cannot catch mid-flight.

**Given up:** the guarantee is only as good as the filesystem beneath it, and
the same-filesystem constraint is a real restriction on where the temp file
may live.

### Values — `json`

**Needed:** serialize application state to bytes, and read it back after a
crash.

**Used:** `json.dumps(value, separators=(",",":"), sort_keys=True,
ensure_ascii=False, allow_nan=False)`.

**Instead of:** `pickle` (standard library but a different trade), or
`msgpack`/`orjson`/`cbor2`.

**Why JSON rather than pickle.** Not because pickle is universally unsafe —
it is a reasonable choice for trusted, internal, same-version data. It is
*unnecessary* here, and its costs land badly on this product:

- **Interoperability.** A Ledger file can be read by anything that reads
  JSON. Pickle is Python-only, and version-sensitive.
- **Inspectability.** During the crash demo the file is examined with `cat`
  and a hex dump. Values are readable text. That is a feature of the
  debugging story, not an accident.
- **Explicit format.** `DESIGN.md` specifies the byte layout completely.
  Pickle's opcode stream would have to be treated as opaque.
- **No requirement for arbitrary objects.** Ledger stores application state:
  documents, counters, cursors. It has no need to reconstruct live Python
  objects, so it gains nothing from a format that can.

`allow_nan=False` is the deliberate part. Python's `json` emits bare `NaN`,
`Infinity` and `-Infinity` tokens, which are **not** JSON and which other
parsers reject. A file that is meant to be inspectable must not contain bytes
only Python can read back, so those three are refused at `put` with a
`ValueError`: anything Ledger persists successfully is valid, interoperable
JSON.

`sort_keys=True` makes a given value's encoding byte-stable, which is what
lets compaction be deterministic and lets tests assert on exact bytes.

**Given up:** a narrower type model. Tuples come back as lists, non-string
dict keys come back as strings, and `set`, `bytes` and `datetime` are
rejected outright rather than silently mangled. JSON is also slower than a
binary codec. For application state — small documents, far more writes than
reads of any one key — that trade is the right way round, and the rejections
are loud rather than quiet.

### CLI — `argparse`

**Needed:** six subcommands, each taking a file and one or two arguments,
with stable exit codes.

**Used:** `argparse` with subparsers, plus a small `ArgumentParser` subclass
that remaps argparse's usage exit from 2 to 1, because 2 is this CLI's code
for a corrupt store.

**Instead of:** `click`, `typer`, or `rich` for output.

**Why sufficient:** the CLI is a thin skin over the public API — each command
opens a store, calls one method, prints the result. `click` and `typer` pay
off across dozens of commands with shared options; here they would be a
dependency for six subparsers.

**Given up:** manual plumbing. No shell completion, no colour, no table
rendering. Output is plain `key<TAB>json` because that is what pipes into
other tools, and the exit-code remapping is something a framework would have
handled.

### Testing — `unittest`

**Needed:** a test runner, in a project that must have zero third-party test
dependencies.

**Used:** `unittest`, run as `python3 -m unittest discover -s tests`.
365 tests.

**Instead of:** `pytest`.

**Why sufficient:** `unittest` is in the interpreter, so the suite runs
anywhere Python runs, including under `-E -s -S` with `site-packages` off the
path entirely. That last property is what makes the zero-dependency proof
possible at all — a pytest suite could not run in an interpreter that cannot
see `site-packages`.

**Given up — genuinely.** `pytest` has the better ergonomics: `assert`
rewriting with useful failure output, a more flexible fixture model,
parametrisation that reads better than `subTest`, and a large plugin
ecosystem. Several test files here would be shorter under it. The trade was
accepted because running with `site-packages` removed is worth more to this
project than concise fixtures.

### Crash testing — `subprocess` + `signal`

**Needed:** kill a writer mid-operation, for real, and prove the store
recovers.

**Used:** `subprocess.Popen` with pipes, `selectors` for bounded reads, and
`os.kill(os.getpid(), signal.SIGKILL)` in `tests/crash_child.py`.

**Instead of:** `pexpect`, or a mocking library that simulates failure
in-process.

**Why sufficient:** the child kills *itself* with `SIGKILL` — uncatchable, no
`finally` blocks, no `atexit`, no buffer flush. A mock cannot produce that; it
produces a story about it. Synchronisation is a two-way pipe handshake, so
nothing depends on timing.

Crucially, **`ledger.py` contains no test hook** — no environment-variable
crash point, no injectable write function. The seam lives entirely in the
child script, so the code path under test is the code path that ships.

**Given up:** no pseudo-terminal handling, and the parent-side driver
(bounded line reads, watchdogs, orphan cleanup) is about 90 lines we wrote
ourselves. `pexpect` would have supplied some of that.

### Dependency auditing — `ast` + `sys.stdlib_module_names`

**Needed:** prove mechanically that no third-party import exists.

**Used:** `ast.parse` plus `ast.walk` to find every import including ones
inside functions; `sys.stdlib_module_names` to identify the standard library;
`importlib.util.find_spec` only to separate "exists somewhere" (third-party)
from "exists nowhere" (unresolved). Implemented twice, independently:
`tests/test_no_dependencies.py` and `tools/depcheck.py`.

**Instead of:** `pipdeptree`, `deptry`, `pip-audit`.

**Why sufficient:** those tools answer a different question. They describe
what is *installed*; only the source says what the project *imports*, and the
source is what ships.

**Given up:** no dependency-graph visualisation, no CVE database, no version
resolution — all irrelevant with no dependencies to resolve. The two
implementations are cross-checked by a test, since a tool agreeing with itself
is not evidence.

### Deterministic build — `zipfile`

**Needed:** `dist/ledger.pyz`, byte-identical from any machine, directory or
time.

**Used:** `zipfile` directly, in `tools/build.py`.

**Instead of:** `zipapp.create_archive` (standard library, but insufficient),
or `shiv`, `pex`, `PyInstaller`.

**Why the low-level module.** A zip is *not* reproducible by default, and
`zipapp.create_archive` does not expose the metadata that has to be pinned:
member order (sorted, not filesystem order), timestamps (`SOURCE_DATE_EPOCH`
or the 1980 zip epoch, never the wall clock), `external_attr` (constant, so a
contributor's umask cannot leak in), `create_system` (pinned to Unix, which
`zipfile` otherwise takes from the building platform), and the compression
level. Writing the archive by hand is what makes each of those an explicit,
reviewable decision.

**Why a packaging tool was unnecessary:** the artifact is two files — a
generated `__main__.py` and `ledger.py`. `shiv` and `pex` exist to vendor
dependency trees, and there are none. `PyInstaller` bundles an interpreter,
which is a different product.

**Given up:** no wheel, no `pip install ledger`, no vendoring. The
determinism rules are ours to maintain and could break silently — which is why
`tools/build.py --verify` builds twice and, on a mismatch, names the member
and field that differ rather than only reporting different hashes.

### Memory measurement — `tracemalloc`

**Needed:** answer two design questions with evidence rather than intuition —
whether the bytes-based reader should become streaming, and whether
compaction doubles memory.

**Used:** `tracemalloc` peak measurement, plus `resource.getrusage` for RSS.

**Instead of:** `memory_profiler`, `pympler`, `guppy`.

**Why sufficient:** the question was coarse — does peak scale with the log or
with live data? `tracemalloc` answers it directly. Compaction was measured to
peak with live data rather than log size, so the reader stayed bytes-based and
`DESIGN.md` records why.

**Given up:** no line-level attribution, no object-graph analysis. Enough for
a yes/no design decision, not enough to optimise a hot loop.

### Adversarial testing — deterministic matrices

**Needed:** exhaustive truncation and bit-flip coverage over the recovery
reader, reproducibly.

**Used:** explicit matrices in `tests/test_matrices.py` — every byte offset,
every single-bit flip over bounded fixtures — plus seeded multi-bit damage
via `random.Random(20260828)`. 12,734 sub-cases in about one second.

**Instead of:** `hypothesis`.

**Why sufficient:** for a torn tail, the input space *is* enumerable. Every
truncation length of a small log is a finite set, and so is every single-bit
flip. Enumerating them is stronger than sampling them, and it is exactly
reproducible: the same failure appears on every machine, every run. Expected
results come from an independent oracle that computes boundaries
arithmetically and never calls the reader, so the tests cannot pass by
agreeing with the code they test.

**Given up — and this should not be overstated.** These matrices do **not**
replace property-based testing in general. `hypothesis` shrinks failing cases
automatically, explores spaces too large to enumerate, and finds shapes a
human did not think to write down. Where our space is unbounded — multi-bit
corruption — we fall back to seeded sampling, which is the weaker technique.
What is claimed is narrower: for *this* bounded space, exhaustive enumeration
was both possible and better.

---

## What we deliberately gave up

| Choice | Cost accepted |
| --- | --- |
| `fcntl.flock` | POSIX only. No Windows support, and advisory rather than enforced. |
| `unittest` | Less ergonomic than pytest: rigid fixtures, verbose assertions, no plugin ecosystem. |
| `argparse` | Manual CLI plumbing. No completion, no colour, no table output. |
| `zipfile` | Reproducible ZIP metadata is ours to maintain, not a tool's. |
| `zlib.crc32` | Error detection only. Not cryptographic, not tamper-resistant, not a MAC. |
| `json` | Narrower type model. Tuples become lists, dict keys become strings, `set`/`bytes`/`datetime` rejected. Slower than binary. |
| `struct` | No declarative schema; layout and design document kept in step by hand. |
| Deterministic matrices | No automatic shrinking or unbounded exploration. |
| `tracemalloc` | Coarse measurement, no line-level attribution. |

---

## Verification

Every command below is part of the project and passes today.

**Run the suite under an interpreter that cannot see `site-packages`.**
`-S` removes `site-packages` from `sys.path` entirely, `-E` ignores
`PYTHONPATH`, `-s` ignores the user site directory. This is the strongest
proof, because it does not inspect the code — it removes the possibility:

```
python3 -E -s -S -m unittest discover -s tests
```

**Print the complete import inventory**, grouped into stdlib, project-local,
third-party and unresolved. Exit 0 only when the last two are empty:

```
python3 tools/depcheck.py
python3 tools/depcheck.py --json     # machine-readable
```

**Verify the build is reproducible** by building twice and comparing bytes:

```
python3 tools/build.py --verify
```

**Run the suite normally:**

```
python3 -m unittest discover -s tests
```

The dependency audit is also a test (`tests/test_no_dependencies.py`), so a
third-party import is a failing suite rather than a review finding.
