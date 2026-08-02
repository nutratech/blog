---
title: "[DRAFT] DAG exercise: Computing the forward reachable set"
date: "2026-08-01"
description: "Advanced performance trick, not implemented by Synapse."
subtitle: 'Computing the "forward transitive closure" for given events.'
tags: ["Matrix", "Performance", "Algorithms"]
draft: true
---

This is an advanced technique[^v21_fwd_note] not yet implemented by Synapse
[^synapse_note01], nor any homeserver to my knowledge.

The performance gain depends on the fork depth. In general, this will not be
very significant (even for the ~35K member "Matrix Community" space), since a
homeserver is already fairly robustly guarded by simple integer-keyed indexes:

```text
(room_id, shortevent_id) -> (&room_id, [shortprev_events])

# or, more developer-friendly,

(shortevent_id) -> [shortprev_events]
```

This DB-index maps (short) event IDs to their `prev_event` edges, allowing rapid
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
10,000 or 100,000 row updates[^upsert_penalty] if the event passes state res and
is added to the timeline).

**TODO:** Draft/wip.

## Future work

Future work includes simplifying the code and claiming any remaining performance
gains.

A challenging and worthwhile follow-up investigation is studying proposed
methods of synchronizing the event set forward of given extremities over the
network and between two servers. Such functionality may be relevant in the
not-yet-implemented "forward fill" feature/endpoint[^msc4000].

**TODO:** Draft/wip.

<!-- markdownlint-disable MD033 -->

_<u>AI Disclosure:</u> OpenAI's `codex` CLI Assistant used to pinpoint relevant
areas in Synapse's codebase. Fable 5 and Gemini 3.1 Pro Web assisted in
brainstorming ideas for and helping to check the initial code demo
implementation (which was written and shaken out a week before this blog post
and follow-up was handwritten)._

<!-- markdownlint-enable MD033 -->

### Footnotes and references

[^v21_fwd_note]:
    _"It is also possible to return only the relevant forwards reachable events
    rather than all forwards reachable events to speed up load times of forwards
    reachable events, but this is out of scope for this guide."_

    State Res v2.1: An implementer's guide
    <https://matrix.org/docs/spec-guides/state-res-2.1/>

[^msc4000]:
    This MSC can be solved in many ways. The forward transitive closure is one
    possible tool in some circumstances.

    _MSC4000: Forwards fill (`/backfill` forwards) by MadLittleMods._
    <https://github.com/matrix-org/matrix-spec-proposals/pull/4000>

[^upsert_penalty]:
    As much as 90% to 100% of a room is backwards reachable from the frontier.

[^synapse_note01]:
    Synapse populates v2.1 forward reachable events incrementally (partially
    on-demand, partially from persisted backward auth-chain). It does not have a
    separate forward-closure index.
    <https://github.com/element-hq/synapse/blob/5ed830b3b4c74c89d876cc07756c5d98a100cbed/synapse/storage/databases/main/event_federation.py#L2498-L2560>
