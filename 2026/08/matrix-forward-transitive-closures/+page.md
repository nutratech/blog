---
title: "[DRAFT] DAG exercise: Computing the forward reachable set"
date: "2026-08-01"
description: "Analysis of forward reachability, adaptive DAG traversal, and storage locality."
subtitle: "Beyond the transitive closure."
tags: ["Matrix", "Performance", "Algorithms"]
tags: ["Matrix", "Performance", "Algorithms", "RocksDB"]
draft: true
---

It is a prevailing illusion among software engineers that a graph, once
persisted to a database, may be effortlessly traversed. In the context of a
Matrix homeserver, where the room topology is a Directed Acyclic Graph (DAG)
possessing a variable branching factor, the cost of determining forward
reachability—whether event _A_ eventually leads to event _B_—presents a
formidable structural problem.

To my knowledge, advanced forward-reachability indexing remains an unrealized
optimization in the Matrix ecosystem; even Synapse does not presently implement
a dedicated forward-closure index, relying instead upon incremental evaluation
and historical auth-chains.

What follows is an analysis of why naive traversals fail, why materializing the
transitive closure is a mathematical trap, and how we might elegantly resolve
the tension between memory, storage I/O, and CPU cycles using adaptive
reachability accelerators.

### The Naive Approach and the Storage Reality

At first glance, one might assume that a homeserver is robustly protected by
simple integer-keyed adjacency lists:

```text
(shortroomid, shortevent_id) -> [shortprev_events]

```

Such an index permits recursive queries without the crushing expense of parsing
complete JSON events. However, a purely random adjacency list is an I/O
disaster. To ask for the edges of a given event is to trigger a random
point-read. A deep topological walk, left to its own devices upon such a schema,
degenerates into an unpredictable sequence of random disk operations.

The salvation of the adjacency list lies entirely in physical storage locality.
By prefixing the adjacency keys with a `shortroomid`, we do not flatten the
graph into a sequential list, but we _do_ constrain it to a contiguous sequence
of blocks on disk. Consequently, when the application requests the immediate
parents of an event, RocksDB pulls the block into RAM. Because Matrix DAGs are
topologically clustered by room, subsequent recursive hops—to grandparents and
siblings—cease to be disk I/O; they become instantaneous block cache hits. The
storage layer's duty is not to walk the graph, but to warm the cache so that the
CPU may walk it unhindered.

### The Illusion of the Transitive Closure

If recursive queries upon warm cache blocks are efficient, what is there to gain
by pre-computing the forward reachable set?

Consider the proposition of materializing the forward transitive closure. In a
`ForwardReachabilityIndex`, every node stores a compressed bitmap of all its
descendants. Queries become incredibly rapid, reducing to a single bitwise `OR`
operation.

Yet, as a durable storage mechanism for a live federated system, this is
structurally untenable. Matrix DAGs are not static; they continually branch and
merge. Re-computing the forward transitive closure on-demand as events stream in
would require updating all backwards-reachable events to append the new
extremity. As a room's history grows, this update penalty compounds
catastrophically, triggering tens of thousands of row updates for a single
incoming event and destroying any efficiency gained during the forward sweep. We
are forced to conclude that storing the full transitive closure is a fool's
errand.

### The Resolution: Adaptive Reachability

If we reject both the agonizing latency of a naive disk-bound BFS and the
prohibitive write-amplification of the full transitive closure, we must
synthesize a compromise.

This resolution is embodied in the `RangePrefilterReachability` accelerator.
Rather than materializing the full descendant bitmap, it extracts the adjacency
list from the room-prefixed storage cache and pairs it with a lightweight,
coarse descendant interval `[min_descendant, max_descendant]`.

By tracking these intervals, the algorithm maintains exactitude while
drastically pruning the search space. If a target node falls outside a branch's
descendant interval, the traversal immediately abandons that path. Furthermore,
the accelerator dynamically evaluates the geometry of the query to select the
optimal traversal mode:

- **Plain Indexed BFS:** Deployed for broad candidate sets where upfront hashing
  amortizes efficiently.
- **Range Pruned:** Utilized for selective queries, leveraging the descendant
  intervals to skip dead branches entirely.
- **Segment Jumps:** Engaged for highly selective queries upon long, unbranched
  chains, allowing the CPU to leap over compressible segments of the topology.

By relying on the storage layer solely to provide a cache-local adjacency list,
`RangePrefilterReachability` computes reachability entirely in memory at CPU
speeds, avoiding the $O(N^2)$ update penalty while preserving the strict latency
requirements of state resolution.

### Future Work: Forward Fill

The architectural principles established here extend beyond mere local state
resolution. A challenging and worthwhile corollary to this investigation is the
synchronization of forward event sets over the network.

Such mechanics are directly applicable to proposed features like MSC4000
(Forward Fill, or `/backfill` forwards). Determining precisely which events are
forward-reachable from a given extremity, without transmitting redundant
history, requires exactly the adaptive, interval-pruned graph traversal
described above. Simplifying these accelerators and claiming the remaining
performance gains will be the subject of future inquiry.
