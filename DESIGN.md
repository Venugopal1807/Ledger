# Ledger — Engineering Design Document

**Status:** Phase 1 design. No implementation code exists yet.
**Version:** 1 (on-disk format `LEDGERv1`)
**Target:** Python 3.9+, POSIX (Linux, macOS), standard library only.

> Local application state that survives crashes, with zero runtime dependencies.

---

## 1. Product scope

Ledger is a crash-safe embedded key/value state store for local-first Python
applications. It is a library first and a CLI second. It is designed for the
category of data that applications currently persist by writing a JSON file and
hoping: user preferences, window layout, sync cursors, job queues, offline
edit buffers, partial download manifests, "resume where I left off" state.

The concrete problem it solves: **the naive `json.dump(state, open(path,"w"))`
pattern destroys all previous state if the process dies mid-write.** The file
is truncated at `open()` and then rewritten; a crash between those two points
leaves an empty or half-written file, and the application starts up with
nothing. Ledger replaces that pattern with an append-only, checksummed
write-ahead log whose worst case is losing the single in-flight write.

In scope:

- Durable `put` / `get` / `delete` of string keys to JSON-representable values.
- Ordered `scan` over keys, with optional prefix filter.
- Automatic, deterministic recovery on open, including truncation of an
  incomplete or corrupt log tail.
- Log compaction that reclaims space from overwritten and deleted keys.
- A read-only `inspect` diagnostic that reports the physical state of the log
  without modifying it.
- A small `argparse` CLI for operating on a store from the shell.
- Datasets that fit comfortably in process memory (target: up to a few hundred
  megabytes of live state).

Positioning: Ledger sits between ad-hoc JSON persistence, `shelve`, and
diskcache-style local persistence. It is **not** a replacement for SQLite,
PostgreSQL, Redis, or an ORM, and this document makes no such claim.

Design priority order, applied whenever two goals conflict:

1. Never silently lose acknowledged data.
2. Deterministic, explainable recovery.
3. Small, auditable implementation.
4. Small public API.
5. Performance.

## 2. Non-goals

Explicitly out of scope for this project. These are not "future work we ran out
of time for" — several are deliberately refused because they would make the
durability claims harder to prove.

| Non-goal | Reason |
| --- | --- |
| SQL, query planner, secondary indexes | Out of category; key lookup and prefix scan are enough for application state. |
| Multi-key transactions / atomic batches | We can only prove single-record atomicity. Promising more without proof is the failure mode we are trying to avoid. |
| Multi-process concurrent writers | Requires either a lock protocol we cannot test properly in 72h, or a shared-memory index. One writer is provable. |
| Networking, server mode, RPC | Embedded store. A server would be a different product. |
| Replication, sync, CRDTs | Explicitly excluded by the track rules and by scope. |
| Encryption, authentication, tamper-proofing | CRC32 detects accidental damage, not attackers. See §23. |
| ORM / schema / migrations | Values are JSON documents; the application owns their shape. |
| Datasets larger than RAM | The in-memory index holds all live values. See §13 for the rejected alternative. |
| TTL, eviction, cache policies | This is a state store, not a cache. |
| Windows support | Requires a different locking primitive and different rename semantics, with no way to test it here. Documented as unsupported rather than claimed and broken. See §22. |
| Network filesystems (NFS, SMB) | `fcntl.flock` and `fsync` semantics are unreliable there. Documented as unsupported. |
| Background threads, async API | Every operation is synchronous and caller-driven. No hidden concurrency. |
| Pluggable storage backends / codecs | One format, fully specified. Abstractions would obscure the thing being demonstrated. |
| SQLite as the storage engine | Excluded by the hackathon rules and by the point of the exercise. |

## 3. Public API

The entire library surface is one class, one factory method, eight operations,
and one exception hierarchy. Anything not listed here is private.

```python
from ledger import Ledger

db = Ledger.open("state.ledger")           # opens, recovers, locks
db.put("user:42", {"name": "Venu"})        # durable on return
value = db.get("user:42")                  # -> {"name": "Venu"}
db.get("missing")                          # -> None
db.get("missing", default={})              # -> {}
db.delete("user:42")                       # -> True if the key existed
for key, value in db.scan(prefix="user:"): # sorted by key
    ...
db.compact()                               # rewrite log with live records only
report = db.inspect()                      # read-only physical report
db.close()                                 # releases the lock

with Ledger.open("state.ledger") as db:    # context manager, closes on exit
    db.put("k", 1)
```

Signatures:

```python
@classmethod
def open(cls, path, *, mode="rw", durability="fsync", repair=True) -> "Ledger"

def put(self, key: str, value: JSONValue) -> None
def get(self, key: str, default=None) -> JSONValue
def delete(self, key: str) -> bool
def scan(self, prefix: str = "") -> Iterator[tuple[str, JSONValue]]
def compact(self) -> CompactionResult
def inspect(self) -> LogReport
def close(self) -> None

def __enter__(self) / __exit__(...)
def __len__(self)          # number of live keys
def __contains__(self, key: str)
```

`open` parameters — three, all keyword-only, all correctness-relevant:

- `mode`: `"rw"` (default) takes the writer lock and repairs the tail;
  `"r"` opens read-only, takes no lock, and never modifies the file.
- `durability`: `"fsync"` (default) calls `os.fsync` before each mutating call
  returns — survives process crash *and* power loss. `"os"` skips the fsync —
  survives process crash only, because the bytes are in the OS page cache. §9
  states exactly what each mode guarantees.
- `repair`: `True` (default) truncates a damaged tail on open. `False` raises
  `CorruptLogError` instead, for an operator who wants to look first.

There is deliberately no configuration for block size, cache size, sync
interval, codec, or compaction threshold. Every additional knob is a
combination we would have to test.

Types: keys are non-empty `str` (encoded UTF-8, ≤ 4 KiB). Values are anything
`json.dumps` accepts: `dict`, `list`, `str`, `int`, `float`, `bool`, `None`
(encoded ≤ 8 MiB). See §4 for the round-trip caveats this implies.

## 4. Storage format

A store is **one data file** plus **one lock sidecar**:

```
state.ledger          the log: 32-byte file header + append-only records
state.ledger.lock     zero-length; exists only to hold fcntl.flock (§16)
state.ledger.compact  transient, only during compaction (§14)
state.ledger.salvage.<n>  written only when a corrupt tail is discarded (§11)
```

The data file is:

```
+--------------------------+  offset 0
| file header (32 bytes)   |
+--------------------------+  offset 32
| record 0                 |
+--------------------------+
| record 1                 |
+--------------------------+
| ...                      |
+--------------------------+  <- valid prefix ends here
| (possibly a torn or      |
|  corrupt tail)           |
+--------------------------+  EOF
```

File header, little-endian, `struct` format `<8sHHI12sI`, 32 bytes total:

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 8 | `magic` | `b"LEDGERv1"` — ASCII, visible in a hex dump |
| 8 | 2 | `format_version` | `1` |
| 10 | 2 | `flags` | `0`; any other value is rejected |
| 12 | 4 | `generation` | incremented by each successful compaction |
| 16 | 12 | `reserved` | zeros, covered by the CRC |
| 28 | 4 | `header_crc` | `zlib.crc32` of bytes `[0:28]` |

The header is written once, at creation, and rewritten only by compaction
(which produces a whole new file). It is never mutated in place, so there is no
"update the header on every commit" write amplification and no window in which
the header disagrees with the records.

`generation` exists for diagnostics only: it lets `inspect` and the demo show
that a compaction actually happened. Nothing in recovery depends on it.

**Value encoding.** Values are stored as UTF-8 JSON produced with
`json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)`.
`sort_keys` makes the byte encoding of a given value deterministic, which
matters for reproducible compaction output and for byte-exact tests.

We chose JSON over `pickle` deliberately, and the tradeoff is real:

- **For JSON:** unpickling a corrupted or attacker-supplied log is arbitrary
  code execution. A store whose entire pitch is "this file may be damaged, and
  we will read it anyway" must not use a format that executes on read. JSON is
  also inspectable by `cat`, `strings`, and `jq` during the demo.
- **Against JSON:** it is slower than `pickle`, and it does not round-trip
  Python types faithfully. `tuple` returns as `list`, non-string `dict` keys
  return as strings, and `set`/`bytes`/`datetime` are rejected at `put` time
  with a `TypeError`. This is documented in the README, not hidden.

The `flags` field in the record header reserves room for an alternate value
codec later. We are not implementing one.

## 5. WAL record format

Every mutation is one self-describing record: a fixed 32-byte header followed
by the key bytes and then the value bytes.

```
+---------------------------+---------------+-------------------+
| header (32 bytes)         | key (key_len) | value (val_len)   |
+---------------------------+---------------+-------------------+
```

Header, little-endian, `struct` format `<4sBBHQIIII`, 32 bytes total:

| Offset | Size | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 4 | `magic` | `b"LGR\x1e"` — per-record sync marker |
| 4 | 1 | `version` | record version, `1` |
| 5 | 1 | `op` | `1` = PUT, `2` = DELETE. Anything else is invalid |
| 6 | 2 | `flags` | `0`; any other value is invalid |
| 8 | 8 | `seq` | u64, starts at 1, exactly +1 per record in this file |
| 16 | 4 | `key_len` | u32, `1 <= key_len <= 4096` |
| 20 | 4 | `val_len` | u32, `<= 8 MiB`; must be `0` when `op == DELETE` |
| 24 | 4 | `payload_crc` | `zlib.crc32(key_bytes + value_bytes)` |
| 28 | 4 | `header_crc` | `zlib.crc32(header_bytes[0:28])` |

Every field earns its place against a specific failure mode:

| Failure to detect | Detected by |
| --- | --- |
| Wrong file / garbage at this offset | `magic`, `header_crc` |
| Format drift after an upgrade | `version`, `format_version` |
| Invalid operation | `op` range check |
| Invalid / absurd lengths | `key_len`, `val_len` range checks, checked **after** `header_crc` |
| Bit rot in the header itself | `header_crc` |
| Bit rot in key or value | `payload_crc` |
| Incomplete write (torn record) | short read against `key_len + val_len` |
| A stale record surviving in reused disk blocks | `seq` continuity (`seq == previous + 1`) |
| Reserved bits used by a future writer | `flags == 0` |

Two CRCs rather than one is the single most important format decision. It is
what makes recovery able to distinguish *"the write did not finish"* from
*"these bytes were damaged"* (§11), and it means we never trust `key_len` or
`val_len` — and therefore never allocate a buffer sized by them — until the
header has been verified.

DELETE records carry the key and no value (`val_len == 0`). They are
tombstones, not header-only markers, because the key is what recovery needs.

## 6. Record framing

Framing is **fixed header + length-prefixed payload**, with no delimiters, no
escaping, and no padding. Record boundaries are computed arithmetically:

```
next_offset = offset + 32 + key_len + val_len
```

Consequences, all intentional:

- Reading the log is one forward pass. There is no index block, no footer, and
  nothing to seek back and patch after a commit.
- A record's own header is sufficient to find the next record. There is no
  global table that can disagree with the data.
- Because payloads are length-prefixed and never escaped, arbitrary bytes
  (including `b"LGR\x1e"`) can appear inside a value with no ambiguity for the
  normal reader.
- The per-record `magic` is therefore *not* required for framing during normal
  recovery. It exists as a cheap corruption tripwire, and it is what
  `inspect`'s resynchronisation scan (§17) searches for when a human wants to
  know what lies beyond a damaged region. Normal recovery never resynchronises
  — see §11 for why that restraint is deliberate.

Records are never rewritten, never reordered, and never partially updated. The
only mutations the file ever undergoes are: append at the end, and
`ftruncate` back to a validated boundary.

## 7. Checksum strategy

`zlib.crc32` (stdlib, C-implemented, no dependency, fast enough to be free
relative to an `fsync`).

- `header_crc` covers header bytes `[0:28]` — every header field except itself.
  Verified **before** any header field is used, including the lengths. This is
  the guard that prevents a corrupt `val_len` of `0xFFFFFFFF` from turning into
  a 4 GiB read.
- `payload_crc` covers `key_bytes + value_bytes` as a single contiguous CRC.
  Verified after the payload is fully read. The key and value are not
  checksummed separately: they are written and read as one unit, so a combined
  CRC costs one field and loses no diagnostic power.

Why CRC32 and not SHA-256 / BLAKE2 (both also stdlib):

- The threat model is accidental damage: torn writes, bit rot, truncated files,
  a partially flushed page. CRC32 detects all single-bit errors, all burst
  errors up to 32 bits, and ~99.99999998% of larger random corruptions. That is
  the right tool for this job.
- A cryptographic hash would cost 8× the bytes and materially more CPU while
  still not resisting an attacker, because the attacker can simply recompute
  it. Integrity against a *malicious* editor requires a keyed MAC and key
  management, which is out of scope (§23). Choosing a strong hash here would
  imply a security property we do not provide.

CRCs are computed over the exact bytes that are written, before the write, on
the assembled buffer. There is no path where a record is checksummed from one
copy of the data and written from another.

## 8. Write semantics

The write path for one `put` or `delete`:

1. Validate arguments (key type/length, value JSON-encodable, size limits).
   Any failure raises **before** anything touches the file.
2. Encode key and value to bytes.
3. Build the complete record — header plus payload — in memory as one
   `bytes` object. CRCs are computed here.
4. `os.write(fd, record)` in a single call, with the fd opened `O_APPEND`.
   Retry on short writes and on `EINTR`.
5. If `durability == "fsync"`, `os.fsync(fd)`.
6. Update the in-memory index.
7. Return.

Properties this ordering gives us:

- **The index never contains data the log does not.** The index is updated
  after the bytes are durable, never before. A crash at any point leaves the
  on-disk log as the single source of truth, and the next open reconstructs the
  index from it. There is no reconciliation step, because there is nothing to
  reconcile.
- **Existing bytes are never overwritten.** `O_APPEND` makes the kernel place
  every write at the current end of file. Combined with "recovery only ever
  truncates", the file is append-only in the strong sense: no committed byte is
  ever modified in place.
- **One record, one `write()` call.** This does not make the record atomic — a
  crash can tear a single `write()` at arbitrary byte granularity, and we
  assume it can (§22). It does mean we never leave an interleaved or
  out-of-order record, and it keeps the torn-tail shape simple: a prefix of one
  record, at the very end of the file.
- **Errors poison the handle.** If step 4 or 5 raises, the file may now end
  with a partial record. The handle is marked failed: it releases nothing, and
  every subsequent operation raises `WriteError`. The caller must `close()` and
  reopen; reopening runs the standard recovery path, which truncates the torn
  tail. We deliberately do *not* attempt an in-process self-heal (truncate back
  to the pre-write offset and continue), even though the pre-write offset is
  known: it would be a second repair path with its own failure modes, needing
  its own tests, to save a reopen in a rare case. One recovery path, fully
  tested, is worth more.

`delete` on a key that is not in the index writes nothing and returns `False`.
Tombstones are only written for keys that exist, so repeated deletes cannot
grow the log.

## 9. Commit semantics

**A mutation is committed when the call returns normally.** Everything else
follows from that sentence.

| Durability mode | Survives `SIGKILL` / process crash | Survives OS crash or power loss |
| --- | --- | --- |
| `"fsync"` (default) | Yes | Yes, to the extent the hardware honours `fsync` (§22) |
| `"os"` | Yes — bytes are in the kernel page cache, which outlives the process | No |

Precise guarantees:

- If `put` returns, a subsequent open of the store returns that value (or a
  later value for the same key), for the failure classes in the table above.
- If `put` raises, the write may or may not be present after recovery. The
  caller must treat the outcome as unknown and re-read after reopening. This is
  the only ambiguity in the model and it is stated rather than papered over.
- If the process dies *during* `put`, the record is either fully recovered or
  entirely discarded. There is no state in which half a value becomes visible,
  because a record is applied to the index only after both CRCs verify.
- Records commit in `seq` order and are recovered in `seq` order. The last
  writer of a key wins.
- Each record is its own commit unit. There are no multi-record transactions,
  so a crash during a sequence of `put` calls leaves a **prefix** of them
  applied. Applications that need "all or nothing" across several keys must put
  them in one value.
- Visibility: within a process, an update is visible to `get` after `put`
  returns. A separate read-only handle opened earlier sees its own snapshot
  (§15).

`close()` is not a commit point — every mutation has already committed. It
flushes nothing that matters, closes the fd, and releases the lock. A process
that dies without calling `close()` loses no acknowledged data; the only cost
is that the advisory lock is released by the OS instead of by us.

## 10. Recovery algorithm

Recovery runs on every `open`. It is a single forward pass with no
backtracking, no heuristics, and no randomness — the same file always produces
the same result.

```
open(path, mode, repair):
  1. if mode == "rw": acquire exclusive flock on <path>.lock, else LockedError
  2. if <path>.compact exists: delete it        # never authoritative (§14)
  3. if file missing or size == 0:
        write 32-byte file header; fsync file; fsync parent directory
        return empty store
  4. read 32-byte file header
        short read, bad magic, bad header_crc, unknown version, flags != 0
          -> FormatError, do NOT truncate, do NOT create
  5. offset = 32; expected_seq = 1; index = {}; last_good = 32
  6. loop:
       a. read 32 bytes at offset
            0 bytes            -> tail = CLEAN, break
            1..31 bytes        -> tail = TORN (short_header), break
       b. verify header_crc    -> mismatch: tail = CORRUPT (header_crc), break
       c. verify magic, version, op in {PUT, DELETE}, flags == 0,
          1 <= key_len <= 4096, val_len <= 8 MiB,
          val_len == 0 if op == DELETE
                              -> failure: tail = CORRUPT (<specific reason>), break
       d. verify seq == expected_seq
                              -> mismatch: tail = CORRUPT (seq_gap), break
       e. read key_len + val_len bytes
            short read         -> tail = TORN (short_payload), break
       f. verify payload_crc  -> mismatch: tail = CORRUPT (payload_crc), break
       g. decode key as UTF-8; if op == PUT, decode value as JSON
            failure            -> tail = CORRUPT (bad_encoding), break
       h. apply: PUT -> index[key] = value_bytes ; DELETE -> index.pop(key, None)
       i. offset += 32 + key_len + val_len
          last_good = offset; expected_seq += 1
  7. if tail != CLEAN:
       if not repair or mode == "r": raise CorruptLogError(report)
       if tail == CORRUPT: copy bytes[last_good:EOF] to <path>.salvage.<n>
       ftruncate(fd, last_good); fsync(fd); fsync(parent directory)
  8. next_seq = expected_seq; write_offset = last_good; store is open
```

**Why we stop at the first invalid record instead of skipping it.** Once a
record fails validation, everything after it is unreliable: we cannot tell a
surviving later record from a stale record left in a reused disk block, and
`seq` continuity — our only cross-record invariant — is broken. Skipping
forward would mean resurrecting data whose relationship to the rest of the log
we cannot establish, which is exactly how a store silently returns a value that
was overwritten years ago. So recovery is conservative and stops.

That restraint has a cost, and we state it rather than hide it: bit rot in the
*middle* of a log discards the valid records after it. Two things mitigate it.
First, `inspect` (§17) *does* resynchronise, reporting how many further records
parse cleanly beyond the damaged region, so a human can make an informed
decision that the automatic path refuses to make. Second, compaction shortens
the log, which shrinks the window.

**Why truncation cannot lose acknowledged data.** The log is append-only and
single-writer, so bytes appear in `seq` order and no committed byte is ever
rewritten. Recovery scans from the start and truncates at `last_good` — the end
of the last fully-validated record. Every acknowledged record therefore lies
entirely before `last_good`, because acknowledgement (§9) required its bytes to
be written, in order, before any later byte existed. Truncation can only
discard bytes belonging to a record that never returned to a caller. The one
exception is the honest one: in `durability="os"` mode, a machine crash can
lose acknowledged records that were still in the page cache — which is what
that mode means.

## 11. Corrupt-tail behaviour

Recovery classifies the end of the log into exactly three states, and `inspect`
reports which one it found:

| State | Physical cause | Action |
| --- | --- | --- |
| `CLEAN` | File ends exactly on a record boundary | Nothing |
| `TORN` | Fewer bytes present than the framing requires: a short header (1–31 bytes) or a payload shorter than `key_len + val_len` | Truncate to `last_good`. This is the expected result of a crash mid-write. Not an error, not logged as damage. |
| `CORRUPT` | Enough bytes are present, but they fail validation: bad `header_crc`, bad magic, invalid `op`, out-of-range length, `seq` gap, bad `payload_crc`, or undecodable CRC-valid payload | Copy the discarded bytes to `<path>.salvage.<n>`, truncate to `last_good`, and surface the reason. |

The `TORN` / `CORRUPT` distinction is the practical value of the two-CRC
format. A torn tail means "the write did not finish" — a normal, expected event
that Ledger repairs without comment. A corrupt tail means "bytes we did write
are not the bytes we wrote" — media damage, a buggy filesystem, or someone
editing the file. That deserves a different reaction.

The distinction is a strong signal, not a proof: a torn write whose tail lands
in a disk block that already held old data can present as `CORRUPT` rather than
`TORN`. Both are truncated to the same offset, so the *recovered state is
identical either way*; only the diagnostic differs. The design never makes a
correctness decision that depends on telling them apart.

`salvage` files exist so that a corrupt tail is never *silently* discarded: the
bytes are preserved verbatim for offline analysis before the file is shortened.
They are never read back automatically. They may contain application data, and
therefore inherit the store's `0600` permissions (§23).

With `repair=False`, or in read-only mode, no truncation happens at all:
`CorruptLogError` is raised carrying the full report (offset, reason, valid
record count, bytes that would be discarded), and the file is left untouched.

## 12. Delete semantics

Deletes are tombstone records, not in-place erasure — required, since the log is
append-only.

- `delete(key)` on a live key appends `op=DELETE, val_len=0` and returns `True`.
  On return the deletion is durable under the same rules as `put` (§9).
- `delete(key)` on an absent key writes nothing and returns `False`. This keeps
  the log from growing under repeated deletes and makes the operation
  idempotent in both state and bytes.
- During recovery, a DELETE record removes the key from the index. A later PUT
  of the same key reinstates it. Ordering is `seq` order, so the last record
  wins.
- After a delete, `get` returns the caller's `default`, `key in db` is `False`,
  and the key is absent from `scan`.
- **The deleted value's bytes remain in the log until compaction.** `delete` is
  not secure erasure, and §23 says so plainly. `compact()` is what physically
  removes them, and even then only from the live file — not from `salvage`
  files, filesystem free lists, or SSD wear-levelling reserves.

Tombstones are dropped entirely by compaction (§14): once no earlier record for
that key exists in the file, the tombstone has nothing to shadow.

## 13. In-memory index reconstruction

The index is a plain `dict[str, bytes]` mapping key → **the exact JSON payload
bytes** for that key. It is built only by the recovery pass in §10, and updated
in place by subsequent `put`/`delete`.

Storing encoded bytes rather than decoded Python objects is deliberate:

- **No aliasing bugs.** If we cached decoded objects, `db.get("k")["items"].append(x)`
  would mutate the store's in-memory state without writing anything to disk, and
  memory would silently diverge from the log. Decoding on every `get` makes the
  returned object the caller's alone.
- **Memory is the compact form.** Footprint tracks the serialized size of live
  data, not the (typically several times larger) Python object graph.
- **Compaction copies bytes.** No re-encoding, therefore byte-exact output and
  no risk of a re-encode changing a value.

The cost is a `json.loads` per `get`. For application-state workloads — small
documents, far more writes than reads of any single key — that is the right
trade, and §21 says how we would measure it if it stopped being true.

The rejected alternative was an offset index (`key -> (offset, length)`) with a
read from disk per `get`. It supports datasets larger than RAM, at the cost of
a syscall per read, a second cache layer to make that acceptable, and a
compaction step that must rewrite offsets under the lock. Larger-than-RAM is a
non-goal (§2), so we took the simpler structure. **The consequence is a real
limit: all live values must fit in memory**, and the README will say so next to
the sizing guidance.

`scan` iterates `sorted(index)` (optionally filtered by prefix) and decodes
lazily as it yields, so a prefix scan does not decode the whole store. Sorted
order is part of the contract: it makes CLI output and test assertions
deterministic.

Reconstruction cost is one sequential pass over the file: O(bytes on disk),
no seeking, no random I/O. §21 covers what that means for startup time.

## 14. Compaction design

Compaction rewrites the log containing only live records — one PUT per live
key, no superseded versions, no tombstones — and atomically replaces the old
file.

```
compact():
  1. requires the writer lock (already held by this handle)
  2. write <path>.compact:
       file header with generation = old_generation + 1
       for key in sorted(index):        # sorted -> deterministic output
           one PUT record, seq renumbered from 1
  3. fsync(<path>.compact)
  4. os.replace(<path>.compact, <path>)     # atomic within one filesystem
  5. fsync(parent directory)                # makes the rename itself durable
  6. reopen fd on the new file; reset write_offset and next_seq
     (the in-memory index is unchanged by compaction and is not rebuilt)
  7. return CompactionResult(records_before, records_after, bytes_before, bytes_after)
```

Crash safety, by phase:

| Crash point | State on disk | Result of next open |
| --- | --- | --- |
| Before step 4 | Original file intact; stray `.compact` file | Stray file deleted (§10 step 2); original recovered normally |
| During step 4 | `os.replace` is atomic: the path names either the old inode or the new one, never a mixture | Whichever it names is a complete, valid log |
| After step 4, before step 5 | New file in place, rename possibly not durable | Either the old or the new file — both are complete and contain the same live state |

There is no window in which the store is unreadable, and no window in which a
live key is absent from both files. The old inode is unlinked by `os.replace`;
any reader holding it open keeps reading a valid older snapshot until it
reopens (§15).

The `.compact` temp file is **never** authoritative. It is deleted, not
resumed, on the next open. Resuming a partial compaction would mean trusting a
file whose writer died at an unknown point — the one thing we refuse to do
anywhere else in this design.

Sequence numbers restart at 1 in the new file. `seq` is a *within-file* framing
invariant (§5), not a global logical clock, and nothing outside a single file's
recovery pass depends on it. `generation` in the file header is the counter that
survives compaction, and it is diagnostic only.

Compaction is explicit — `db.compact()` or `ledger compact FILE`. There is no
automatic trigger, no background thread, and no threshold to tune. `inspect`
reports the dead-byte ratio so an application or an operator can decide. Adding
an automatic policy would add configuration, a timing-dependent code path, and
a class of test that is hard to make deterministic; the value it adds in a
72-hour project is negative.

## 15. Concurrency model

The smallest model we can actually prove:

**One writer process. Many reader processes. Thread-safe within a process.**

1. **One writer at a time, enforced.** A `mode="rw"` open takes a
   non-blocking exclusive `flock` on the sidecar lock file. If another process
   holds it, `open` raises `LockedError` immediately rather than blocking. Fail
   fast beats a mysterious hang.
2. **Readers take no lock.** A `mode="r"` handle never writes, never truncates,
   and never locks. It is safe concurrently with the writer because of the
   append-only invariant: the writer only appends, so a reader's forward scan
   sees a *valid prefix* of the log. If it catches a record mid-write it
   classifies the tail as `TORN` and stops — yielding exactly the state as of
   the last complete record. That is a consistent point-in-time snapshot, not a
   torn read.
3. **Reader snapshots are immutable and can go stale.** A reader's index is
   built at open and never refreshed; to see newer writes, reopen. If the
   writer compacts, `os.replace` swaps the inode and the reader keeps its old
   (complete, valid) file open until it reopens.
4. **Thread safety within a process.** All public methods on a handle are
   serialized by one `threading.Lock`. Concurrent `put` from multiple threads
   is safe and produces a well-defined total order. This is serialization, not
   parallelism — a multi-threaded writer gets correctness, not throughput. The
   lock is held across the `fsync`, so concurrent writers queue behind it.
5. **`Ledger` handles are not fork-safe.** A forked child inherits the fd and
   the flock, and both processes would append through the same file
   description. Open a fresh handle after forking. Documented, not defended
   against.

What we explicitly do **not** promise: multi-process writes, multi-key
atomicity, transactions, isolation levels, reader–writer coordination beyond
the snapshot property above, or any ordering between processes.

Test 18 (§19) is written against exactly these claims and nothing more:
concurrent threads produce a consistent store; a second `rw` open raises
`LockedError`; a concurrent reader observes a valid prefix and never an invalid
or partial value.

## 16. File locking strategy

`fcntl.flock(lock_fd, LOCK_EX | LOCK_NB)` on `<path>.lock`, a zero-length
sidecar created on demand and never deleted.

**Why a sidecar rather than the data file itself.** Locks are held on an inode.
Compaction calls `os.replace`, which points the path at a *new* inode — so a
lock taken on the data file would silently stop protecting the store at the
moment of the swap, and a second writer opening just after a compaction would
lock a different inode and see no conflict. The lock file is never replaced, so
its inode is stable for the life of the store.

The lock file is never unlinked, either. Deleting it opens a classic race:
process A unlinks the file it holds locked while process B is opening it, and B
ends up locking an inode with no name that A's successor will never see. A
stale empty lock file costs nothing.

`flock` was chosen over `fcntl.lockf` (POSIX record locks) because `lockf`
locks are associated with the *process* and are dropped when *any* fd on the
file is closed — a well-known footgun in libraries, where an unrelated part of
the program opening and closing the same path silently releases your lock.
`flock` locks are associated with the open file description, which matches the
handle lifetime we actually want.

Documented limitations, stated as limitations:

- **POSIX only.** `fcntl` does not exist on Windows. Windows is unsupported
  (§22), not partially supported.
- **Advisory.** It constrains processes that use Ledger. It does not stop
  `echo garbage >> state.ledger`.
- **Not reliable over NFS/SMB.** `flock` semantics on network filesystems are
  implementation-dependent. Local filesystems only.
- **Process-scoped only.** It coordinates processes, not threads; §15's
  `threading.Lock` handles threads.
- Released automatically by the OS when the process exits or the fd closes, so
  a crashed writer never leaves the store permanently locked.

## 17. CLI design

`argparse`, one subcommand per operation, invoked as `ledger` (console script),
`python -m ledger`, or `./ledger.pyz` (the zipapp, §24).

```
ledger put     FILE KEY VALUE [--json]
ledger get     FILE KEY
ledger delete  FILE KEY
ledger scan    FILE [--prefix P] [--json]
ledger inspect FILE [--json] [--verbose]
ledger compact FILE
```

- `put` treats `VALUE` as a plain string by default; `--json` parses it as JSON
  first. String-by-default is the unsurprising behaviour for
  `ledger put db.ledger name Venu`, and `--json` is explicit for structured
  values. Guessing between the two would make `ledger put db count 42`
  ambiguous.
- `get` prints the value as JSON on stdout, so output is unambiguous and pipeable.
- `scan` prints `key<TAB>json_value` per line, sorted by key. `--json` emits a
  single JSON object instead.
- `inspect` is **read-only**: it opens in `mode="r"`, never locks, never
  truncates, and never repairs. It reports: file size, `generation`, format
  version, total records, valid records, live keys, dead bytes and dead-byte
  ratio, tail state (`CLEAN`/`TORN`/`CORRUPT`) with the offset and the specific
  reason, and — when the tail is damaged — the result of a resynchronisation
  scan for further `LGR\x1e` markers beyond it, so a human can see what
  automatic recovery is choosing to discard. This is the diagnostic centrepiece
  of the demo.
- `compact` opens `rw` (so it takes the lock and repairs), compacts, and prints
  before/after sizes.

**There is no `ledger recover` command,** and its absence is a design
statement. Recovery is not an optional maintenance step the user must remember
to run — it happens automatically on every write-mode open. A `recover`
subcommand would imply the opposite. The demo is stronger for it: `inspect`
shows the torn tail, then a plain `get` returns the committed value, because
recovery already happened. `inspect` covers "tell me what is wrong"; opening
covers "fix it".

Exit codes, fixed and testable:

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Key not found (`get`, `delete`) |
| 2 | Usage error (argparse) |
| 3 | Store locked by another process |
| 4 | Format error or corrupt log with `repair` refused |
| 5 | I/O error |

Everything the CLI prints on success goes to stdout; diagnostics and warnings
go to stderr. No colours, no spinners, no progress bars — the output is meant
to be diffed in a test.

## 18. Error model

```
LedgerError(Exception)              base; catch this to catch everything
├── FormatError                     not a Ledger file, unknown version, bad file header
├── CorruptLogError                 damaged tail found and not repaired (carries the report)
├── LockedError                     another process holds the writer lock
├── ReadOnlyError                   mutation attempted on a mode="r" handle
├── ClosedError                     operation on a closed handle
└── WriteError                      write/fsync failed; handle is poisoned (§8)
```

Rules:

- Every failure that is *about the store* raises a `LedgerError` subclass.
  Every failure that is *about the caller's arguments* raises the standard
  built-in: `TypeError` for a non-`str` key or a non-JSON-encodable value,
  `ValueError` for an empty key or an oversize key/value.
- `get` never raises `KeyError`; a missing key returns `default`. Absence is
  not exceptional in a state store.
- `CorruptLogError` and `inspect()` share one report object, so the
  programmatic and human paths report identical facts.
- Exceptions carry the store path, byte offset, and specific reason where those
  exist. `"corrupt log"` with no offset is not an acceptable message.
- No exception is raised from `close()` for an already-closed handle; `close()`
  is idempotent.
- OS errors from the filesystem (`ENOSPC`, `EACCES`) propagate as `OSError`
  unless they occur inside the write path, where they are wrapped in
  `WriteError` (with `__cause__` preserved) because the handle state changes as
  a result.

## 19. Testing strategy

`unittest` from the standard library. No pytest, no plugins. One command:

```
python3 -m unittest discover -s tests -v
```

The eighteen required cases, mapped to files:

| # | Case | Test |
| --- | --- | --- |
| 1 | Empty database | `test_basic.TestBasic.test_open_empty_creates_header` |
| 2 | Single put/get | `test_basic.test_put_get_roundtrip` |
| 3 | Multiple puts | `test_basic.test_many_keys` |
| 4 | Update existing key | `test_basic.test_overwrite_returns_latest` |
| 5 | Delete | `test_basic.test_delete_removes_key` |
| 6 | Restart persistence | `test_recovery.test_state_survives_reopen` |
| 7 | Multiple restarts | `test_recovery.test_state_survives_ten_reopens` |
| 8 | Partial record | `test_corruption.test_truncated_record_is_torn_tail` |
| 9 | Partial header | `test_corruption.test_partial_header_1_to_31_bytes` |
| 10 | Partial payload | `test_corruption.test_partial_payload` |
| 11 | Invalid checksum | `test_corruption.test_flipped_payload_crc` / `test_flipped_header_crc` |
| 12 | Invalid length | `test_corruption.test_absurd_key_len` / `test_absurd_val_len` |
| 13 | Unknown operation | `test_corruption.test_unknown_op_byte` |
| 14 | Crash during write | `test_crash_subprocess.test_sigkill_mid_write` |
| 15 | Recovery after crash | `test_crash_subprocess.test_committed_state_survives_sigkill` |
| 16 | Compaction correctness | `test_compaction.test_compaction_preserves_state` |
| 17 | Delete + compaction | `test_compaction.test_compaction_drops_tombstones` |
| 18 | Concurrent access | `test_concurrency.py` (whole module) |

Beyond the required set, three test groups do most of the actual proving:

**Exhaustive truncation matrix** (`test_recovery`). Build a log of N records.
For **every** byte length L from 0 to the file size, truncate a copy to L,
reopen it, and assert: open succeeds; the recovered state equals the state
after the largest whole number of records that fit in L; the file has been
truncated to that record boundary; and reopening a second time changes nothing.
This is a complete proof over the torn-write space for a small log, it is
deterministic, and it runs in under a second.

**Single-bit corruption sweep** (`test_corruption`). For every byte offset in a
small log, flip one bit in a copy and reopen. Assert the invariants that must
hold for *every* offset: the open either succeeds or raises a `LedgerError`
(never an unhandled exception, never a hang, never an unbounded allocation);
every value returned is a value that was actually written at some point; and
the recovered prefix is always a prefix of the true history. Seeded
`random.Random` where sampling is needed, so failures reproduce exactly.

**Idempotent recovery** (`test_recovery`). Recovering an already-recovered file
is a no-op, byte for byte. Recovery twice equals recovery once.

Supporting rules:

- `tests/helpers.py` owns the byte-surgery utilities (`truncate_to`,
  `flip_bit_at`, `set_header_field`, `append_garbage`) so corruption tests
  express *intent*, not `struct.pack` calls.
- Every test uses `tempfile.TemporaryDirectory`. No test touches a fixed path,
  and tests are order-independent.
- Assertions are on observable behaviour and on documented byte layout — never
  on private attributes.
- Concurrency tests are structured around deterministic synchronisation
  (`threading.Barrier`, reading a child's stdout line), never `sleep`.
- `test_no_dependencies.py` is a real test, not documentation (§25).

## 20. Crash-injection strategy

The requirement is a *deterministic* crash test. Racing `SIGKILL` against a
`write()` is not deterministic — the kernel usually completes the write, so the
interesting torn-record case would appear rarely and unpredictably. We use a
subprocess harness with two deterministic injection modes instead.

`tests/crash_child.py` is a standalone script, run by the parent with
`subprocess.Popen`. It takes a store path and a scenario name and always ends
by killing *itself* with `os.kill(os.getpid(), signal.SIGKILL)` — a real,
uncatchable process death with no interpreter cleanup, no `finally` blocks, no
buffer flushing, and no `atexit`.

**Mode A — kill between commits.** The child opens the store, writes N records
through the public API (each fsync'd), prints a line to stdout, and flushes
after each. The parent reads exactly N lines — so it knows precisely which
writes committed — and then the child `SIGKILL`s itself. The parent reopens the
store in a fresh process and asserts all N records are present and the tail is
`CLEAN`. Fully deterministic: the synchronisation is a pipe read, not a timer.

**Mode B — torn record.** The child commits N records normally, then
deliberately appends the first *k* bytes of a well-formed record N+1 and
immediately `SIGKILL`s itself. The parent reopens and asserts: the tail is
detected as `TORN` at the right offset, the file is truncated to the boundary
after record N, all N committed records are intact, and record N+1 is entirely
absent. Sweeping *k* across header-boundary values (1, 16, 31, 32, 33,
32+key_len, 32+key_len+val_len−1) covers every torn shape the format can
produce.

Two properties of this harness matter:

- **No test hooks in library code.** The seam lives entirely in the child
  script, which writes the partial bytes itself. Nothing in `ledger.py` knows
  tests exist — no `if os.environ.get("LEDGER_CRASH_AT")`, no injectable write
  function, no monkeypatch surface. The shipped code path is the tested code
  path.
- **The on-disk shape is honest.** A power failure mid-write leaves a prefix of
  a record at the end of the file. So does mode B. Recovery cannot tell the
  difference, and that is the point. A variant appends `bytes[:k] + random
  garbage` to reproduce the "torn write landing in a reused block" case, which
  is what exercises the `CORRUPT` classification path.

**The demo uses this exact harness**, not a scripted imitation of it — the
`SIGKILL` on screen is the same one in the test suite.

## 21. Performance considerations

We will publish a cost model now and measured numbers only if we measure them
on the demo machine, with the command and hardware named. No comparative
benchmarks against other libraries unless we actually run those libraries. No
extrapolated or illustrative figures.

Cost model:

| Operation | Cost |
| --- | --- |
| `put` / `delete` | 1 `write()` + 1 `fsync()`. **The fsync dominates by orders of magnitude** — roughly tens of microseconds on NVMe, single-digit milliseconds on spinning rust. Everything else (JSON encode, two CRCs, `struct.pack`) is noise beside it. |
| `put` with `durability="os"` | 1 `write()`, no fsync. Bounded by memcpy into the page cache. |
| `get` | dict lookup + one `json.loads`. No I/O, no syscall. |
| `scan` | `sorted()` over the key set + lazy decode per yielded item. |
| `open` | one sequential pass over the whole file: O(bytes on disk). This is the number that matters for startup, and it is why compaction exists. |
| `compact` | one pass over live keys + one fsync + one rename. O(live bytes). |
| Memory | ≈ the serialized size of live values, plus dict overhead per key. |

Consequences we accept, and would fix in this order if we had reason:

1. Every mutation fsyncs. That is the durability the product is named for. The
   documented escape hatch is `durability="os"` for callers who genuinely do
   not need power-loss safety.
2. Log growth is unbounded until `compact()`. Overwrites and deletes leave dead
   bytes; `inspect` reports the ratio so the application can decide.
3. Startup is linear in file size, not in live-key count. A store that is
   overwritten heavily and never compacted gets slow to open.

Deliberately **not** implemented, and why: group commit / batched fsync (needs a
batch API and raises atomicity questions we would then have to answer), `mmap`
(no benefit for an append-only writer, real complexity around remapping),
binary value codecs (JSON is the safe, inspectable choice — §4), offset-only
index (larger-than-RAM is a non-goal — §13), background compaction (timing-
dependent code path, hard to test deterministically). Each is a real
optimisation; none is needed to demonstrate what this project is demonstrating,
and each would add a code path to defend.

## 22. Platform assumptions

Supported: **Linux and macOS, Python 3.9+, local filesystems** (ext4, xfs,
btrfs, APFS, HFS+). CI/dev target 3.11 and 3.12.

Assumptions we rely on, stated so they can be challenged:

1. `os.replace` is atomic when source and destination are on the same
   filesystem. Compaction's correctness rests on this (§14).
2. `fsync` on a *directory* fd makes a rename in that directory durable. This
   is why compaction and store creation fsync the parent directory, not just
   the file.
3. `fcntl.flock` provides advisory exclusion between processes on the same
   local filesystem (§16).
4. `O_APPEND` writes are placed at the current end of file by the kernel.
5. **A crash can tear a `write()` at arbitrary byte granularity.** We assume
   *nothing* about sector-atomicity — no "512-byte writes are atomic", no
   "4 KiB is a sector". This is the strongest safe assumption, and the
   truncation matrix in §19 tests every byte boundary because of it.

Assumptions we do **not** make, and the honest caveat: some consumer SSDs and
some virtualised or network-backed block devices acknowledge `fsync` before
data is on stable media. No userspace library can detect or fix that. Ledger's
power-loss guarantee is exactly as good as the hardware's `fsync`, and the
README will say so in those words rather than claiming durability we cannot
deliver.

Not supported, deliberately: **Windows** (no `fcntl`; different rename
semantics for open files; would need a separate locking design and a test
environment we do not have — adding it untested would be worse than omitting
it), **NFS/SMB** (locking and fsync semantics are not dependable), **32-bit
platforms with files above 2 GiB**, and **forked handles** (§15).

## 23. Security limitations

Stated plainly, because a storage project that is vague about this is not
trustworthy.

1. **No encryption at rest.** Keys and values are stored as readable UTF-8
   JSON. Anyone who can read the file can read the data.
2. **CRC32 is error detection, not tamper detection.** An attacker who can
   write to the file can change a value and recompute both CRCs. Detecting
   *deliberate* modification requires a keyed MAC and key management, which is
   out of scope. We never describe checksums as protecting against tampering.
3. **Advisory locking only.** `flock` constrains cooperating processes. It does
   not stop any process from writing to the file directly (§16).
4. **File permissions.** The store is created `0600` (owner read/write only),
   and so are `.lock`, `.compact`, and `.salvage` files. The parent directory's
   permissions are the caller's responsibility. Note that `.salvage` files
   contain raw discarded log bytes and therefore application data.
5. **`delete` is not erasure.** The value's bytes remain in the log until
   compaction, and even then may persist in filesystem free space, journals,
   backups, or SSD over-provisioning. Ledger offers no secure-delete guarantee.
6. **JSON was chosen over pickle specifically as a security decision** (§4). A
   store designed to read damaged and possibly foreign files must never
   deserialize a format that can execute code. This is the single most
   important security property of the design.
7. **Untrusted input.** Reading a Ledger file from an untrusted source is
   memory-safe (all lengths are bounds-checked after CRC verification, so no
   attacker-controlled allocation) but the *values* are attacker-controlled
   JSON. Validate them like any other untrusted input.
8. **No resource limits.** A local caller can fill the disk. There is no quota,
   rate limit, or maximum store size.
9. **Path handling is unguarded.** No protection against a symlink or a
   hostile parent directory at the store path.

## 24. Reproducibility strategy

Target: **byte-identical build output** from an identical source tree, on any
machine, at any time, in any directory.

One documented build command, pure standard library:

```
python3 tools/build.py          # -> dist/ledger.pyz + dist/SHA256SUMS
```

The artifact is a zipapp: a zip archive of the sources with a `#!` shebang
prepended, executable as `./ledger.pyz` and importable via `PYTHONPATH`.

`zipapp.create_archive` does not expose enough control over the archive to make
it deterministic, so `tools/build.py` writes the zip directly with `zipfile`,
which is still standard library and makes every determinism decision explicit:

1. **Fixed member order** — sources sorted by path, not filesystem order.
2. **Fixed timestamps** — every `ZipInfo.date_time` set to a constant, honouring
   `SOURCE_DATE_EPOCH` when set and defaulting to the zip epoch
   (1980-01-01 00:00:00) otherwise.
3. **Fixed permissions** — `external_attr` set to a constant `0644` (`0755` for
   the archive itself), so a contributor's umask cannot change the output.
4. **Fixed compression** — `ZIP_DEFLATED` at an explicitly pinned level.
5. **No `__pycache__`, no `.pyc`** — bytecode embeds paths and magic numbers
   that vary across Python versions.
6. **No build metadata** — no build date, no hostname, no absolute path, no
   version-control state written into the artifact.
7. **Fixed shebang** — `/usr/bin/env python3`.

Verification, also one command:

```
python3 tools/build.py --verify   # builds twice into separate temp dirs,
                                  # compares SHA-256, exits non-zero on mismatch
```

The expected digest is committed in `dist/SHA256SUMS` and printed in the
README, so a judge can run the build and compare against a published hash. A
test in the suite runs the double-build comparison, making non-reproducibility
a test failure rather than a claim.

The build depends only on: CPython ≥ 3.9 and the source tree. No compiler, no
network access, no lockfile, no build backend, no `pip`.

## 25. Zero-dependency strategy

The rule is that the runtime imports **only** the Python standard library. We
treat that as something to *prove mechanically*, not to assert in a README.

**Runtime import allowlist** (the complete set `ledger.py` may import):

`os`, `io`, `sys`, `json`, `zlib`, `struct`, `fcntl`, `errno`, `signal`,
`argparse`, `pathlib`, `threading`, `typing`, `dataclasses`, `contextlib`,
`binascii`, `logging`.

Test and build tooling may additionally use `unittest`, `tempfile`,
`subprocess`, `random`, `shutil`, `zipfile`, `hashlib`, `ast`, `time`,
`statistics` — all standard library, and none of it shipped in the artifact.

**Three independent proofs**, all runnable by a judge:

1. **Static import audit as a test** — `tests/test_no_dependencies.py` parses
   every source file with the `ast` module, collects every `import` and
   `from … import` statement (including function-local ones), and asserts each
   top-level module name is either the project itself or a member of
   `sys.stdlib_module_names`. A third-party import is a **failing test**, not a
   review finding. (A pinned fallback list covers Python 3.9, where
   `sys.stdlib_module_names` does not exist.)
2. **Isolated interpreter run** — the suite also runs under
   `python3 -I -S -m unittest discover -s tests`. `-S` disables `site`, so
   `site-packages` is not on `sys.path` at all, and `-I` ignores user site and
   `PYTHONPATH`. If anything third-party were reachable, this run fails. This
   is the strongest of the three: it does not inspect the code, it removes the
   possibility.
3. **Import inventory** — `python3 tools/depcheck.py` prints every imported
   module with its resolved origin, so the claim can be read rather than
   trusted. Its output is pasted verbatim into the dependency-proof section of
   the README.

Structural commitments: no `requirements.txt`, no `install_requires`, no
optional-extras, no vendored code, no code copied from other projects. The
`pyproject.toml` exists only for packaging metadata and declares zero
dependencies; the zipapp needs no installation at all.

`STDLIB.md` will document each place where a third-party package is the
conventional answer and which standard-library facility we used instead —
`zlib.crc32` for checksums, `struct` for binary framing, `fcntl` for locking,
`os.replace` for atomic file replacement, `argparse` for the CLI, `unittest`
for tests, `subprocess` + `signal` for crash injection, seeded deterministic
matrices in place of a property-testing library, `zipapp`/`zipfile` for
packaging, `ast` + `sys.stdlib_module_names` for dependency auditing, and
several more. Each entry names the package it replaces, what we gave up, and
what we gained.
