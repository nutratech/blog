---
title: "Atomic Handover: Preventing Split-Brains in Matrix Arbiter Failovers"
date: "2026-08-09"
description:
  "Why implicit failovers fail, and how in-band topological consensus guarantees
  airtight state recovery."
draft: true
---

In
[our previous post](/blog/posts/2026/08/matrix-byzantine-arbiter-verification),
we proved that the computational cost of verifying a Byzantine arbiter's fraud
proof in Kegan's _Epoch-Resolved Arbitration (ERA)_ can be neutralized using
MSC4511 and HAMTs. But solving the computational bottleneck leaves the most
dangerous distributed systems challenge unresolved: **reaching global consensus
on the arbiter eviction.**

## The Threat of Asynchrony

In a highly asynchronous network like Matrix, out-of-band gossip and implicit
local rules guarantee catastrophic split-brains during a failover.

Imagine Server A verifies a fraud proof (a double-signed equivocation from
Arbiter 1) and quietly transitions to Arbiter 2. Meanwhile, Server B misses the
gossip broadcast due to a temporary network partition. Server B continues to
accept epochs from the now-malicious Arbiter 1. The room is now completely
fractured, and the CRDT's core promise of eventual consistency is shattered.

Relying on out-of-band propagation to trigger a state-machine transition means
you are gambling that information travels faster than state mutations. In
hostile federations, that is a losing bet.

## The "Atomic Handover" Solution

To patch this vulnerability, the failover orchestration cannot rely on
asynchronous gossip; it must be an explicit, in-band topological event. We call
this the **Atomic Handover**.

When a peer detects fraud, rather than just gossiping the proof, they send it
directly to the fallback arbiter (Arbiter 2). Arbiter 2 then authors a
specialized **"Eviction Epoch"** state event. Crucially, this event _embeds_ the
mathematical fraud proof (Arbiter 1's conflicting signatures) directly into its
payload.

By doing this, the handover becomes topologically atomic. The exact coordinate
where Arbiter 1 loses authority and Arbiter 2 gains it is fixed precisely in the
DAG.

## Matrix Auth Rules as Consensus

Matrix does not rely on global consensus; it relies on deterministic
authorization rules applied over the DAG topology.

To enforce the Atomic Handover, we upgrade the room's authorization rules to
mandate that the first epoch from a fallback arbiter is _only conditionally
valid_ if it contains the embedded fraud proof against the previous arbiter.

This guarantees:

1. **Synchronous Evaluation:** When a homeserver receives Arbiter 2's Eviction
   Epoch, the auth rules force them to parse the embedded fraud proof
   synchronously with the state transition.
2. **Self-Healing State:** If a server missed the initial warning gossip, they
   aren't permanently fractured. The moment they sync the Eviction Epoch, the
   embedded proof acts as a self-healing mechanism, instantaneously aligning
   their state.
3. **Cryptographic Finality:** The failover itself becomes a finalized,
   undeniable event in the CRDT's materialized view, permanently burning the
   bridge back to the malicious arbiter.

By shifting the eviction from an implicit local rule to an explicit, in-band
topological event, we completely eliminate the asynchronous race condition. This
hardens Kegan's ERA proposal for the reality of a fragmented federation,
guaranteeing airtight state recovery.
