---
title: "DAG exercise: Computing a node's forward reachable set"
date: "2026-08-01"
description: "Advanced performance trick, not implemented by Synapse."
subtitle: "Computes the fast forward transitive closure for given events."
tags: ["Matrix", "Performance", "Algorithms"]
---

This is an advanced technique[^v21_fwd_note] not yet implemented by Synapse, nor
any homeserver to my knowledge.

The performance gain depends on the fork depth. In general, this will not be
very significant (even for the ~35K member "Matrix Community" space), since a
homeserver is already fairly robustly guarded by simple integer-keyed indexes:

```text
(room_id, shortevent_id) -> (&room_id, [shortprev_events])

# or, more developer-friendly,

(shortevent_id) -> [shortprev_events]
```

This DB index maps (short) event IDs to their `prev_event` edges, allowing rapid
**recursive queries** without full event parsing. Both PostgreSQL and RocksDB
generally perform better reading only the 8-byte `shortprev_event` values as
opposed to loading the entire event JSON, just to throw it away.

Since we only expect a small performance gain, this is a theoretical exercise or
hobbyist investigation.

## Supplanting naive forward recursion

If for most rooms forward recursion over `shortprev_event` edges is sufficient,
what is there to gain?

Re-computing the forward transitive closure on-demand as events stream in would
involve updating all backwards reachable events to include the new extremity.
This update penalty becomes worse as the room and backward reachable subgraph
grow, destroying any potential efficiency gains in the forward sweep (triggering
10,000 or 100,000 row updates if the event passes state res and is added to the
timeline).

**TODO:** Draft/wip.

### Footnotes and references

[^v21_fwd_note]:
    _"It is also possible to return only the relevant forwards reachable events
    rather than all forwards reachable events to speed up load times of forwards
    reachable events, but this is out of scope for this guide."_ State Res v2.1:
    An implementer's guide <https://matrix.org/docs/spec-guides/state-res-2.1/>
