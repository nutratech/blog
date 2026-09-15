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
**never mutate records, never delete records, never tombstone records.** Every
node is content-addressed and appended once. Garbage collection is a background
repack that rewrites only reachable data in a chosen traversal order. The result
is a storage engine with constant-time _in-memory_ index probes and a layout
that can make selected graph walks much more sequential.

Custom binary format, inspired by `libmdbx` and `LeanStore` (also `SplinterDB`,
`Fjall`, `git-repack`, and `PGM-index` — the last one mostly as a
counterexample; a learned index over uniformly random hashes degenerates to the
flat fanout table below, so that's just what's built). Compile times under 3
seconds. One `unsafe` block in the whole crate, for `mmap2` — reads make
effectively zero syscalls, mapped straight to their offset by the index below.
An index probe takes a few memory operations; a cache miss still pays for the
record read. The cache makes repeated accesses cheap, not literally free.

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
| Gen 4 NVMe SSD | ~7,000 MB/s     | ~70–80 MB/s    | ~90×  |
| Gen 5 NVMe SSD | ~13,000 MB/s    | ~80–100 MB/s   | ~140× |
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

mtxdb stores nodes in per-room packfiles. Each packfile is an append-only log of
framed records:

```text
[MAGIC: "MDB1"] [version: 0x01]

Record 0:
  [u32 len]       — byte length of (hash ++ node_bytes), little-endian
  [16-byte hash]  — structural hash (index-rebuild metadata only)
  [node bytes]    — opaque node payload
  [u32 crc32]     — CRC32 covering len + hash + node_bytes

Record 1:
  ...
```

The design choices:

- **Content-addressed**: the 16-byte structural hash is computed from the node
  bytes. Identical data always produces the same hash, so deduplication is free
  — just don't insert duplicates.

- **CRC32 per record**: each frame carries a checksum. A torn write or disk
  sector error is detected instantly during scan, and the packfile truncates at
  the last good record.

- **No deltas, no compression**: every record stores the full node bytes. This
  trades space for access simplicity — no delta chain traversal, no zlib
  inflation. On Matrix's state nodes (typically 200-2000 bytes each), the
  compression ratio is poor anyway.

- **Records are immutable once written**: the active packfile grows by appending
  complete frames; existing frames are never changed. Readers hold an
  `Arc<PackGeneration>` for the duration of a traversal. The repacker writes a
  new, complete pack, fsyncs it, renames it atomically, then swaps the room's
  pointer via `ArcSwap`. The old pack is unlinked when the last reader releases
  its `Arc`.

The `Record` struct in Rust:

```rust
pub struct Record {
    pub hash: [u8; 16],
    pub data: Bytes,
}
```

The frame on disk is `len(u32) + hash([u8;16]) + data(Bytes) + crc32(u32)`.
Total overhead per record: 24 bytes of framing. For a typical 500-byte HAMT
node, that is 4.8% overhead.

## The lossy fanout index

The packfile is the durable store; the index is the fast path. Each room gets a
`LossyIndex` — a flat, power-of-two sized table of 64-bit slots:

```text
┌─────────────────────────────────────────────────────────┐
│ IndexSlot (u64)                                         │
├──────────────┬──────────┬───────────────────────────────┤
│ tag (24 bit) │ pack (8) │ offset (32 bit)               │
│ fingerprint  │ pack id  │ byte offset within the pack   │
└──────────────┴──────────┴───────────────────────────────┘
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
3. If the tag matches, return the `(pack_id, offset)` as a candidate.
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

At 8 bytes per slot, a 1000-node room with a 2048-slot index costs 16KB. A
server with 100 active rooms costs 1.6MB total. The index for every room
combined fits in L2 cache.

### Slot layout efficiency

The 64-bit slot packs three fields with zero wasted bits:

| Field  | Bits | Range | Purpose                     |
| ------ | ---- | ----- | --------------------------- |
| tag    | 24   | 0–16M | Fast rejection (0 = empty)  |
| pack   | 8    | 0–255 | Which pack generation       |
| offset | 32   | 0–4GB | Byte offset within the pack |

This addresses 256 packs × 4GB each = 1TB per room. Empty slots are all-zeros,
and since hash values are uniformly random, the probability of a legitimate hash
mapping to tag 0 is 1/16M — indistinguishable from "not present" in practice.

## Topological repack

The packfile is append-only, but the insertion order doesn't match the read
order. Events arrive out of order from federation, backfill fetches history in
reverse-chronological batches, and late-arriving events land at the tail. The
packfile on disk is a jumble of chronological positions.

The repacker fixes this. It runs in the background during idle periods and does
for mtxdb what `git gc` does for Git: rewrites reachable data in traversal order
and reclaims garbage.

### The algorithm

1. **Walk the DAG** from the current root using BFS. The resolver function
   returns `(node_data, child_hashes)` for each hash encountered.

2. **Write a new packfile** with nodes in BFS traversal order. The late-arriving
   backfilled event that was stuck at the end of the old file is now physically
   written right next to its historical parents.

3. **Atomic swap**: fsync the new pack, rename it over the old path, then swap
   the room's `Arc<PackGeneration>` via `ArcSwap`. The old pack is unlinked when
   the last reader releases.

4. **GC for free**: nodes that are unreachable from the current root are simply
   not copied. A room with 4.5M accumulated HAMT nodes but only 1K reachable
   nodes reclaims 99.98% of the pack.

### Why this works

Content-addressing makes this trivially correct. The repacker doesn't need to
know what a node contains — it just follows hashes. If two rooms share a node
(the same HAMT subtree appears in both), the node is reachable from both roots
and will be copied into both repacked files. Deduplication across rooms happens
naturally.

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
    fn delete_room(&self, room_id: &[u8; 16])
        -> Result<(), StorageError>;
    fn sync(&self) -> Result<(), StorageError>;
}
```

Every operation is scoped to a single room. The caller always knows which room a
node belongs to; the engine uses this to select the correct per-room index and
packfile. This keeps each room's active index at ~8KB — 100 active rooms cost
less than 1MB total.

The `PackfileStorage` implementation holds:

- A `LossyIndex` per room (in-memory, 64-bit slots).
- A `PackGeneration` per room (the current packfile on disk).
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

`scripts/external_bench.py` compares mtxdb with libmdbx and SQLite for the same
generated workload. The figures below are from one benchmark host, not a claim
about every disk or production Matrix workload. The run used an Intel Core
i5-8600K (3.60 GHz), 32 GB of DDR4-2133 memory, and the repository on a 3.6 TB
Seagate ST4000NM0115 SATA HDD. The system volume was a 256 GB Crucial MX300 SATA
SSD. Filesystem, library-version, durability-setting, and cache-state details
also matter when reproducing the results.

The command ran each size once, testing all three mtxdb checksum modes. `full`
is mtxdb's default and is the directly relevant comparison.

<!-- markdownlint-disable MD013 -->

| Engine | CRC32 | Bulk write (ms) | Warm open (ms) | Check-point (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) | Disk (MB) | Index (MB) | RAM open (MB) | RAM warm (MB) |
| ------ | ----- | --------------- | -------------- | ---------------- | ----------------- | ----------------- | --------------- | ------------------ | ---------------- | --------- | ---------- | ------------- | ------------- |
| mtxdb  | none  | 99.8            | 0.079          | 0.127            | 0.30              | 29.13             | 3.91            | 0.44               | 0.12             | 100.6     | 3.0        | 5.8           | 106.3         |
| mtxdb  | write | 104.6           | 0.080          | 0.110            | 0.31              | 28.44             | 3.84            | 0.49               | 0.14             | 100.6     | 3.0        | 5.8           | 106.3         |
| mtxdb  | full  | 109.7           | 0.201          | 0.274            | 0.49              | 28.20             | 3.72            | 0.46               | 0.12             | 100.6     | 3.0        | 6.7           | 106.3         |
| mdbx   | n/a   | 336.4           | 0.537          | 0.515            | 0.55              | 9.21              | 1.36            | 1.81               | 0.73             | 192.0     | _embedded_ | 1.5           | 179.5         |
| sqlite | n/a   | 2689.8          | 0.103          | 0.112            | 29.22             | 42.35             | 1.37            | 10.54              | 0.87             | 436.6     | _embedded_ | 1.8           | 3.6           |

<!-- markdownlint-enable MD013 -->

At a 1 GB sample (`MTXDB_BENCH_EXT_GB=1`):

<!-- markdownlint-disable MD013 -->

| Engine | CRC32 | Bulk write (ms) | Warm open (ms) | Checkpoint (ms) | Point lookup (μs) | First append (ms) | First sync (ms) | Steady append (ms) | Steady sync (ms) | Disk (GB) | Index (MB) | RAM open | RAM warm |
| ------ | ----- | --------------- | -------------- | --------------- | ----------------- | ----------------- | --------------- | ------------------ | ---------------- | --------- | ---------- | -------- | -------- |
| mtxdb  | none  | 1047.3          | 0.095          | 0.131           | 0.34              | 15.37             | 0.12            | 0.74               | 0.13             | 1.01      | 48.0       | 13.9     | 133.9    |
| mtxdb  | write | 1150.3          | 0.097          | 0.133           | 0.34              | 15.17             | 0.12            | 0.73               | 0.12             | 1.01      | 48.0       | 14.0     | 133.8    |
| mtxdb  | full  | 1113.8          | 2.028          | 1.845           | 0.50              | 14.72             | 0.12            | 0.74               | 0.13             | 1.01      | 48.0       | 29.9     | 133.9    |
| mdbx   | n/a   | 26010.3         | 0.424          | 0.375           | 1.59              | 66.97             | 1.97            | 4.68               | 1.24             | 1.7       | _embedded_ | 5.4      | 1.4      |
| sqlite | n/a   | 43397.7         | 0.127          | 0.128           | 43.69             | 58.80             | 1.61            | 15.24              | 1.13             | 4.3       | _embedded_ | 1.7      | 3.5      |

<!-- markdownlint-enable MD013 -->

`none` disables both frame and checkpoint CRC32 checks; `write` writes CRCs but
does not re-verify them on reads; `full` verifies them on every read. libmdbx
and SQLite have no equivalent engine-level read-time checksum sweep in this
benchmark. At both sizes, even the default `full` mode writes the initial data
set faster and uses less disk space than the other tested engines. Its explicit
in-memory index grows with the data set; mdbx and SQLite keep their index
structures in their database files. The first-append path favors mdbx at 0.1 GB
and mtxdb in this single 1 GB sample. These figures need repeated controlled
measurements with representative Matrix data before supporting a broader claim.

### What mtxdb buys you

<!-- markdownlint-disable MD013 -->

| Operation        | B-tree (Synapse)    | mtxdb                                                   |
| ---------------- | ------------------- | ------------------------------------------------------- |
| Point lookup     | Tree traversal      | O(1) index probe + record read                          |
| State resolution | Often scattered I/O | Can be near-sequential after a workload-specific repack |
| Event ingestion  | Read-modify-write   | Append-only                                             |
| GC               | Tombstone + compact | Reachability repack                                     |
| Crash recovery   | WAL replay          | Scan last good rec                                      |

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

- **Per-room file proliferation**: each room gets its own pack set. A server in
  10K rooms needs 10K+ files. Mitigate with an LRU'd fd cache and a shared
  small-rooms pack for rooms below a size threshold.

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
- **WAL, transactions, snapshots, backups, repair.** Durability today is
  fsync-pack, fsync-rename. That's it.
- **Segment/bulk queries** beyond `get_many`.
- **A RocksDB benchmark.** The external benchmark currently covers mtxdb,
  libmdbx, and SQLite. Any claim about RocksDB remains unverified.

---

mtxdb is early — the `StorageEngine` trait and packfile format are implemented,
the lossy index is tested, and the repack manager handles atomic swaps. The
repository is at
[github.com/Wombat-Foundation/mtxdb](https://github.com/Wombat-Foundation/mtxdb).
