---
title: "[DRAFT] mtxdb: A write-once packfile storage engine for Matrix"
date: "2026-09-05"
description: "Making Matrix possible on spinning disks."
---

Matrix homeservers can store gigabytes of room data. The event DAG for a busy
room may accumulate millions of HAMT nodes over its lifetime, most of them
unreachable from the latest state after subsequent transitions. Traditional
B-tree storage engines are optimized for mutable records; their write paths can
involve read-modify-write cycles and random I/O that are especially costly on
spinning disks.

[mtxdb](https://github.com/Wombat-Foundation/mtxdb) takes the opposite approach:
**never mutate individual records.** New records are appended; a collection can
be deleted logically, and physical space is reclaimed by a background repack
that rewrites reachable data in a chosen traversal order. The result is a
storage engine with constant-time _in-memory_ index probes and a layout that can
make selected graph walks much more sequential.

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
enormous, even on modern hardware.

<!-- markdownlint-disable MD013 -->

| Drive Type     | Sequential Read | Random 4K Read | Gap   |
| -------------- | --------------- | -------------- | ----- |
| Gen 5 NVMe SSD | ~13,000 MB/s    | ~80–100 MB/s   | ~140× |
| Gen 4 NVMe SSD | ~7,000 MB/s     | ~70–80 MB/s    | ~90×  |
| SATA SSD       | ~550 MB/s       | ~40–50 MB/s    | ~12×  |
| HDD            | ~150 MB/s       | ~0.5–1 MB/s    | ~200× |

<!-- markdownlint-enable MD013 -->

A Gen 4 NVMe SSD advertising 7,000 MB/s on the box can deliver far less when a
workload reads thousands of small, scattered files. The drive is not broken:
sequential bandwidth and random-I/O throughput are different measurements, and
the latter also depends on queue depth, block size, firmware, and the host.

([Source](https://www.oscooshop.com/blogs/blogs/ssd-sequential-vs-random-speed))

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
[MAGIC: "MDB1"] [version: 0x01]

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
comparing the full 16-byte hash. Tag collisions (at 24 bits, ~1 in 16M per
probe) surface as verification failures, not silent wrong results.

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

| Field  | Bits | Range    | Purpose                        |
| ------ | ---- | -------- | ------------------------------ |
| tag    | 24   | 0–16M    | Fast rejection (0 = empty)     |
| shard  | 12   | 0–4095   | Which shared shard contains it |
| offset | 28   | ~0–256MB | Byte offset within the shard   |

The pool can address 4,096 shards of just under 256 MiB each — about 1 TiB in
total. Empty slots are all-zeros, and since hash values are uniformly random,
the probability of a legitimate hash mapping to tag 0 is 1/16M —
indistinguishable from "not present" in practice.

## Topological repack

The shared shards are append-only, but a room's insertion order doesn't match
its read order. Events arrive out of order from federation, backfill fetches
history in reverse-chronological batches, and late-arriving events land at the
tail. A room's records can therefore be physically scattered through the pool.

The repacker fixes this. It runs in the background during idle periods and does
for mtxdb what `git gc` does for Git: rewrites reachable data in traversal order
and reclaims garbage.

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

### Why this works

Content-addressing makes this trivially correct. The repacker doesn't need to
know what a node contains — it just follows hashes. Repacking is logically
per-room, even though its physical destination shards are shared; a batch repack
can compact several rooms through one output stream.

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

The external benchmark compares mtxdb with libmdbx, SQLite, and Fjall for the
same generated workload. The figures below are from one benchmark host, not a
claim about every disk or production Matrix workload. The run used an Intel Core
i5-8600K (3.60 GHz), 32 GB of DDR4-2133 memory, and the repository on a 3.6 TB
Seagate ST4000NM0115 SATA HDD. The system volume was a 256 GB Crucial MX300 SATA
SSD. Filesystem, library-version, durability-setting, and cache-state details
also matter when reproducing the results.

The latest sweep ran 0.0625, 0.125, 0.25, 0.5, and 1.0 GB (with a repeated
0.0625 GB sample), testing all three mtxdb checksum modes plus libmdbx, SQLite,
and Fjall at every size. `full crc32` is mtxdb's default and is the directly
relevant comparison. The tables below use the latest run; timings vary between
invocations on this host.

The current `make bench` harness also reports a separate default 0.1 GB target.
That run uses the write-only mtxdb mode and is shown separately because 0.1 GB
is not one of the sweep sizes below.

#### ── At 0.1 GB (make bench, latest run) ─────────────────────────────────────

<!-- markdownlint-disable MD013 -->

|   Engine    |   CRC32   | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) | Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :---------: | :-------: | --------------: | -------------: | ---------------: | ----------------: | ----------------: | --------------: | -----------------: | ---------------: | --------: | ---------: | ------------: | ------------: |
|    mtxdb    | writeonly |           113.6 |          0.333 |            0.248 |              0.32 |             29.47 |            3.87 |               0.52 |             0.15 |     105.4 |        3.1 |           4.1 |         108.6 |
|    mdbx     |    n/a    |           340.7 |          0.547 |            0.513 |              0.55 |              9.39 |            1.70 |               1.89 |             0.72 |     201.3 |        n/a |           5.7 |         192.3 |
|   sqlite    |    n/a    |          2791.1 |          0.106 |            0.129 |             29.61 |             41.38 |            1.35 |              10.69 |             0.87 |     457.8 |        n/a |          12.4 |          12.4 |
| fjall (lsm) |    n/a    |           250.9 |          2.243 |            2.414 |              3.24 |              1.57 |            1.12 |               0.41 |             0.30 |      94.1 |        n/a |          70.3 |          70.3 |
|    fjall    |    n/a    |           500.9 |          8.027 |            4.803 |              4.00 |              2.11 |            1.54 |               0.54 |             0.39 |     137.8 |        n/a |         120.4 |         120.4 |

<!-- markdownlint-enable MD013 -->

Fjall reports its on-disk footprint rather than a separately measured in-memory
index, like libmdbx and SQLite in this harness.

#### Sustained-write tail (512 MB)

With `MTXDB_BENCH_SUSTAINED=1` and `MTXDB_BENCH_SUSTAINED_MB=512`, six
subprocess runs completed with `VERIFY_OK=true`. This phase reports batch-tail
latency, which the size-sweep table does not capture:

<!-- markdownlint-disable MD013 -->

| Engine | Throughput (records/s) | p50 (ms) | p95 (ms) |  p99 (ms) | Reopen (ms) |
| :----: | ---------------------: | -------: | -------: | --------: | ----------: |
| mtxdb  |               ~590,000 |  6.2–6.5 | 8.6–12.0 | 18.2–18.5 |       15–18 |
| fjall  |                438,000 |      8.3 |     15.3 |      20.3 |        57.9 |
|  mdbx  |                159,000 |     20.2 |     48.9 |      51.4 |         1.6 |
| sqlite |                 23,000 |      187 |      202 |       213 |         0.1 |

<!-- markdownlint-enable MD013 -->

At this volume, mtxdb has the tightest p50-to-p99 spread; MDBX shows the worst
write tail, while Fjall's dominant cost is reopen time. SQLite is consistently
slow but comparatively flat. These are one-host observations, not universal
performance guarantees.

#### ── At 0.0625 GB ──────────────────────────────────────────────────────────

<!-- markdownlint-disable MD013 -->

|   Engine    | CRC32 | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) | Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :---------: | :---: | :-------------: | :------------: | :--------------: | :---------------: | :---------------: | :-------------: | :----------------: | :--------------: | :-------: | :--------: | :-----------: | :-----------: |
|    mtxdb    | none  |   **_67.0_**    |  **_0.079_**   |   **_0.107_**    |    **_0.31_**     |    **_1.82_**     |   **_0.13_**    |     **_0.47_**     |       0.14       | **_63_**  | **_3.0_**  |      5.7      |     68.9      |
|    mtxdb    | write |      69.9       |     0.083      |      0.126       |       0.34        |       1.88        |   **_0.13_**    |        0.48        |    **_0.13_**    | **_63_**  | **_3.0_**  |      5.8      |     68.9      |
|    mtxdb    | full  |      72.4       |     0.155      |      0.267       |       0.50        |       1.89        |      0.14       |        0.51        |       0.15       | **_63_**  | **_3.0_**  |      6.7      |     68.9      |
|    mdbx     |  n/a  |      209.1      |     0.478      |      1.039       |       0.65        |       22.39       |      1.39       |        1.80        |       0.77       |    112    |   _n/a_    |   **_1.5_**   |     112.6     |
|   sqlite    |  n/a  |     1622.6      |     0.125      |      0.112       |       28.77       |       41.43       |      1.44       |       10.46        |       0.91       |    273    |   _n/a_    |      1.9      |   **_3.7_**   |
| fjall (lsm) |  n/a  |      250.9      |     2.243      |      2.414       |       3.24        |       1.57        |      1.12       |        0.41        |       0.30       |   94.1    |    n/a     |     70.3      |     70.3      |

<!-- markdownlint-enable MD013 -->

#### ── At 0.125 GB ───────────────────────────────────────────────────────────

<!-- markdownlint-disable MD013 -->

|   Engine    | CRC32 | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) | Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :---------: | :---: | :-------------: | :------------: | :--------------: | :---------------: | :---------------: | :-------------: | :----------------: | :--------------: | :-------: | :--------: | :-----------: | :-----------: |
|    mtxdb    | none  |   **_135.1_**   |     0.082      |   **_0.113_**    |       0.34        |       3.77        |      0.13       |        0.46        |       0.13       | **_127_** | **_6.0_**  |      3.5      |     109.5     |
|    mtxdb    | write |      141.1      |  **_0.078_**   |   **_0.113_**    |    **_0.33_**     |       3.82        |      0.13       |        0.50        |       0.14       | **_127_** | **_6.0_**  |      3.5      |     109.4     |
|    mtxdb    | full  |      139.0      |     0.331      |      0.391       |       0.50        |    **_3.60_**     |   **_0.11_**    |     **_0.44_**     |    **_0.12_**    | **_127_** | **_6.0_**  |      5.6      |     109.6     |
|    mdbx     |  n/a  |      422.6      |     0.563      |      0.553       |       0.66        |       17.17       |      1.27       |        2.14        |       1.02       |    224    |   _n/a_    |   **_1.5_**   |     223.5     |
|   sqlite    |  n/a  |     3729.9      |     0.111      |      0.127       |       31.06       |       46.69       |      1.40       |       11.58        |       0.97       |    546    |   _n/a_    |      1.8      |   **_3.6_**   |
| fjall (lsm) |  n/a  |      500.9      |     5.342      |      3.359       |       3.82        |       1.20        |      0.86       |        0.31        |       0.23       |   156.2   |    n/a     |     138.3     |     138.3     |

<!-- markdownlint-enable MD013 -->

#### ── At 0.25 GB ────────────────────────────────────────────────────────────

<!-- markdownlint-disable MD013 -->

|   Engine    | CRC32 | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) | Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :---------: | :---: | :-------------: | :------------: | :--------------: | :---------------: | :---------------: | :-------------: | :----------------: | :--------------: | :-------: | :--------: | :-----------: | :-----------: |
|    mtxdb    | none  |   **_263.1_**   |     0.097      |      0.112       |    **_0.34_**     |       6.46        |   **_0.12_**    |     **_0.51_**     |       0.13       | **_253_** | **_12.0_** |      2.6      |     110.5     |
|    mtxdb    | write |      288.3      |  **_0.081_**   |      0.124       |       0.35        |    **_6.42_**     |      0.14       |        0.53        |       0.13       | **_253_** | **_12.0_** |      2.6      |     110.5     |
|    mtxdb    | full  |      291.1      |     0.698      |      0.581       |       0.50        |       6.46        |   **_0.12_**    |        0.54        |       0.14       | **_253_** | **_12.0_** |      6.6      |     110.5     |
|    mdbx     |  n/a  |     1163.3      |     0.739      |      0.716       |       0.83        |       24.42       |      1.39       |        2.30        |       0.91       |    448    |   _n/a_    |   **_1.5_**   |     444.9     |
|   sqlite    |  n/a  |     8196.5      |     0.115      |   **_0.110_**    |       33.16       |       44.77       |      1.29       |       11.71        |    **_0.87_**    |   1100    |   _n/a_    |      1.7      |   **_3.5_**   |
| fjall (lsm) |  n/a  |     1001.1      |     6.177      |      11.594      |       4.36        |       2.08        |      1.49       |        0.54        |       0.39       |   280.4   |    n/a     |     275.4     |     275.4     |

<!-- markdownlint-enable MD013 -->

#### ── At 0.5 GB ─────────────────────────────────────────────────────────────

<!-- markdownlint-disable MD013 -->

| Engine | CRC32 | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) | Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :----: | :---: | :-------------: | :------------: | :--------------: | :---------------: | :---------------: | :-------------: | :----------------: | :--------------: | :-------: | :--------: | :-----------: | :-----------: |
| mtxdb  | none  |   **_542.6_**   |  **_0.085_**   |   **_0.116_**    |    **_0.34_**     |       10.14       |      0.13       |     **_0.54_**     |    **_0.13_**    | **_506_** | **_24.0_** |      1.9      |     113.9     |
| mtxdb  | write |      567.3      |     0.086      |      0.128       |    **_0.34_**     |       10.11       |   **_0.12_**    |        0.57        |       0.14       | **_506_** | **_24.0_** |      2.0      |     113.9     |
| mtxdb  | full  |      563.5      |     0.947      |      0.928       |       0.50        |    **_10.03_**    |      0.14       |        0.59        |       0.14       | **_506_** | **_24.0_** |      9.9      |     113.8     |
|  mdbx  |  n/a  |     3344.5      |     0.413      |      0.365       |       1.33        |       38.48       |      2.00       |        2.84        |       1.01       |    896    |   _n/a_    |   **_1.4_**   |     865.4     |
| sqlite |  n/a  |     17195.1     |     0.116      |      0.184       |       36.92       |       55.11       |      1.50       |       13.82        |       0.92       |   2100    |   _n/a_    |      1.8      |   **_3.6_**   |

<!-- markdownlint-enable MD013 -->

#### ── At 1.0 GB ─────────────────────────────────────────────────────────────

<!-- markdownlint-disable MD013 -->

| Engine | CRC32 | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) | Disk (MB)  | Index (MB) | RAM open (MB) | RAM warm (MB) |
| :----: | :---: | :-------------: | :------------: | :--------------: | :---------------: | :---------------: | :-------------: | :----------------: | :--------------: | :--------: | :--------: | :-----------: | :-----------: |
| mtxdb  | none  |  **_1116.1_**   |     0.093      |      0.132       |       0.39        |       16.68       |   **_0.12_**    |     **_0.72_**     |    **_0.12_**    | **_1012_** | **_48.0_** |     13.9      |     133.9     |
| mtxdb  | write |     1190.1      |  **_0.091_**   |      0.125       |    **_0.35_**     |       16.76       |   **_0.12_**    |        0.74        |    **_0.12_**    | **_1012_** | **_48.0_** |     13.9      |     133.8     |
| mtxdb  | full  |     1197.3      |     1.977      |      2.255       |       0.53        |    **_16.27_**    |   **_0.12_**    |        0.75        |       0.13       | **_1012_** | **_48.0_** |     30.0      |     133.9     |
|  mdbx  |  n/a  |     24620.3     |     0.372      |      0.370       |       1.46        |       63.15       |      1.72       |        2.90        |       0.93       |    1700    |   _n/a_    |      3.1      |     1400      |
| sqlite |  n/a  |     36968.1     |     0.118      |   **_0.113_**    |       37.52       |       52.71       |      1.66       |       13.96        |       0.90       |    4300    |   _n/a_    |   **_1.8_**   |   **_3.6_**   |

<!-- markdownlint-enable MD013 -->

---

`none` disables frame CRC32 generation and read verification; `write` retains
frame CRCs but skips their read-time verification; `full` verifies frame CRCs on
every read. The benchmark keeps checkpoint CRCs written in every mode, but skips
their open-time verification for `none` and `write`. libmdbx and SQLite have no
equivalent engine-level read-time checksum sweep in this benchmark. Across this
0.0625–1.0 GB sweep, even the default `full` mode writes the initial data set
faster and uses less disk space than the other tested engines. Its explicit
in-memory index grows with the data set; mdbx and SQLite keep their index
structures in their database files. The first-append path is faster for mtxdb at
every sampled size. These figures need repeated controlled measurements with
representative Matrix data before supporting a broader claim.

### What mtxdb buys you

<!-- markdownlint-disable MD013 -->

| Operation        | B-tree (Synapse)    | mtxdb                                                   |
| ---------------- | ------------------- | ------------------------------------------------------- |
| Point lookup     | Tree traversal      | `O(1)` index probe + record read                        |
| State resolution | Often scattered I/O | Can be near-sequential after a workload-specific repack |
| Event ingestion  | Read-modify-write   | Append-only                                             |
| GC               | Tombstone + compact | Reachability repack                                     |
| Crash recovery   | WAL replay          | Validate checkpoints or rescan shard frames             |

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
`NodeCache`, topological repacker.

Not built:

- **POPCOUNT-indexed HAMT/CHAMP trie.** The index above is a flat hash table —
  no bitmap, no `count_ones()`. The real thing (bitmap child-index, `O(1)`
  descent) exists as a proof of concept in a sibling project; needs its own
  crate before mtxdb can depend on it.
- **WAL, transactions, snapshots, backups, repair.** The current durability path
  syncs dirty shards and persists index checkpoint/delta metadata; it is not a
  transactional WAL design.
- **Segment/bulk queries** beyond `get_many()`.
- **A RocksDB benchmark.** The external benchmark currently covers mtxdb,
  libmdbx, and SQLite. Any claim about RocksDB remains unverified.

---

mtxdb is early — the `StorageEngine` trait and packfile format are implemented,
the lossy index is tested, and the repack manager handles atomic swaps. The
repository is at
[github.com/Wombat-Foundation/mtxdb](https://github.com/Wombat-Foundation/mtxdb).
