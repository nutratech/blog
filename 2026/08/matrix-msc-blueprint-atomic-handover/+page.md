---
title: "The MSC Blueprint: Cryptographically Sealing the Atomic Handover"
date: "2026-08-09"
description:
  "Designing the m.room.era.epoch schema to mathematically prove Byzantine
  equivocation under split-canonicalization."
draft: true
---

In our previous posts, we established that solving the "Duelling Admins" problem
requires Epoch-Resolved Arbitration (ERA), and that we can neutralize the cost
of verifying an arbiter's fraud proof using MSC4511 and HAMTs. We then
introduced the **Atomic Handover**: an explicit, in-band topological state
transition that prevents split-brains by embedding the fraud proof directly into
a fallback arbiter's "Eviction Epoch."

But how do we actually represent this in the Matrix protocol?

Designing the `m.room.era.epoch` JSON payload reveals a fascinating
cryptographic challenge introduced by MSC4511's split-canonicalization.

## The Split-Canonicalization Pitfall

Under MSC4511 Part C, a modern Matrix `event_root` is generated from isolated
metadata leaves, rather than a monolithic hash of the entire JSON object. These
leaves include the `content_hash`, `prev_events_hash`, `auth_events_hash`,
`other_signed_fields_hash`, and a Merkleized `event_header_root`.

If our "Eviction Epoch" only embeds the malicious arbiter's signatures and the
resulting `event_root` hashes, we have a massive cryptographic hole.

A verifier can mathematically confirm that Arbiter 1 signed those two hashes,
but they have absolutely no way to know what those hashes represent. `Epoch A`
might be an `m.room.message` and `Epoch B` might be an `m.room.member` update.
Signing two different events is perfectly legal; it is not equivocation.

To prove Byzantine backdating, the verifier must definitively link those signed
`event_root` hashes to the specific `epoch_number` and event `type`. Therefore,
the payload must carry the Merkle inclusion proofs and the content payload
required to rebuild the `event_root`.

## The Corrected Schema: `m.room.era.epoch`

To execute the Atomic Handover, Arbiter 2 must author a state event that
provides the missing topological and content hashes, forcing the state
resolution algorithm to mathematically verify the fraud. By explicitly providing
the raw `content` object inside the proof, the verifier possesses the exact
bytes required to execute the canonicalization and hashing sequence,
mathematically anchoring the opaque `content_hash` string to the `epoch_number`.

Here is the blueprint for the eviction payload:

```json
{
  "type": "m.room.era.epoch",
  "state_key": "",
  "sender": "@arbiter2:fallback.example.com",
  "room_id": "!room:example.org",
  "content": {
    "epoch_number": 42,
    "previous_epoch_id": "$epoch_41_event_id",
    "eviction_proof": {
      "evicted_arbiter": "example.com",
      "reason": "equivocation",
      "conflicting_epochs": {
        "epoch_a": {
          "event_id": "$malicious_epoch_42_A",
          "content": {
            "epoch_number": 42
          },
          "top_level_hashes": {
            "content_hash": "base64url_sha3_256_hash_content_A",
            "prev_events_hash": "base64url_sha3_256_hash_prev_A",
            "auth_events_hash": "base64url_sha3_256_hash_auth_A",
            "other_signed_fields_hash": "base64url_sha3_256_hash_other_A"
          },
          "header_proofs": {
            "type": "m.room.era.epoch",
            "sender_domain": "example.com",
            "leaf_paths": {
              "type": [{ "side": "right", "hash": "..." }],
              "sender_domain": [{ "side": "left", "hash": "..." }]
            }
          },
          "signatures": {
            "example.com": {
              "ed25519:key_1": "signature_A_base64"
            }
          }
        },
        "epoch_b": {
          "event_id": "$malicious_epoch_42_B",
          "content": {
            "epoch_number": 42
          },
          "top_level_hashes": {
            "content_hash": "base64url_sha3_256_hash_content_B",
            "prev_events_hash": "base64url_sha3_256_hash_prev_B",
            "auth_events_hash": "base64url_sha3_256_hash_auth_B",
            "other_signed_fields_hash": "base64url_sha3_256_hash_other_B"
          },
          "header_proofs": {
            "type": "m.room.era.epoch",
            "sender_domain": "example.com",
            "leaf_paths": {
              "type": [{ "side": "right", "hash": "..." }],
              "sender_domain": [{ "side": "left", "hash": "..." }]
            }
          },
          "signatures": {
            "example.com": {
              "ed25519:key_1": "signature_B_base64"
            }
          }
        }
      }
    }
  },
  "auth_events": ["$m.room.create_event_id", "$epoch_41_event_id"],
  "prev_events": ["$malicious_epoch_42_A", "$malicious_epoch_42_B"],
  "signatures": {
    "fallback.example.com": {
      "ed25519:key_2": "arbiter2_signature_base64"
    }
  }
}
```

## Matrix Auth Rules: Validating the Equivocation

When a homeserver processes this event, the Matrix auth rules perform a
rigorous, synchronous verification of the fraud proof. The mechanics of
split-canonicalization defined in **MSC4511 Part C: Merkleized Metadata
Room-Version Sketch** dictate these validation steps:

1. **Target Entitlement:** The server verifies that the `sender`
   (`@arbiter2:fallback.example.com`) is the correctly ordered fallback arbiter.
2. **Reconstructing the Header:** For both `epoch_a` and `epoch_b`, the auth
   rules use the `leaf_paths` to rebuild the `event_header_root`, proving that
   the original event `type` was indeed `m.room.era.epoch` and the
   `sender_domain` was `example.com` as defined in MSC4511 Part C.
3. **Validating the Content Pre-image:** The verifier takes the explicit
   `content` payload provided in the proof (which contains
   `"epoch_number": 42`), canonicalizes it, and hashes it. It asserts that this
   locally computed hash exactly matches the provided `content_hash` string.
4. **Cryptographic Assembly:** As outlined in MSC4511 Part C, the verifier
   hashes the `prev_events_hash`, `auth_events_hash`, the rebuilt
   `event_header_root`, the verified `content_hash`, and the
   `other_signed_fields_hash` together to securely derive the final
   `event_root`.
5. **The Signature Check:** Finally, the verifier confirms that Arbiter 1
   (`example.com`) provided a valid Ed25519 signature over the canonical
   envelope containing this newly reconstructed `event_root`, strictly following
   the signature rules of MSC4511 Part C.

By successfully rebuilding two distinct roots that share the exact same epoch
sequence number, the verifier proves mathematically that Arbiter 1 signed
concurrent epoch events.

Simultaneously, Arbiter 2 places both `$malicious_epoch_42_A` and
`$malicious_epoch_42_B` in the `prev_events` array, forcing the DAG to merge the
fractured branches topologically. Arbiter 1 is permanently evicted, and the CRDT
smoothly transitions to the fallback arbiter without risking a split-brain.
