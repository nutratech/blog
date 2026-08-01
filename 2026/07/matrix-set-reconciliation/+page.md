---
title: "Matrix: Set Reconciliation"
date: "2026-07-30"
description: "Synchronizing or proving set equality over the network."
---

## Use cases (proposed)

### Federation state synchronization

|         | Expected bandwidth, 10K state events, 100 differing |
| ------- | --------------------------------------------------- |
| Naive   | 50 bytes/event × 10,000 events = 500 KB             |
| MSC4521 | 65 bytes/event × 100 events + 3 KB = 9.5 KB         |

```mermaid
%%{init: {"flowchart": {"subGraphTitleMargin": {"top": 15, "bottom": 15}}}}%%
flowchart TD
    subgraph Federation["[Federation] State Sync"]
        direction TB
        HS1["Homeserver A (State Set A)"]
        HS2["Homeserver B (State Set B)"]

        HS1 <-->|"1. Exchange Sketches"| HS2
        HS1 -. "2. Identify Missing State (B - A)" .-> HS1
        HS2 -. "2. Identify Missing State (A - B)" .-> HS2
        HS1 ==>|"3. Fetch Only Missing Events"| HS2
    end
```

---

### CSAPI integrity

Client developers are frequently blamed when servers fail to properly send down
all required `/sync` data. Take `/v3` as a basic example ( these issues can also
affect `/v5` but the application of set reconciliation to SSS is out of scope
for this blog post).

Some commonly reported issues:

1. Left rooms reappearing or sticking around;
2. State drift, client state resets (old profile photo/name, incorrect member
   list);
3. Zombie read receipts (similar to left rooms, they keep reappearing).

```mermaid
%%{init: {"flowchart": {"subGraphTitleMargin": {"top": 15, "bottom": 15}}}}%%
flowchart TD
    subgraph CSAPI["[CSAPI] State/Room Checks"]
        direction TB
        Client["Matrix Client (Local Cache C)"]
        Server["Homeserver (Server State S)"]

        Client <-->|"1. Exchange sketches on /sync"| Server
        Client -. "2. Detect Outdated Rooms/State (S - C)" .-> Client
        Server ==>|"3. Push Only Delta Updates"| Client
    end
```

#### How set reconciliation can help

Rather than falling back to the grand old, often painfully slow "clear cache &
reload," efficient set reconciliation can instantly send the client precisely
what it missed, either via the following `/sync` request or a custom exchange,
endpoint, or negotiation.
