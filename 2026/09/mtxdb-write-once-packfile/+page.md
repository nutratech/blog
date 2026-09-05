---
title: "mtxdb: A write-once packfile storage engine for Matrix"
date: "2026-09-05"
description: "Making Matrix possible on spinning disks."
---

Matrix homeservers store terabytes of state data. The state DAG for a busy room
accumulates millions of HAMT nodes over its lifetime, most of them unreachable
after the next state transition. Traditional B-tree storage engines treat every
write as a mutation — read-modify-write cycles that scatter random seeks across
spinning platters, destroying IOPS.

[mtxdb](https://github.com/Wombat-Foundation/mtxdb) takes the opposite approach:
**never mutate, never delete, never tombstone.** Every node is content-addressed
and append-only. Garbage collection is a background repack that rewrites only
reachable data in traversal order. The result is a storage engine where reads
are either O(1) point lookups or pure sequential scans.

<!-- markdownlint-disable MD013 -->

[The problem](#the-problem) · [Packfile format](#packfile-format) ·
[The lossy fanout index](#the-lossy-fanout-index) ·
[Topological repack](#topological-repack) ·
[The storage engine trait](#the-storage-engine-trait) ·
[Benchmarks and trade-offs](#benchmarks-and-trade-offs)

<!-- markdownlint-enable MD013 -->

## The problem

Synapse's state storage has two hot paths:

1. **State resolution**: given a set of state groups, walk the DAG to find the
   current state. This is a graph traversal over `prev_events` and
   `auth_events`, touching hundreds of nodes per room.

2. **Event ingestion**: append a new event and its state snapshot. This is a
   sequential write — but the state snapshot is a content-addressed HAMT node
   that may reference thousands of historical nodes.

On SSDs, both paths are fast enough. On spinning disks — which is what most
self-hosted Matrix servers actually run — random seeks are catastrophic. A
single state resolution that touches 500 nodes at random offsets costs 500 × 8ms
seek time = 4 seconds. That is not a typo.

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

A Gen 4 NVMe SSD advertising 7,000 MB/s on the box? When the OS is booting and
reading thousands of small scattered files, it effectively operates at ~70–80
MB/s — roughly 1% of its headline speed. The drive isn't broken. The task is
random, and random performance has a completely different ceiling.

([Source](https://www.oscooshop.com/blogs/blogs/ssd-sequential-vs-random-speed))

The core insight is that **most of those 500 nodes are garbage**. A room with 1M
state events has produced ~4.5M HAMT nodes (4-5 per state change), but the
current-state closure is maybe a thousand. If you could read only the reachable
nodes in order, the entire operation becomes a single sequential scan.

## Packfile format

mtxdb stores nodes in per-room packfiles. Each packfile is an append-only log of
framed records:

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

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

- **Immutable once written**: a packfile is never modified after the header is
  written. Readers hold an `Arc<PackGeneration>` for the duration of a
  traversal. The repacker writes a new pack, fsyncs, renames atomically, then
  swaps the room's pointer via `ArcSwap`. The old pack is unlinked when the last
  reader releases its Arc.

The `Record` struct in Rust:

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

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

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

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

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

```rust
fn bucket(&self, hash: &[u8; 16]) -> usize {
    let top_bytes = u64::from_be_bytes(hash[..8].try_into().unwrap());
    let masked = (top_bytes >> self.shift) & u64::from(self.mask);
    usize::try_from(masked).unwrap_or(usize::MAX)
}
```

The 24-bit tag is extracted from the top of the hash and stored in the slot.
When probing:

1. Compute bucket from hash.
2. Read the slot. If empty, the key is absent — **empty terminates the probe**.
3. If the tag matches, return the `(pack_id, offset)` as a candidate.
4. Advance to the next bucket (linear probing).

The caller then verifies the candidate by reading the record from disk and
comparing the full 16-byte hash. Tag collisions (at 24 bits, ~1 in 16M per
probe) surface as verification failures, not silent wrong results.

### Why "lossy"

The index is lossy because:

- **24-bit tags** have a ~1/16M false-positive rate per probe. A collision means
  one wasted `pread` — the caller reads the record, computes the hash, and
  continues probing if it doesn't match.

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

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

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

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

```rust
pub enum NodeRef {
    Lazy(NodeId),
    Resolved(NodeId, Arc<NodeData>),
}
```

This lets callers defer disk reads until the data is actually needed, and cache
resolved nodes for the duration of a traversal without extra allocations.

## Benchmarks and trade-offs

### What mtxdb buys you

| Operation        | B-tree (Synapse)    | mtxdb               |
| ---------------- | ------------------- | ------------------- |
| Point lookup     | O(log n) seek       | O(1) index probe    |
| State resolution | N random seeks      | Sequential scan     |
| Event ingestion  | Read-modify-write   | Append-only         |
| GC               | Tombstone + compact | Reachability repack |
| Crash recovery   | WAL replay          | Scan last good rec  |

### What it costs

- **Write amplification**: the repacker rewrites reachable data. For a room with
  99.9% garbage, this is a net win. For a room that's mostly live data, the
  repack is nearly a full rewrite for minimal reclamation.

- **No random writes**: you can't update a node in place. If you need to change
  state, you append a new version and the old one becomes garbage. This is fine
  for Matrix (state is immutable per state group) but wouldn't work for a
  mutable key-value store.

- **Tag collisions at 24 bits**: ~1 in 16M per probe. A collision costs one
  wasted `pread` (8ms on a spinning disk). At 100K nodes per room, expect ~0.006
  wasted reads per lookup. At 1M nodes, ~0.06. Negligible.

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

---

mtxdb is early — the `StorageEngine` trait and packfile format are implemented,
the lossy index is tested, and the repack manager handles atomic swaps. The
repository is at
[github.com/Wombat-Foundation/mtxdb](https://github.com/Wombat-Foundation/mtxdb).
