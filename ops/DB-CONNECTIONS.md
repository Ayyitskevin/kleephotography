# Connection reuse

`db.one` and `db.all_` reuse a **per-thread** SQLite connection. `db.run` and
`db.tx()` still open their own, unchanged. That is option 1 below, shipped
because connect-per-statement was 99.7% of a read and a 60-photo gallery load
spent ~150 ms opening the database.

Reads are keyed by `config.DB_PATH` so a test that points at a throwaway file
does not keep reading the previous one. Write paths and `BEGIN IMMEDIATE`
semantics are untouched. Do not hold a cursor on the read connection across a
yield — `one`/`all_` exhaust theirs in the same statement; a held cursor pins
a WAL snapshot and blocks checkpointing.

The rest of this file is the decision record that led here.

## What it costs (measured before the change)

Measured against a fully migrated database:

| | |
|---|---|
| `db.one` — connect + query + close | **0.879 ms** |
| the same query on a connection already open | **0.003 ms** |

Connection setup is **99.7%** of a read. A 60-photo gallery load makes ~186
reads — 6 for the page, 3 per thumbnail for the gallery / visitor / asset
lookups — so roughly **150 ms per gallery load is spent opening the database** to
perform about half a millisecond of querying.

## Why, and why it gets worse

Not the filesystem, and not the pragmas. Both were checked:

| | |
|---|---|
| connect to a **1-table** database | 0.102 ms |
| connect to the **Mise** database (50 tables, 66 indexes, 31 KB of DDL) | **0.744 ms** |

Every connection re-parses `sqlite_master`. The cost scales with schema size, so
it has grown with every one of the 73 migrations and will keep growing. Dropping
`PRAGMA journal_mode` saves nothing — it is persistent in the file, and the
apparent cost of any pragma is really the first statement forcing the lazily
opened file to be read.

That also means the number transfers to the prod host: the schema is the same
everywhere, and only the ~0.1 ms floor is filesystem-dependent. **Confirm before
acting** — on `mickey`:

```sh
sudo -u mise python3 - <<'PY'
import sqlite3, statistics, time
P = "/opt/mise/data/mise.db"
ts = []
for _ in range(200):
    t = time.perf_counter()
    c = sqlite3.connect(P, timeout=30)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000"); c.execute("PRAGMA synchronous=NORMAL")
    c.close()
    ts.append((time.perf_counter() - t) * 1000)
print(f"connect: {statistics.median(ts):.3f} ms  ->  x186 = {186*statistics.median(ts):.0f} ms/gallery")
PY
```

If that prints well under 0.2 ms, the whole question is worth roughly 20 ms per
gallery load and the honest answer is **option 0**.

## The constraint that shapes every option

`sqlite3.connect` defaults to `check_same_thread=True`, so a connection cannot
be shared across threads without disabling that — and disabling it means owning
the serialization yourself. Any reuse scheme therefore has to be **per-thread or
narrower**, never a process-wide singleton.

Threads that touch the database: the anyio worker pool that runs every sync
route handler (40 by default), the job pool (`MISE_JOB_WORKERS`, default 2), the
job sweeper, the recurring scheduler, and the single `/healthz` prober.

## Options

### 0. Leave it alone

~150 ms per gallery load, growing slowly with each migration. Those 60 thumbnail
requests are already parallel and each also does file I/O, so the user-visible
cost is smaller than the total suggests. No risk, no work.

Reasonable if the number on `mickey` comes back small, and the honest default if
nobody wants to own the risks below.

### 1. Thread-local connection for reads — **recommended**

`db.one` and `db.all_` take a connection from thread-local storage and leave it
open. `db.run` and `db.tx()` keep opening their own, unchanged.

Measured on 186 reads: **148.3 ms → 0.4 ms.**

Why reads only: writes are where transaction state, rollback and `BEGIN
IMMEDIATE` semantics live, and `db.tx()`'s contract is that its connection is
scoped to the block. Leaving that alone keeps the blast radius on the path that
is provably hot and provably read-only.

Risks, and how big each really is:

- **Page cache per connection.** `cache_size` defaults to −2000, i.e. 2 MB per
  connection. Across ~45 live threads that is up to ~90 MB of steady-state
  memory the process does not use today. Mitigate by setting a smaller
  `cache_size` on read connections, and measure RSS before and after.
- **A held cursor pins a WAL snapshot.** Demonstrated: a half-read cursor left
  open on a persistent connection blocked checkpointing and let the WAL grow to
  10 MB, which a checkpoint then truncated to 0 the moment it was closed. But
  `db.one` writes `con.execute(...).fetchone()` with the cursor as a *temporary*
  — CPython frees it immediately, and that pattern was measured to pin nothing.
  `db.all_` exhausts its cursor. So the risk is real but does not apply to the
  two functions being changed. It **would** apply if either ever started holding
  a cursor, which is worth a comment at the call site and, better, a test that
  asserts the WAL can still be checkpointed after a read.
- **Thread lifetime.** Anyio worker threads and the job pool are long-lived, so
  connections accumulate to a ceiling rather than leaking without bound. Threads
  that do die take their connection's file descriptor with them only at process
  exit; the count is bounded by the pool sizes above.
- **Staleness is not a risk here** — checked, not assumed. A long-lived read
  connection was made to read a row, a *different* connection updated it, and the
  reused connection saw the new value on its next statement: each statement in
  autocommit starts a fresh read snapshot. A `wal_checkpoint(TRUNCATE)` also
  returned busy=0 immediately afterwards, so an ordinary read leaves nothing
  pinned. Both belong in the test that ships with the change.

### 2. One connection per HTTP request

A connection created at the start of a request, put in a `ContextVar`, closed by
middleware at the end.

Cleaner lifetime semantics — guaranteed close, nothing accumulates — but it wins
less: a gallery load is 61 HTTP requests, so this replaces 186 connections with
61, about **two thirds** of the saving rather than effectively all of it. It also
does nothing for the job and scheduler threads, which have no request to hang the
connection on.

Worth preferring only if the memory ceiling in option 1 turns out to matter.

### 3. A real connection pool

Checkout/checkin, sizing, health checks. This is the shape used for network
databases, where the expensive part is a TCP handshake and an auth round trip.
Here the connection is a local file handle; the pool would add machinery and a
new class of bug (leaked checkouts) to solve a problem options 1 and 2 already
solve. Not recommended.

### 4. Ask for less, orthogonally

The media path spends 3 queries per thumbnail: gallery, visitor, asset. The
first two are *identical across all 60 requests* in a gallery load. Caching them
in-process for a few seconds would cut ~120 of the 186 reads regardless of which
option above is chosen.

Deliberately listed last, because it caches an **authorization** decision. A
revoked PIN or a deleted visitor would keep working for the life of the cache
entry, which is a security property traded for latency — a different kind of
decision from the others here, and one this file does not recommend making
casually.

## Recommendation (taken)

**Option 1, reads only.** Long-lived read connections set `PRAGMA cache_size=-500`
(500 KiB instead of the 2 MB default). A test asserts a `wal_checkpoint(TRUNCATE)`
still returns busy=0 after a reused read, and that a write on another connection
is visible on the next `one()`.

Option 2 remains the fallback if RSS on the prod host grows more than a few tens
of MB. Option 4 (caching the per-thumbnail auth lookups) is still not recommended:
it caches an authorization decision.

## What would make this wrong

Written down so it can be checked rather than argued:

- `connect` on `mickey` is much faster than measured here — then option 0.
- RSS after the change grows more than a few tens of MB — cap `cache_size` first,
  then fall back to option 2.
- The WAL stops being checkpointed in steady state (watch its size) — a read
  path is holding a cursor; find it before going further.
