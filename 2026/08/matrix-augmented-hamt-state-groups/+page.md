---
title: "Matrix: Faster State Groups via an Augmented HAMT"
date: "2026-08-13"
description:
  "Replacing delta-chain state groups with a content-addressed CHAMP trie keyed
  to MSC4500's LtHash16 accumulator — and what it looked like to rip the old
  design out of a real homeserver."
---

Every Matrix homeserver has to answer the same question, constantly: "what was
the state of this room at this point in the DAG?" The answer is a state group —
a snapshot of every `(type, state_key) -> event_id` mapping at that point — and
how you store and diff those snapshots turns out to be one of the
highest-leverage implementation decisions a homeserver can make.

Synapse-lineage engines store state groups as delta chains: each new group is a
small `O(1)` diff against its predecessor, cheap to write, but the chain has to
be replayed to materialize state, so every `MAX_STATE_DELTA_HOPS` events (100,
by default) the engine pays a full `O(S)` snapshot write to keep read latency
from degrading. Conduit-lineage engines went a different direction and hashed
locally-assigned short IDs into a `shortstatehash` — cheap to compare, but the
hash is only ever comparable against another hash produced by the same server's
own ID allocation. Two servers that independently resolve to byte-identical
state still can't recognize that fact without materializing both and diffing
them.

Neither defect is really about the hashing or the chaining — it's that the
identity of a state group is tied to something local: an auto-incrementing
integer, or a hash built on top of one. This post is about replacing that
identity with a content-derived one, and backing it with a data structure that
makes the identity nearly free to compute: an **augmented HAMT**, keyed to the
`LtHash16` accumulator from
[MSC4500](/blog/posts/2026/07/matrix-state-accumulators).

## Structure: CHAMP, not a plain HAMT

The state map lives in a 32-way CHAMP-style (Compressed Hash Array Mapped
Prefix) trie — a HAMT variant where leaf entries are inlined directly into their
parent node instead of being boxed as one-entry child nodes. For the shallow,
high-fan-out shape of Matrix room state, that inlining is the whole point: it
keeps node density and cache behavior sane. Ordering canonicality is a nice side
effect, not the reason to pick CHAMP here.

Every internal node carries two 32-bit bitmaps — `datamap` for inlined entries,
`nodemap` for child nodes — and a cached subtree digest used to skip unchanged
branches during diffing:

```rust
pub struct HamtNode<K, V> {
    /// Bitmap marking which of the 32 slots contain leaf data.
    pub datamap: u32,
    /// Bitmap marking which of the 32 slots contain child internal nodes.
    pub nodemap: u32,
    pub leaves: Vec<(K, V)>,
    pub children: Vec<NodeRef<K, V>>,
    /// Structural hash for O(1) subtree equivalence checks.
    pub structural_hash: StructuralHash,
}
```

That `structural_hash` is deliberately _not_ the wire-facing digest. It's a
128-bit `BLAKE2b` output keyed with a per-server secret — cheap at 16
bytes/node, and closed against grinding because an attacker minting state events
in a shared room can't target a digest built with a key they don't have. A
64-bit unkeyed hash would be gameable in ~2³² work by anyone who can author
state in the room, and a false subtree match isn't just a slow path — it's a
correctness hazard: the diff algorithm below would silently skip a subtree that
actually differs and produce a wrong delta.

The root is the one place this default gets overridden. It caches the true,
_unkeyed_ `LtHash16` lattice, because the root is the thing that has to be
comparable across servers — that's the entire point of building this on top of
MSC4500. Internal-node digests only ever get compared against the same server's
own cache after a reload; the root gets compared against another homeserver's
accumulator entirely. Alongside the 2048-byte lattice, the root also caches
`BLAKE2b-256(lattice)` — the same collapse MSC4500 already defines — so the
common case is a 32-byte equality check, not a 2048-byte one.

## The state group ID falls out for free

Because `LtHash` is homomorphic, the root lattice after adding one event and
removing another is just `previous_lattice - removed + added` — an `O(1)`
update, no re-hash of the whole state map required. That lattice, or rather its
`BLAKE2b-256` collapse, _is_ the state group ID:

```rust
pub type StructuralHash = [u8; 16]; // local-only, keyed
pub type StateGroupId = [u8; 32];   // cross-server, from LtHash

pub fn state_group_id_from_lthash(lattice: &LtHash) -> StateGroupId {
    lattice.checksum() // BLAKE2b-256(lattice)
}
```

This is what actually fixes branch obliviousness. If two independent forks —
say, a network partition that gets healed, or two admins racing a kick — end up
resolving to the exact same state, their root lattices collide by construction,
and the server deduplicates the state group without ever materializing or
comparing either one. A `shortstatehash` scheme can't do this across a restart,
let alone across servers, because its inputs are local IDs. A sort-then-hash
scheme _could_ do it, but pays `O(S log S)` every time to find out.

## Delta isolation without decompressing a chain

State resolution and DAG healing spend most of their time asking "what's
different between state map A and state map B?" With two persistent HAMTs,
that's a three-tier check, cheapest first:

1. **Pointer identity.** If `A'` and `B'` are the same `Arc` — same process,
   same in-memory trie — skip the subtree in `O(1)`. This only pays off because
   equal content reliably produces equal node objects, which requires canonical
   shape (more on that below).
2. **Structural hash.** Across a process boundary or a reload, compare the
   cached 128-bit digests. Match means skip. This is where the keyed hash earns
   its cost.
3. **Deep diff.** Only on a digest mismatch, walk the `datamap`/`nodemap`
   bitmaps positionally and recurse into the children that actually differ.

The whole thing runs in `O(|Δ| · log₃₂ S)` — proportional to the size of the
actual delta, not the size of the room. Applied at the root with the
`BLAKE2b-256(lattice)` shortcut, tier 2 becomes the `O(1)` whole-state-map
convergence check from the previous section.

None of this works if the trie's shape isn't canonical. CHAMP gives you that for
free on insertion — shape depends only on hash-prefix and bitmap occupancy, not
insertion order — but **not** on deletion, unless you enforce the standard
repair invariant: when a removal leaves a node with a single entry and no
children, that entry gets inlined into the nearest ancestor with other content,
rather than left as a degenerate one-entry node. Skip that repair and you
reintroduce insertion-order-dependent shape, which breaks pointer sharing,
structural-hash matching, and positional deep-diff all at once — not slower,
just wrong.

## Why writes stop being spiky

A state append writes `⌈log₃₂ S⌉` trie nodes on the path from the changed leaf
to the root — 4 nodes for a 50,000-event room, and that bound holds out to
`S ≈ 1.05 × 10⁶`, past the size of any real Matrix room today. Nodes are
immutable, so this is a descend-then-append: `log₃₂ S` dependent reads down the
existing trie, then the same number of sequential writes for the fresh nodes.
There's no full-`S` structure rewritten in one step, ever — which is what
actually eliminates the delta-chain snapshot cliff, not just amortizes it.

The honest comparison against Synapse-lineage engines is amortized
`O(S / MAX_STATE_DELTA_HOPS)` per state event, not raw `O(S)`, since that's what
a hop ceiling of 100 buys you. Counting rows, that's still roughly two orders of
magnitude in the HAMT's favor for a 50k-event room. Counting bytes with the
default 16-byte keyed digest, it's closer to 12×, because every HAMT node now
carries a digest the delta rows never needed. And the dependent-hop count
matters as much as the byte count: the top few trie levels are shared across
every state group in the room, so they stay resident in cache — realistic
uncached depth is closer to 1–2 hops than 4, against a delta chain that's walked
once and evicted, up to 100 predecessor pointers deep, with no equivalent of a
permanently-hot upper trie.

## What this looked like in a real homeserver

The `Conduit` → `continuwuity` state layer used a `state_compressor` module —
the classic delta-chain design. Porting the room state service over to the
augmented HAMT meant deleting that entire module (733 lines gone in one commit)
and replacing it with a much smaller `state_hamt` service backed by `rezzy`'s
generic HAMT crate.

The generic trie itself is deliberately storage-agnostic — `mod.rs` for the
node/bitmap machinery, `codec.rs` for a dense on-disk encoding of persisted
internal nodes, `delta.rs` for the three-tier diff above, `hash.rs` for the
keyed structural hash and the `LtHash`-derived state group ID. State resolution
(`resolve_state.rs`), the event handler's outlier upgrade path, and the sync
`/joined` endpoint all got noticeably _smaller_ in the diff — most of what they
used to do by hand (walking delta chains, comparing `shortstatehash`es, deciding
when to snapshot) the trie now does implicitly.

One non-obvious cost that only shows up once you're running this for real:
`state_hamt::store.rs` has to treat a failed HAMT node load as a hard error that
propagates, not a default-to-empty. A generic HAMT quietly returning "no entry"
for a lazy child it failed to fetch from disk is indistinguishable from that key
genuinely being absent — and for a state map, those two outcomes are not allowed
to look the same.

## Trade-offs, honestly

This isn't a strict upgrade over delta chains:

- **Point queries get slower per-query.** `log₃₂ S` dependent, unprefetchable
  I/O hops instead of one hash-table probe. It's a real cost against a
  single-probe index; it's only a win _relative to delta chains_, which pay
  worse on both hop count and cache locality.
- **Write amplification, twice over.** `log₃₂ S` node writes instead of one
  delta row, and on an LSM-backed store, compaction rewrites each of those nodes
  another 10–30× over its lifetime.
- **Memory for subtree digests.** Roughly `S/31` internal nodes for the first
  trie plus ~4 new nodes per subsequent group, at 16 bytes each under the
  default keyed digest. The optional full-lattice-per-node variant (2048
  bytes/node, for uniform lattice-strength equality at every level rather than
  just the root) is the one operators will actually notice.
- **Garbage collection becomes mandatory.** Structural sharing across state
  groups means pruning old history needs real refcounting or mark-and-sweep over
  trie nodes — you can't just drop a row.
- **The auth chain is still a separate problem.** The trie isolates the state
  delta; topologically sorting it via the auth chain is a DAG problem the trie
  doesn't touch.

None of this is wire-facing. The HAMT is a local indexing structure — nothing
about it changes what goes over federation. The only thing that has to be
reproducible across servers is the root lattice, and that's MSC4500's job, not
this structure's. This proposal is just what it looks like to build an efficient
local index directly on top of an accumulator that was already designed to be
compared.
