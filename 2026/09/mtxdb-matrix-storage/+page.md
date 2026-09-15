---
title: "mtxdb: append-only packfiles for Matrix DAG storage"
date: "2026-09-05"
description:
  "Run Synapse on spinning HDDs. Append-oriented packfile storage with a
  persisted index, inspired by MDBX, BadgerDB, and WiscKey."
subtitle: "Append-only packfiles, inspired by MDBX, BadgerDB, and WiscKey."
tags: ["Matrix", "Storage", "Performance"]
draft: false
---

Matrix homeservers can store gigabytes of room data. The event DAG for a busy
room may accumulate millions of HAMT nodes over its lifetime, most of them
unreachable from the latest state after subsequent transitions. Traditional
B-tree storage engines are optimized for mutable records; their write paths can
involve read-modify-write cycles and random I/O that are especially costly on
spinning disks.

[mtxdb](https://github.com/Wombat-Foundation/mtxdb) takes the opposite approach:
**never mutate individual records.** New records are appended; a collection can
be deleted logically, and physical space is reclaimed by a caller-scheduled
repack that rewrites reachable data in a chosen traversal order. The result is
an append-oriented storage engine with constant-time _in-memory_ index probes
and a layout hypothesized to make selected graph walks more sequential
(unmeasured so far — see
[Benchmarks and trade-offs](#benchmarks-and-trade-offs)).

mtxdb is append-oriented, not pure sequential I/O: cold point reads are still
location-directed record reads, and repack reads can be scattered before the
repacker writes sequential output. The sequential win is on the write path
(appends) and on reopen via a persisted index.

Custom binary format, inspired by `libmdbx` and `LeanStore` (also `SplinterDB`,
`Fjall`, `git-repack`, and `PGM-index` — the last one mostly as a
counterexample; a learned index over uniformly random hashes degenerates to the
flat fanout table below, so that's just what's built). The core crate isolates
one `unsafe` block for `memmap2`; after a shard is mapped, steady-state reads
can access its bytes without a read syscall. An index probe takes a few memory
operations; a cache miss still pays for the record read. The cache helps writes
and swizzled nodes, rather than turning every cold lookup into an LRU entry.

The POPCOUNT-indexed HAMT trie, WAL, transactions, and snapshots aren't built
yet — see [Roadmap](#roadmap).

<!-- markdownlint-disable MD013 -->

[The problem](#the-problem) · [Packfile format](#packfile-format) ·
[The lossy fanout index](#the-lossy-fanout-index) ·
[Topological repack](#topological-repack) ·
[The storage engine trait](#the-storage-engine-trait) ·
[Benchmarks and trade-offs](#benchmarks-and-trade-offs) · [Roadmap](#roadmap)

<!-- markdownlint-enable MD013 -->

## The problem

Synapse's state storage has two hot paths:

1. **State resolution**: given a set of state groups, walk the DAG to find the
   current state. This is a graph traversal over `prev_events` and
   `auth_events`, touching hundreds of nodes per room.

2. **Event ingestion**: append a new event and its state snapshot. This is a
   sequential write — but the state snapshot is a content-addressed HAMT node
   that may reference thousands of historical nodes.

On SSDs, both paths may be fast enough for many deployments. On spinning disks,
random seeks are costly. A deliberately pessimistic state resolution that
performs 500 uncached, serialized reads at roughly 8 ms each would spend about
four seconds waiting on seeks. Real workloads benefit from caching, request
parallelism, and the drive's scheduler, but the example illustrates the scale of
the gap.

Here's why this matters: the gap between sequential and random reads is
enormous, even on modern hardware. The table below is illustrative
(order-of-magnitude, not a reproducible benchmark for this workload):

<!-- markdownlint-disable MD013 -->

| Drive Type     | Sequential Read | Random 4K Read   | Gap   |
| -------------- | --------------- | ---------------- | ----- |
| Gen 5 NVMe SSD | ~13,000 MB/s    | **~80–100 MB/s** | ~140× |
| Gen 4 NVMe SSD | ~7,000 MB/s     | ~70–80 MB/s      | ~90×  |
| SATA SSD       | ~550 MB/s       | ~40–50 MB/s      | ~12×  |
| HDD            | **~150 MB/s**   | ~0.5–1 MB/s      | ~200× |

<!-- markdownlint-enable MD013 -->

A Gen 4 NVMe SSD advertising 7,000 MB/s on the box can deliver far less when a
workload reads thousands of small, scattered files. The drive is not broken:
sequential bandwidth and random-I/O throughput are different measurements, and
the latter also depends on queue depth, block size, firmware, and the host.
Treat the figures above as illustrative vendor-spec magnitudes; they carry no
workload/queue-depth methodology for this article's premise. Check vendor spec
sheets and independent storage benchmarks (e.g. with `fio` at a stated queue
depth and block size) before reasoning about a specific drive.

The core hypothesis is that a large share of historical nodes is unreachable
from a workload's chosen roots. If the reachable closure is small and the
repacker places it in the order that walk consumes it, the walk approaches a
sequential scan. That must be measured against real room histories: a current
state traversal, backfill, and auth-chain walk do not necessarily want the same
order.

## Packfile format

mtxdb stores nodes in a global pool of shared, append-only shard files. Each
frame carries its collection (room) ID, so a shard can contain records from many
rooms while each room retains its own logical index:

```text
[MAGIC: "MTDB"] [version: 0x04]
[4 KiB shard header: pack ID, creation time, feature flags, header CRC]

Record 0:
  [u32 len]       — byte length through node bytes, little-endian
  [u8 flags]      — compression flags
  [u32 raw len]   — original payload length
  [16-byte room]  — collection ID (for shard-scan recovery)
  [16-byte hash]  — structural hash (index-rebuild metadata only)
  [node bytes]    — opaque node payload
  [u32 crc32]     — CRC32 covering all preceding frame fields

Record 1:
  ...
```

The design choices:

- **Fixed 4 KiB shard header**: every shard file starts with a 4 KiB header
  carrying the magic `MTDB`, version `0x04`, pack ID, creation time, feature
  flags, and a header CRC. Record frames follow the header.

- **Caller-supplied structural IDs**: the API accepts a 16-byte node ID with
  each write and records it as index-rebuild metadata. The caller is responsible
  for deriving that ID and avoiding duplicate inserts; the storage engine does
  not calculate it from the payload.

- **CRC32 per record**: each frame carries a checksum. A scan detects a torn
  write or disk-sector error; recovery can truncate a torn tail at the last good
  record.

- **No payload deltas; opportunistic compression**: every record is
  self-contained, so reading a node never needs a data-delta chain traversal.
  The writer may use zstd only when it makes a frame smaller; otherwise it
  stores the payload raw. Separately, `index.delta` appends fixed-width index
  updates between full checkpoint rewrites.

- **Records are immutable once written**: active shards grow by appending
  complete frames; existing frames are never changed. Each room's immutable
  index/cache generation is published through `ArcSwap`. A repack copies its
  live frames into destination shards, publishes a replacement index, and only
  retires a source shard once no room still references it.

The `Record` struct in Rust:

```rust
pub struct Record {
    pub collection_id: [u8; 16],
    pub hash: [u8; 16],
    pub data: Bytes,
}
```

The frame on disk is
`len + flags + raw_len + collection_id + hash + data + crc32`: 45 bytes of
framing before any optional compression. For a typical 500-byte HAMT node, that
is about 9% overhead.

## The lossy fanout index

The shared shard pool is the durable store; the index is the fast path. Each
room gets a `LossyIndex` — a flat, power-of-two sized table of 64-bit slots:

```text
┌─────────────────────────────────────────────────────────────┐
│ IndexSlot (u64)                                             │
├──────────────┬────────────────┬─────────────────────────────┤
│ tag (24 bit) │ shard (12 bit) │ offset (28 bit)             │
│ fingerprint  │ shard ID       │ byte offset within shard    │
└──────────────┴────────────────┴─────────────────────────────┘
```

The slot is a single `u64`. Lookup is a single memory access — no pointer
chasing, no cache-line bouncing.

### How it works

The index uses **open addressing with linear probing**. The bucket is selected
by masking the top bits of the 16-byte hash:

```rust
fn bucket(&self, hash: &[u8; 16]) -> usize {
    let top_bytes = u64::from_be_bytes(hash[..8].try_into().unwrap());
    let masked = (top_bytes >> self.shift) & u64::from(self.mask);
    usize::try_from(masked).unwrap_or(usize::MAX)
}
```

The 24-bit tag is extracted from the hash and stored in the slot. It must be
derived from bits independent of those used for the initial bucket; otherwise a
matching tag adds no discrimination within a probe run. When probing:

1. Compute bucket from hash.
2. Read the slot. If empty, the key is absent — **empty terminates the probe**.
3. If the tag matches, return the `(shard_id, offset)` as a candidate.
4. Advance to the next bucket (linear probing).

The caller then verifies the candidate by reading the record from disk and
comparing the full 16-byte hash. Tag collisions surface as verification
failures, not silent wrong results.

### Why "lossy"

The index is lossy because:

- **24-bit tags** have a ~1/16M false-positive rate for each occupied slot
  examined. A collision means one wasted record read — the caller compares the
  full hash and continues probing if it doesn't match.

- **Empty terminates**: since the table is write-once with no deletions, empty
  slots are never tombstoned. A probe sequence is always bounded by the next
  empty slot. This means insertions must keep the table below 75% load to
  guarantee bounded probe lengths.

- **No exact match in the index**: the index is a fast filter, not an exact map.
  The full 16-byte hash comparison happens at read time against the record on
  disk.

### Memory cost

The raw slot is 8 bytes; the live index also carries its synchronization and
growth bookkeeping. Index memory therefore scales with the nodes in each active
room, rather than with the number of shared shard files.

### Slot layout efficiency

The 64-bit slot packs three fields with zero wasted bits:

| Field  | Bits | Range    | Purpose                           |
| ------ | ---- | -------- | --------------------------------- |
| tag    | 24   | 0–16M    | Fast rejection (zero is valid)    |
| shard  | 12   | 0–4095   | Which shared shard contains it    |
| offset | 28   | ~0–256MB | Byte offset within the shard (+1) |

The pool can address 4,096 shards of just under 256 MiB each — about 1 TiB in
total. Empty slots are the all-zero `u64`. Offsets are encoded as `offset + 1`,
so a valid slot with tag zero still has a nonzero offset field and is
distinguishable from empty.

## Topological repack

The shared shards are append-only, but a room's insertion order doesn't match
its read order. Events arrive out of order from federation, backfill fetches
history in reverse-chronological batches, and late-arriving events land at the
tail. A room's records can therefore be physically scattered through the pool.

The repacker fixes this. It is caller-scheduled, not a background service: the
engine exposes `needs_repack`, but nothing in `mtxdb-core` polls it or runs a
worker. The caller decides when to repack. It does for mtxdb what `git gc` does
for Git: rewrites reachable data in traversal order and reclaims garbage.

### The algorithm

1. **Walk the DAG** from the current root using BFS. The resolver function
   returns `(node_data, child_hashes)` for each hash encountered, then derives a
   topological ordering for the copied live set.

2. **Copy live nodes into destination shards** in topological traversal order.
   The late-arriving backfilled event that was physically distant from its
   historical parents is written near them in the replacement output.

3. **Atomic index swap**: fsync the dirty destination shards, then publish the
   room's replacement index via `ArcSwap`. A source shard is retired only after
   every room that references it has moved away.

4. **Logical GC**: nodes unreachable from the current root are simply not
   copied. A room with 4.5M accumulated HAMT nodes but only 1K reachable nodes
   drops 99.98% of its logical contents; physical space becomes reclaimable when
   no room still references the source shards.

### Why this might work (hypothesis, not yet benchmarked)

Content-addressing makes this trivially correct. The repacker doesn't need to
know what a node contains — it just follows hashes. Repacking is logically
per-room, even though its physical destination shards are shared; a batch repack
can compact several rooms through one output stream.

Near-sequential graph walks after repack remain a hypothesis: the harness below
measures bulk write, reopen, point lookup, and append latency. It does not
measure a Matrix DAG traversal before/after topological repack (seek count or
wall time). Until that traversal benchmark exists on real room histories, treat
locality as the prize to measure, not a demonstrated result.

### The branching caveat

A DAG is not a tree. An event with two `prev_events` from diverging federation
branches has both parents somewhere earlier in the file, but they can't both be
adjacent to it. Repacking converts _most_ of a walk into sequential reads, with
a residue proportional to the DAG's branching factor. Most Matrix rooms are
close to linear most of the time, so the residue is small.

Order the repack sequence by the walk you perform most: reverse-topological from
current extremities (for `/sync` and backfill). Accept that complex `auth_chain`
traversals will still incur some seeks.

## The storage engine trait

mtxdb abstracts the backend behind a trait, so the packfile, index, cache, and
repack code don't depend on a specific engine:

```rust
pub trait StorageEngine: Send + Sync {
    fn get(&self, room_id: &[u8; 16], id: &NodeId)
        -> Result<Option<NodeData>, StorageError>;
    fn get_many(&self, room_id: &[u8; 16], ids: &[NodeId])
        -> Result<Vec<Option<NodeData>>, StorageError>;

    fn put(&self, room_id: &[u8; 16], id: &NodeId, data: &NodeData)
        -> Result<(), StorageError>;
    fn put_many(&self, room_id: &[u8; 16], entries: &[(NodeId, NodeData)])
        -> Result<(), StorageError>;

    fn delete_collection(&self, room_id: &[u8; 16])
        -> Result<(), StorageError>;

    fn sync(&self) -> Result<(), StorageError>;

    fn refresh_collection(&self, room_id: &[u8; 16])
        -> Result<(), StorageError>;
}
```

Every operation is scoped to a single room. The caller always knows which room a
node belongs to; the engine uses this to select the correct per-room index and
cache, then follows its slot to a shared shard. This keeps each room's active
index separate while the physical files are pooled.

The `PackfileStorage` implementation holds:

- A `LossyIndex` per room (in-memory, 64-bit slots).
- One shared `ShardPool` (the append-only files on disk).
- A `NodeCache` for recently accessed nodes (avoids repeated disk reads).

The `InMemoryStorage` implementation is a `HashMap<NodeId, NodeData>` for tests.

### The `NodeRef` swizzle

Inspired by LeanStore, the crate defines a `NodeRef` enum that can be either
lazy (just an ID, disk fetch needed) or resolved (data in hand):

```rust
pub enum NodeRef {
    Lazy(NodeId),
    Resolved(NodeId, Arc<NodeData>),
}
```

This lets callers defer disk reads until the data is actually needed, and cache
resolved nodes for the duration of a traversal without extra allocations.

## Benchmarks and trade-offs

### External-engine benchmark

> Topology asymmetry (applies to every external table below): mtxdb writes each
> batch across 32 collections, while Fjall/MDBX/SQLite write one partition/table
> in this harness. That explains a meaningful part of any append-path gap — do
> not read these as same-shape workloads.

The external benchmark compares mtxdb with libmdbx, SQLite, and Fjall on a
generated workload. The figures below are from one benchmark host, not a claim
about every disk or production Matrix workload. The run used an Intel Core
i5-8600K (3.60 GHz), 32 GB of DDR4-2133 memory, and the repository on a 3.6 TB
Seagate ST4000NM0115 SATA HDD. The system volume was a 256 GB Crucial MX300 SATA
SSD. Filesystem, library-version, durability-setting, and cache-state details
also matter when reproducing the results.

The sweep below covers 0.0625, 0.125, 0.25, 0.5, and 1.0 GB (with a repeated
0.0625 GB sample), testing all three mtxdb checksum modes plus libmdbx, SQLite,
and Fjall at every size — including 0.5 and 1.0 GB. `full crc32` is mtxdb's
default and is the directly relevant comparison. The harness names the middle
mode `writeonly` (CRCs written but not re-verified on read); the tables below
use the harness names verbatim.

All sweep tables below come from one captured run sequence on the host above:

```shell
for gb in 0.0625 0.0625 0.125 0.25 0.5 1.0; do
  MTXDB_BENCH_EXT_GB=$gb python scripts/external_bench.py
done
```

The 0.0625 GB tables show the second (repeat) run; the first run's bulk-write
figures were mtxdb 66.1 / 68.8 / 68.5 ms (none/writeonly/full), mdbx 177.8 ms,
fjall 250.9 ms, sqlite 1493.9 ms — the repeat differed by a few percent, except
Fjall's first-append (1.57 ms first run vs 2.24 ms repeat), which is itself a
useful variance signal. Timings vary between invocations on this host; treat
every table as one captured observation, not a stable ranking.

Memory columns: `Index (MB)` is not comparable across engines — for mtxdb it is
measured in-memory index bytes, while for Fjall/MDBX/SQLite the harness reports
file bytes as a proxy (shown as `in-file`, unbolded). Compare memory across
engines with `RAM open (MB)` / `RAM warm (MB)` (PSS), not with `Index (MB)`.
Disk figures the harness printed in GB are converted to MB (×1024) and rounded.

The harness also has a separate default 0.1 GB `make bench` target that is not
part of this sweep capture; it is not shown here pending its own versioned
capture. An earlier draft of this post showed a 0.1 GB table with two
unexplained Fjall rows, one of which exactly duplicated this sweep's 0.0625 GB
Fjall numbers — that table has been removed until the 0.1 GB target has its own
versioned capture.

#### Sustained-write tail (512 MB, exploratory)

With `MTXDB_BENCH_SUSTAINED=1` and `MTXDB_BENCH_SUSTAINED_MB=512`, six
subprocess runs completed with `VERIFY_OK=true`. This phase reports batch-tail
latency, which the size-sweep table does not capture. At 512 MB there are only
about 125 durable batches — useful exploratory data, but not enough for robust
p99 or "tightest spread" claims. Per-run samples/variance are not shown here;
repeat with randomized order before treating any tail gap as stable. Same
topology asymmetry as above (mtxdb over 32 collections, others over one
partition/table).

<!-- markdownlint-disable MD013 -->

| Engine | Throughput (records/s) | p50 (ms) | p95 (ms) |  p99 (ms) | Reopen (ms) |
| :----: | ---------------------: | -------: | -------: | --------: | ----------: |
| mtxdb  |               ~590,000 |  6.2–6.5 | 8.6–12.0 | 18.2–18.5 |       15–18 |
| fjall  |                438,000 |      8.3 |     15.3 |      20.3 |        57.9 |
|  mdbx  |                159,000 |     20.2 |     48.9 |      51.4 |         1.6 |
| sqlite |                 23,000 |      187 |      202 |       213 |         0.1 |

<!-- markdownlint-enable MD013 -->

At this volume on this host, mtxdb's observed p50-to-p99 range was narrower than
MDBX's in these runs; Fjall's dominant observed cost was reopen time, and SQLite
was consistently slow but comparatively flat. Treat this as an initial one-host
observation, not a universal performance guarantee and not a robust p99
comparison.

#### ── At 0.0625 GB ──────────────────────────────────────────────────────────

<!-- markdownlint-disable MD013 -->

|   Engine    |   CRC32   | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) |  Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :---------: | :-------: | --------------: | -------------: | ---------------: | ----------------: | ----------------: | --------------: | -----------------: | ---------------: | ---------: | ---------: | ------------: | ------------: |
|    mtxdb    |   none    |      **_65.0_** |          0.082 |      **_0.113_** |              0.32 |        **_1.65_** |            0.13 |               0.72 |             0.13 | **_63.2_** |        3.0 |           6.0 |          69.2 |
|    mtxdb    | writeonly |            67.8 |    **_0.080_** |            0.114 |        **_0.31_** |              1.71 |      **_0.12_** |               0.74 |             0.13 | **_63.2_** |        3.0 |           6.0 |          69.2 |
|    mtxdb    |   full    |            67.9 |          0.165 |            0.212 |              0.45 |              1.66 |      **_0.12_** |               0.72 |       **_0.12_** | **_63.2_** |        3.0 |           7.0 |          69.2 |
|    mdbx     |    n/a    |           174.7 |          0.499 |            0.478 |              0.50 |             14.27 |            1.16 |               1.67 |             0.72 |      112.0 |    in-file |           1.9 |         113.0 |
|   sqlite    |    n/a    |          1535.5 |          0.102 |            0.119 |             26.49 |             38.36 |            1.25 |               9.58 |             0.78 |      272.9 |    in-file |     **_1.7_** |     **_3.5_** |
| fjall (lsm) |    n/a    |           250.8 |          1.753 |            3.414 |              3.08 |              2.24 |            1.60 |         **_0.63_** |             0.43 |       94.1 |    in-file |          70.3 |          70.3 |

<!-- markdownlint-enable MD013 -->

Topology asymmetry: mtxdb spreads batches over 32 collections; others use one
partition/table. Compare memory with `RAM open/warm (PSS)`, not `Index (MB)`.

#### ── At 0.125 GB ───────────────────────────────────────────────────────────

<!-- markdownlint-disable MD013 -->

|   Engine    |   CRC32   | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) |   Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :---------: | :-------: | --------------: | -------------: | ---------------: | ----------------: | ----------------: | --------------: | -----------------: | ---------------: | ----------: | ---------: | ------------: | ------------: |
|    mtxdb    |   none    |     **_128.6_** |    **_0.081_** |      **_0.110_** |        **_0.30_** |              3.12 |      **_0.11_** |               1.07 |       **_0.13_** | **_126.5_** |        6.0 |           5.8 |         111.8 |
|    mtxdb    | writeonly |           139.7 |    **_0.081_** |            0.111 |        **_0.30_** |              3.22 |            0.13 |               1.25 |             0.16 | **_126.5_** |        6.0 |           5.7 |         111.7 |
|    mtxdb    |   full    |           137.6 |          0.273 |            0.321 |              0.46 |              3.09 |            0.12 |               1.10 |       **_0.13_** | **_126.5_** |        6.0 |           7.7 |         111.7 |
|    mdbx     |    n/a    |           394.8 |          0.599 |            0.541 |              0.55 |             18.84 |            1.32 |               2.07 |             0.78 |       224.0 |    in-file |           1.9 |         223.9 |
|   sqlite    |    n/a    |          3318.2 |          0.106 |            0.112 |             28.62 |             42.36 |            1.35 |              10.60 |             0.81 |       545.6 |    in-file |     **_1.7_** |     **_3.5_** |
| fjall (lsm) |    n/a    |           500.9 |          5.342 |            3.359 |              3.82 |        **_1.20_** |            0.86 |         **_0.31_** |             0.23 |       156.2 |    in-file |         138.3 |         138.3 |

<!-- markdownlint-enable MD013 -->

Topology asymmetry: mtxdb spreads batches over 32 collections; others use one
partition/table. Compare memory with `RAM open/warm (PSS)`, not `Index (MB)`.

#### ── At 0.25 GB ────────────────────────────────────────────────────────────

<!-- markdownlint-disable MD013 -->

|   Engine    |   CRC32   | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) |   Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :---------: | :-------: | --------------: | -------------: | ---------------: | ----------------: | ----------------: | --------------: | -----------------: | ---------------: | ----------: | ---------: | ------------: | ------------: |
|    mtxdb    |   none    |     **_276.7_** |    **_0.080_** |            0.113 |        **_0.33_** |              4.88 |      **_0.12_** |               2.20 |             0.18 | **_252.9_** |       12.0 |           9.3 |         117.2 |
|    mtxdb    | writeonly |           296.2 |    **_0.080_** |            0.112 |        **_0.33_** |              4.57 |      **_0.12_** |               2.13 |       **_0.17_** | **_252.9_** |       12.0 |          10.0 |         117.8 |
|    mtxdb    |   full    |           289.7 |          0.493 |            0.549 |              0.47 |              5.59 |      **_0.12_** |               2.17 |       **_0.17_** | **_252.9_** |       12.0 |           9.8 |         113.7 |
|    mdbx     |    n/a    |           963.8 |          0.771 |            0.747 |              0.74 |             27.55 |            1.43 |               2.18 |             0.78 |       448.0 |    in-file |           1.9 |         445.4 |
|   sqlite    |    n/a    |          7107.3 |          0.101 |      **_0.109_** |             29.51 |             42.79 |            1.48 |              10.95 |             0.78 |       ~1126 |    in-file |     **_1.8_** |     **_3.6_** |
| fjall (lsm) |    n/a    |          1001.1 |          6.177 |           11.594 |              4.36 |        **_2.08_** |            1.49 |         **_0.54_** |             0.39 |       280.4 |    in-file |         275.4 |         275.4 |

<!-- markdownlint-enable MD013 -->

Topology asymmetry: mtxdb spreads batches over 32 collections; others use one
partition/table. Compare memory with `RAM open/warm (PSS)`, not `Index (MB)`.
SQLite disk printed as 1.1 GB in the capture; converted to ~1126 MB.

#### ── At 0.5 GB ─────────────────────────────────────────────────────────────

<!-- markdownlint-disable MD013 -->

|   Engine    |   CRC32   | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) |   Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :---------: | :-------: | --------------: | -------------: | ---------------: | ----------------: | ----------------: | --------------: | -----------------: | ---------------: | ----------: | ---------: | ------------: | ------------: |
|    mtxdb    |   none    |     **_592.8_** |          0.087 |            0.124 |        **_0.33_** |              7.84 |      **_0.12_** |               4.17 |             0.19 | **_505.8_** |       24.0 |           9.6 |         121.6 |
|    mtxdb    | writeonly |           615.0 |    **_0.082_** |            0.112 |        **_0.33_** |              7.36 |      **_0.12_** |               4.16 |             0.19 | **_505.8_** |       24.0 |           2.5 |         114.4 |
|    mtxdb    |   full    |           606.4 |          0.919 |            0.943 |              0.48 |              7.55 |            0.16 |               4.00 |       **_0.18_** | **_505.8_** |       24.0 |          10.5 |         114.4 |
|    mdbx     |    n/a    |          2636.3 |          1.146 |            1.111 |              0.98 |             36.67 |            1.51 |               2.40 |             0.81 |       896.0 |    in-file |     **_1.8_** |         863.0 |
|   sqlite    |    n/a    |         15453.9 |          0.105 |      **_0.108_** |             32.54 |             46.12 |            1.26 |              11.46 |             0.77 |       ~2150 |    in-file |     **_1.8_** |     **_3.6_** |
| fjall (lsm) |    n/a    |          2022.5 |         12.101 |           24.654 |              4.33 |        **_2.37_** |            1.69 |         **_0.57_** |             0.41 |       528.8 |    in-file |         540.9 |         540.9 |

<!-- markdownlint-enable MD013 -->

Topology asymmetry: mtxdb spreads batches over 32 collections; others use one
partition/table. Compare memory with `RAM open/warm (PSS)`, not `Index (MB)`.
SQLite disk printed as 2.1 GB in the capture; converted to ~2150 MB.

#### ── At 1.0 GB ─────────────────────────────────────────────────────────────

<!-- markdownlint-disable MD013 -->

|   Engine    |   CRC32   | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) |    Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :---------: | :-------: | --------------: | -------------: | ---------------: | ----------------: | ----------------: | --------------: | -----------------: | ---------------: | -----------: | ---------: | ------------: | ------------: |
|    mtxdb    |   none    |    **_1428.6_** |          0.095 |            0.125 |              0.35 |             15.22 |      **_0.12_** |               9.46 |       **_0.18_** | **_1011.6_** |       48.0 |           5.3 |         125.3 |
|    mtxdb    | writeonly |          1481.3 |    **_0.094_** |            0.129 |        **_0.34_** |             13.06 |      **_0.12_** |               8.57 |       **_0.18_** | **_1011.6_** |       48.0 |          26.4 |         146.3 |
|    mtxdb    |   full    |          1510.8 |          1.732 |            1.828 |              0.49 |             11.55 |      **_0.12_** |               8.52 |       **_0.18_** | **_1011.6_** |       48.0 |          45.4 |         149.3 |
|    mdbx     |    n/a    |         22029.2 |          1.886 |            1.882 |              1.35 |             63.44 |            1.76 |               2.75 |             0.86 |        ~1741 |    in-file |     **_1.8_** |          1434 |
|   sqlite    |    n/a    |         33834.6 |          0.126 |      **_0.109_** |             34.11 |             49.17 |            1.46 |              12.57 |             0.84 |        ~4403 |    in-file |     **_1.8_** |     **_3.7_** |
| fjall (lsm) |    n/a    |          4248.5 |         23.916 |           26.494 |              4.40 |        **_1.28_** |            0.90 |         **_0.32_** |             0.23 |        ~1024 |    in-file |          1126 |          1126 |

<!-- markdownlint-enable MD013 -->

Topology asymmetry: mtxdb spreads batches over 32 collections; others use one
partition/table. Compare memory with `RAM open/warm (PSS)`, not `Index (MB)`. GB
displays in the capture converted to MB (×1024, rounded): mdbx disk ~1741 and
RAM warm ~1434, fjall disk ~1024 and RAM ~1126, sqlite disk ~4403.

---

`none` disables frame and checkpoint CRC32 generation and verification (fastest,
least safe); `writeonly` writes CRCs but skips their read-time verification;
`full` verifies frame CRCs on every read and is the engine's actual default.
libmdbx, SQLite, and Fjall have no equivalent engine-level read-time checksum
sweep in this benchmark. Across this 0.0625–1.0 GB capture, even the default
`full` mode writes the initial data set faster than libmdbx, SQLite, and Fjall
and uses less disk than every other engine at every size. Its explicit in-memory
index grows with the data set; mdbx and SQLite keep their index structures in
their database files, and SQLite holds the lowest PSS in every table. On first
append, mtxdb beats MDBX and SQLite at every sampled size; against Fjall it is
mixed — Fjall is faster at 0.125–1.0 GB, while the repeated 0.0625 GB sample
split (Fjall 1.57 ms first run vs 2.24 ms repeat, mtxdb ~1.65 ms), so neither
engine owns that cell. Fjall wins steady-append at every size. These figures
need repeated controlled measurements with representative Matrix data before
supporting a broader claim.

The publishable reading so far: append-only writes and persisted-index reopen
look promising on this HDD benchmark; graph-locality benefits remain to be
measured on real Matrix histories.

### What mtxdb buys you

<!-- markdownlint-disable MD013 -->

| Operation        | B-tree (Synapse)    | mtxdb                                                                      |
| ---------------- | ------------------- | -------------------------------------------------------------------------- |
| Point lookup     | Tree traversal      | `O(1)` index probe + record read                                           |
| State resolution | Often scattered I/O | Hypothesized near-sequential after a workload-specific repack (unmeasured) |
| Event ingestion  | Read-modify-write   | Append-only                                                                |
| GC               | Tombstone + compact | Caller-scheduled reachability repack                                       |
| Crash recovery   | WAL replay          | Validate checkpoints or rescan shard frames                                |

<!-- markdownlint-enable MD013 -->

### What it costs

- **Write amplification**: the repacker rewrites reachable data. For a room with
  99.9% garbage, this is a net win. For a room that's mostly live data, the
  repack is nearly a full rewrite for minimal reclamation.

- **No random writes**: you can't update a node in place. If you need to change
  state, you append a new version and the old one becomes garbage. This is fine
  for Matrix (state is immutable per state group) but wouldn't work for a
  mutable key-value store.

- **Tag collisions at 24 bits**: about one in 16M occupied slots examined. The
  expected wasted reads depend on the probe length, not the total nodes in the
  room. At a controlled load factor, that remains negligible; it should still be
  counted in benchmark instrumentation.

- **Shared-shard coupling**: physical files and file descriptors scale with the
  number of shards, not directly with room count. That avoids a file per room,
  but rooms that share a source shard can make retirement and physical
  compaction a multi-room operation. Per-room indexes and caches still grow with
  the active-room count.

### The measurement gate

Before optimizing further, instrument the read budget: what percentage of disk
seeks are for HAMT nodes vs. PDU bodies vs. graph edges? If graph edges dominate
`/sync` and backfill, a flat CSR sidecar for `prev_events` and `auth_events`
(like Git's commit-graph file) matters more than the packfile. If HAMT node
fetches dominate state resolution, the packfile is the right investment.

The sidecar would be a flat, mmap-able file of fixed-width records:
`local_id → (prev_range, auth_range, depth)`. For 1M events at ~32 bytes plus
edges, that is ~50MB per large room — trivially mmap-able, and graph walks
become linear scans with zero PDU reads.

## Roadmap

Implemented so far: packfile format, lossy fanout index, `StorageEngine` trait,
`NodeCache`, caller-scheduled topological repacker (`needs_repack` is exposed;
no background worker polls it yet).

Not built:

- **POPCOUNT-indexed HAMT/CHAMP trie.** The index above is a flat hash table —
  no bitmap, no `count_ones()`. The real thing (bitmap child-index, `O(1)`
  descent) exists as a proof of concept in a sibling project, `rezzy`; needs its
  own crate before mtxdb can depend on it.
- **WAL, transactions, snapshots, backups, repair.** The current durability path
  syncs dirty shards and persists index checkpoint/delta metadata; it is not a
  transactional WAL design.
- **Segment/bulk queries** beyond `get_many()`.
- **A RocksDB benchmark.** The external benchmark currently covers mtxdb,
  libmdbx, SQLite, and Fjall. Any claim about RocksDB remains unverified.

---

mtxdb is early — the `StorageEngine` trait and packfile format are implemented,
the lossy index is tested, and the caller-scheduled repack handles atomic index
swaps. The repository is at
[github.com/Wombat-Foundation/mtxdb](https://github.com/Wombat-Foundation/mtxdb).

### Sources consulted

General vendor spec sheets and independent storage benchmarks (with stated
workload, queue depth, and block size) for sequential vs. random I/O magnitudes.
The sequential-vs-random table above is illustrative, not a measurement of this
workload.
