---
title: "MSC4521: Set Reconciliation"
date: "2026-07-30"
description:
  "Use cases for synchronizing or proving set equality over the network."
subtitle: "Proposed use cases for set reconciliation: federation and clients."
tags: ["Matrix", "Proposals", "Applications"]
---

This post contains a growing list of optimizations, new use cases, or features
provided by MSC4521.

## CS-API integrity

Client developers are frequently blamed when servers fail to properly send down
all required `/sync` data. Take `/v3` as a basic example, and let's tally common
reports. These issues can also affect `/v4` or `/v5` (SSS), but any role of set
reconciliation in repairing SSS cache is both more speculative and technically
out of scope[^v5_complexity] for this blog post. The `/v3` sync contract is
better understood and easier to model.

Some commonly reported UI issues from Synapse users:

1. **Left rooms reappearing** or sticking around;
2. **State drift**, client resets (old profile photos/names, incorrect member
   list);
3. Zombie **read receipts** (similar to left rooms, they keep reappearing).

The Conduit family often experiences these same glitches. In some cases, the
glitches may be deterministic: if you view from a second client (Element), you
may see precisely the same data issue as your first (Cinny); similarly, even
after a cache clear and initial sync, the issue can sometimes stay.

<!-- markdownlint-disable MD024 -->

### How set reconciliation can help

```mermaid
%%{init: {"flowchart": {"subGraphTitleMargin": {"top": 15, "bottom": 15}}}}%%
flowchart TD
    subgraph CSAPI["[CSAPI] Client cache and State/Room data"]
        direction TB
        Client["Client (Local cache C)"]
        Server["Homeserver (Server store S)"]

        Client <-->|"1. Exchange sketches on /sync"| Server
        Client -. "2. Detect outdated data (S - C)
                      [e.g., Rooms list/State/EDUs]" .-> Client
        Server ==>|"3. Push only missing IDs or data"| Client
    end
```

Rather than falling back to the abysmally bad, often painfully slow "clear cache
& reload" — efficient set reconciliation can instantly send the client precisely
what it missed, either via the following `/sync` request or a custom exchange,
endpoint, or negotiation.

The user/client initiates this request via a new, less sledgehammery **"sync
cache" button** (rather than the **infamous "clear cache"**). Clicking the
button and retrieving the data then applies the missing delta and, if the server
has a fully accurate state, restores an accurate view to the client and user.

### Bonus consideration/thought experiment

**Problem:** How do you know, other than trust, that the client actually holds
all the relevant data requested from the server and that **none** has been lost?

The conventional answer is we just trust the software and the server developers.
But homeserver development is notoriously difficult and tedious. Blindly
trusting that nothing has fallen through the cracks leaves the end-user with no
quick way to verify their local store. If their eye catches something in their
Element or Cinny UI client, they might suspect a state reset or cache window
miss has occurred. Every user instinctively reaches for the sledgehammer
solution, the notorious "reset cache" button, which is so often used as an
inconvenient band-aid to repair edge cases like this.

This is clearly not ideal. The user is stuck manually visually scanning to check
for potential signs of a de-sync issue, and must perform a full cache clear and
initial sync (which can be slow). The more interesting approach is to apply
clever math or encoding techniques[^syndrome_decoding_algebraic_sets] to compare
large amounts of data over the wire _without_ needing to send or transmit
significant amounts of it.

<!-- markdownlint-disable MD033 -->

The use case still exists if the `/sync` code is completely stabilized.
End-users and client developers alike **may not fully trust the _incremental_
model**, even for `/v3`, and they are well within their rights to request a
fast, **<u>independent</u> mechanism which cleverly encodes the _full_ data
set** and returns any missing events (and lists any extra the client may somehow
have).

<!-- markdownlint-enable MD033 -->

If the user presses the "sync cache" or "repair cache" button (however we decide
to label it), and their client receives back an empty list of missing events,
then they know (and the client UI can confirm with a toast message) that their
cache was already healthy and fully in agreement with the server.

This provides a fast, reliable way to _prove_ to the end-user's client that it
has all the data the server has, and that none of it has been accidentally lost
during the potentially error-prone shuffle of ongoing data down and along the
incremental `/sync` pipeline.

---

## Federation state synchronization

During live federation, `/state_ids` is a recommended fallback if
`/get_missing_events` fails[^state_ids_fallback].

Some homeservers also implement an admin command to compare state with other
homeservers in a given room, allowing manual diagnosis of local state issues and
broad manual ranking of peers — estimating, by skimming data, which peer(s) has
the best or most trustworthy state.

See the table below for estimates of network bandwidth savings on the
`/state_ids` endpoint.

<!-- markdownlint-disable MD013 -->

| Approach | Estimated bandwidth $(\|S\| = 10,000, d = 100)$ | Estimated round-trip time | Estimated DB/IO time, remote |
| -------- | ----------------------------------------------: | ------------------------: | ---------------------------: |
| Naive    |         50 bytes/event × 10,000 events = 500 KB |                   0.8 sec |      0.6 sec (+0.05 sec CPU) |
| MSC4521  |     100 bytes/event × 100 events + 3 KB = 13 KB |                   0.2 sec |                      0.1 sec |

<!-- markdownlint-enable MD013 -->

### How set reconciliation can help

```mermaid
%%{init: {"flowchart": {"subGraphTitleMargin": {"top": 15, "bottom": 15}}}}%%
flowchart TD
    subgraph Federation["[Federation] State Sync"]
        direction TB
        HS1["Homeserver A (State set A)"]
        HS2["Homeserver B (State set B)"]

        HS1 <-->|"1. Exchange sketches" | HS2
        HS1 -. "2. Identify missing state (B - A)" .-> HS1
        HS2 -. "2. Identify missing state (A - B)" .-> HS2
        HS1 ==>|"3. Fetch only missing data"| HS2
    end
```

This MSC promises to reduce the network bandwidth of `/state_ids` exchanges and
the overall allocation of strings into a set (generally slower than an optimized
bitmap set comparison, but not a huge performance concern).

In general, the `/state_ids` endpoint is not expensive, but it can be under
heavy federation.

The benefits become far more substantial if we consider large rooms (over
100,000 members), or if we widen the query to include _all_ prior state events
(not just currently resolved ones).

The benefits become far more substantial if we consider large rooms (over
100,000 members) or if the query includes all prior state events (not just the
currently resolved set)

A given server, if it diverges, can reach out to a list of other servers (either
admin-defined, or randomly selected or ranked). By reaching out to 10 or 20
servers (or however many are in the room, if fewer), the resident server can
increase their chances of filling in gaps/holes. A strict traversal and
collection of state IDs today is complicated by ordinary (potentially missing)
message events interlacing with state events (see State DAGs[^msc4242], which
replaces `auth_events` with `prev_state_events`, and benefits from a companion
proposal to achieve higher rates of synchronization across the federation and
improved rates of complete event dispersal).

It can also more greatly optimize the `GET /state` endpoint, but this exists
today mostly for legacy/compatibility reasons, so optimizing it is a lower
priority.

### Future considerations

Applying "periodic" room-state reconciliation between other homeservers, MSC4521
provides greater assurance of consensus among "authoritative" peers. It can
signal to admins divergent rooms (rate limiting the logs), and it can allow
manual state comparison over federation if the admin is especially determined to
align or diagnose their room(s).

Furthermore, providing end-users and clients with the ability to quickly verify
complete agreement between large amounts of local and remote data is a
convenience gain and quality assurance to the average user. Who doesn't want to
know they're working with the complete set?

<!-- markdownlint-disable MD033 -->

_<u>AI Disclosure:</u> mermaid diagrams adapted from Gemini 3.1 Pro output.
Grammarly Writing Assistant consulted for intermediate proofreading._

<!-- markdownlint-enable MD033 -->

### Footnotes and sources consulted

[^v5_complexity]:
    Since SSS `/sync` has many more filters and properties and allows clients to
    configure custom timeline boundaries and selective state filters, correctly
    partitioning the set of server events — to precisely align with the client's
    start boundary / limited view and obey all filter rules — may be difficult.

[^msc4242]:
    _MSC4242: State DAGs by kegsay · Pull Request #4242 ·
    matrix-org/matrix-spec-proposals_
    <https://github.com/matrix-org/matrix-spec-proposals/pull/4242>

[^state_ids_fallback]:
    "... server may use the `/get_missing_events` API to acquire the events..."
    _Matrix Spec: Server-Server API — Backfilling and retrieving missing
    events._
    <https://spec.matrix.org/latest/server-server-api/#backfilling-and-retrieving-missing-events>
    **NOTE:** The spec does _not_ explicitly require this, but it is implicitly
    encouraged in a Complement test.

[^syndrome_decoding_algebraic_sets]:
    _Quadratic Modelings of Syndrome Decoding_
    <https://arxiv.org/html/2412.04848v1>
