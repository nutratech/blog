---
title: "[DRAFT] Bloom, Cuckoo, and Quotient filters: break-even formulas"
date: "2026-09-02"
description:
  "Modeling the cross-over points between three probabilistic filters under
  fixed network latency."
subtitle: "When does each filter win?"
tags: ["Algorithms", "Performance", "Data Structures"]
draft: false
---

When two peers reconcile large sets over the network, the standard Minisketch
approach exchanges compact sketches and decodes the symmetric difference. But
when a bucket's residual exceeds the decode budget, the protocol must either
split the bucket (more rounds) or switch to a filter-based fallback. The
question is: which filter, and when?

This post derives the break-even formulas for Bloom, Cuckoo, Counting Quotient,
and a naive remainder-probe baseline, then sweeps false-positive rates from
0.01% to 1% to find the cross-over points under fixed network latency.

## The five filters

### Bloom filter

The workhorse. A bit-array of `m` bits with `k` independent hash functions. No
deletions, no counting. Space-optimal among all probabilistic data structures
with symmetric false-positive/false-negative behavior (though Bloom filters have
no false negatives).

Key formulas:

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
\frac{m}{n} = \frac{-\ln p}{(\ln 2)^2}
\qquad
k = \frac{m}{n} \ln 2
$$

### Cuckoo filter

Power-of-two bucket count, 4 slots per bucket, 13-bit fingerprints (at 0.1%
FPR). The alternate bucket is computed by hashing the fingerprint independently
and XOR-ing with the current index — involutive by construction. A small stash
(8 entries) handles eviction failures.

The lookup checks 2 buckets × 4 slots = 8 fingerprints, so the fingerprint bits
are:

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
f_p = \lceil \log_2(8 / p) \rceil
$$

### Counting Quotient Filter (CQF)

A quotient filter with explicit run-length metadata: `occupied`, `continuation`,
and `shifted` bitmaps. Each slot holds a remainder and a saturated count,
supporting deletion and multiplicity tracking. At 75% load, a lookup budgets 4
candidate remainders per quotient run:

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
r = \lceil \log_2(4 / p) \rceil
$$

### Remainder-probe filter (baseline)

A naive linear-probed remainder array. **Not** a quotient filter — it lacks
run-length encoding, continuation bits, and the quotient-based cluster
structure. Included as an honest baseline to illustrate why the CQF's metadata
matters.

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
r = \lceil \log_2(1 / p) \rceil
$$

### Golomb-coded set (BIP 158)

A sorted, Golomb-Rice encoded array of truncated hash values. Unlike the
membership-only filters above, a GCS is **invertible**: the receiver can
enumerate all stored elements from the wire bytes, enabling direct symmetric
difference computation without a separate probe step.

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
\text{space} \sim P \text{ bits/element}, \quad \text{FPR} = \frac{1}{P}
$$

The wire format is a Golomb-Rice encoded bitstream of sorted delta-coded hashes.

The `rezzy` benchmark (`benches/math/invertible_filter.rs`) implements this as a
BIP 158–style set with `P ∈ {20, 128, 512}`.

## Space overhead

The table below shows the wire cost (bytes per element) for each filter at
`n = 100,000` elements, across four target false-positive rates. These are the
formulas used in the `rezzy` benchmark harness (`benches/math/filters.rs`). GCS
values use the P parameter that achieves each FPR (P = 1/p). PinSketch
(Minisketch) is included as the baseline — it sends one 64-bit coefficient per
element regardless of FPR.

<!-- markdownlint-disable MD013 -->

| FPR   | Bloom    | Cuckoo    | CQF       | Remainder-probe | GCS       | PinSketch |
| ----- | -------- | --------- | --------- | --------------- | --------- | --------- |
| 0.01% | 2.40 B/ε | 10.55 B/ε | 11.80 B/ε | 8.00 B/ε        | 128.0 B/ε | 8.0 B/ε   |
| 0.10% | 1.80 B/ε | 8.44 B/ε  | 8.54 B/ε  | 5.00 B/ε        | 16.0 B/ε  | 8.0 B/ε   |
| 0.50% | 1.38 B/ε | 8.44 B/ε  | 6.57 B/ε  | 4.00 B/ε        | 3.2 B/ε   | 8.0 B/ε   |
| 1.00% | 1.20 B/ε | 8.44 B/ε  | 6.56 B/ε  | 3.00 B/ε        | 1.6 B/ε\* | 8.0 B/ε   |

<!-- markdownlint-enable MD013 -->

\* GCS at 1% FPR uses P = 100, which falls between the benchmark's P = 20 and P
= 128 points; value is interpolated.

**Derivations:**

<!-- markdownlint-disable MD013 -->

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
\begin{aligned}
\text{Bloom:} \quad & \frac{m/n}{8} \quad \text{(bits} \to \text{bytes)} \\[6pt]
\text{Cuckoo:} \quad & \frac{n}{0.955} \times \frac{4 \lceil \log_2(8/p) \rceil}{8} + \frac{16}{n} \\[6pt]
\text{CQF:} \quad & \frac{n}{0.75} \times \left(2 + 2 + \frac{\lceil \log_2(4/p) \rceil}{8} + \frac{3}{8}\right) \\[6pt]
\text{R-probe:} \quad & (n \times 1.1) \times \left(\frac{\lceil \log_2(1/p) \rceil}{8} + \frac{1}{n}\right) \\[6pt]
\text{GCS:} \quad & \frac{P}{8} \quad \text{(Golomb-Rice, } {\sim}P \text{ bits/elem)}
\end{aligned}
$$

<!-- markdownlint-enable MD013 -->

Bloom dominates on space at every FPR. Cuckoo pays a fixed 8.44 B/ε once the
fingerprint floor of 13 bits kicks in (at p ≤ 0.1%). CQF is competitive with
Cuckoo at low FPR but gains the ability to count and delete. The remainder-probe
baseline is cheap because it stores minimal metadata — but its linear-probe
lookups are cache-unfriendly and it cannot handle deletion. GCS trades space for
invertibility: at low FPR it is the most expensive (128 B/ε at 0.01%), but it
stores the full sorted hash array, enabling enumeration without a probe step.

## The filter protocol

There are two fundamentally different filter protocols, depending on whether the
filter is **invertible**.

### Protocol A: Membership-only filters (Bloom, Cuckoo, CQF, remainder-probe)

These filters support `contains()` only. The protocol is asymmetric — one side
builds, the other probes:

```text
RTT 1:
  sender  → receiver:  filter built from sender's bucket elements
  receiver → sender:  candidate list (filter.contains == true)
                      + receiver-only list (filter.contains == false)

Sender computes symmetric difference from candidates + receiver-only.
```

The filter's benefit is avoiding recursive bucket-splitting rounds. It does
**not** reduce response wire cost — the receiver always sends back every element
partitioned as candidate or receiver-only. The filter is never inverted: the
receiver probes its own elements against the filter locally, and the
`contains()` interface is the only requirement.

### Protocol B: Invertible filters (Golomb-coded set)

A GCS stores the sorted hash array in Golomb-Rice encoding. Both sides exchange
GCS, enumerate the decoded elements, and compute the symmetric difference
locally:

```text
RTT 1:
  peer A → peer B:  GCS(A)
  peer B → peer A:  GCS(B)

Both peers enumerate GCS → hash sets, compute symmetric difference.
```

This is a **symmetric** protocol — both sides do the same work. The wire cost is
`2 × wire_bytes(GCS)` (both directions), but there is no probe step and no
false-positive decode overhead. The cost model is:

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
C_{\text{gcs}} = L + 2 \cdot C_{\text{build}}(n) + 2 \cdot n \cdot C_{\text{enumerate}}
$$

where `C_enumerate` is the per-element cost of decoding the Golomb-Rice
bitstream (typically 0.1–1 µs).

### Cost comparison

<!-- markdownlint-disable MD013 -->

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
\begin{aligned}
C_{\text{membership}} &= L + C_{\text{build}}(n) + C_{\text{probe}}(n) + \text{FP} \cdot C_{\text{decode one}} \\
C_{\text{invertible}} &= L + 2 \cdot C_{\text{build}}(n) + 2 \cdot n \cdot C_{\text{enumerate}}
\end{aligned}
$$

<!-- markdownlint-enable MD013 -->

The invertible path eliminates false-positive decode overhead but pays double
the build cost and transmits the filter in both directions. It wins when the
membership-only filter's FP cost exceeds the extra wire — i.e., at **high FPR**
or **large n** where `FP × C_decode_one > C_build(n) + n × C_enumerate`.

## Break-even formula

Sketch splitting costs `R` rounds of `L + C_wire(n) + C_decode(n)` where `R`
grows with the initial bucket size and shrinks with the decode budget. Both
filter protocols cost `R + 1` rounds but avoid recursive splitting for overflow
buckets.

**Membership-only filter wins when:**

<!-- markdownlint-disable MD013 -->

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
L < R \cdot \left(L + C_{\text{wire}}(n) + C_{\text{decode}}(n)\right) - \left[C_{\text{build}}(n) + C_{\text{probe}}(n) + \text{FP} \cdot C_{\text{decode one}}\right]
$$

<!-- markdownlint-enable MD013 -->

**Invertible filter wins when:**

<!-- markdownlint-disable MD013 -->

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
L < R \cdot \left(L + C_{\text{wire}}(n) + C_{\text{decode}}(n)\right) - \left[2 \cdot C_{\text{build}}(n) + 2 \cdot n \cdot C_{\text{enumerate}}\right]
$$

<!-- markdownlint-enable MD013 -->

In practice, the break-even is dominated by two terms:

1. **Extra RTT cost**: `+L` (both protocols add one round trip)
2. **Post-filter overhead**: `+FP × C_decode_one` (membership-only) or
   `+2 × n × C_enumerate` (invertible)

The membership-only path is cheaper when FPR is low (few false positives to
decode). The invertible path is cheaper when FPR is high or when the decode
budget is tight (no PinSketch decode needed at all).

## Cross-over results

The `rezzy` benchmark suite (`cross_over_summary()` in `filter_spillover.rs`)
sweeps Δ ∈ {1K, 5K, 10K, 25K, 50K, 100K} at `n = 1,000,000` elements and reports
the minimum Δ where each filter first beats sketch-split on wall time. The
sketch baseline at Δ = 100K (fastest case, fewest rounds) is shown for
reference.

<!-- markdownlint-disable MD013 MD060 -->

| Latency | Budget    | Cuckoo Δ | Remainder Δ | CQF Δ  | Bloom Δ | Hybrid Δ |
| ------- | --------- | -------- | ----------- | ------ | ------- | -------- |
| 0ms     | 1,000,000 | never    | never       | never  | never   | never    |
| 0ms     | 4,000,000 | never    | never       | never  | never   | never    |
| 0ms     | 8,000,000 | never    | never       | never  | never   | never    |
| 0ms     | 16,000,000| never    | never       | never  | never   | never    |
| 20ms    | 1,000,000 | 50,000   | 50,000      | 25,000 | 25,000  | 50,000   |
| 20ms    | 4,000,000 | 50,000   | 50,000      | 25,000 | 25,000  | 50,000   |
| 20ms    | 8,000,000 | 50,000   | 50,000      | 25,000 | 25,000  | 50,000   |
| 20ms    | 16,000,000| 50,000   | 50,000      | 25,000 | 25,000  | 50,000   |
| 30ms    | 1,000,000 | 25,000   | 25,000      | 10,000 | 10,000  | 25,000   |
| 30ms    | 4,000,000 | 25,000   | 25,000      | 10,000 | 10,000  | 25,000   |
| 30ms    | 8,000,000 | 25,000   | 25,000      | 10,000 | 10,000  | 25,000   |
| 30ms    | 16,000,000| 25,000   | 25,000      | 10,000 | 10,000  | 25,000   |
| 40ms    | 1,000,000 | 10,000   | 10,000      | 5,000  | 5,000   | 10,000   |
| 40ms    | 4,000,000 | 10,000   | 10,000      | 5,000  | 5,000   | 10,000   |
| 40ms    | 8,000,000 | 10,000   | 10,000      | 5,000  | 5,000   | 10,000   |
| 40ms    | 16,000,000| 10,000   | 10,000      | 5,000  | 5,000   | 10,000   |

<!-- markdownlint-enable MD013 MD060 -->

Key observations:

- **At 0ms latency, filters never win.** The CPU overhead of building and
  probing the filter always exceeds the cost of additional sketch-split rounds
  when there is no RTT penalty to avoid.
- **CQF and Bloom break even earliest** (Δ = 10K at 40ms). Their lower
  per-element wire cost means the 1-RTT penalty is amortized sooner.
- **Cuckoo and remainder-probe break even at the same Δ.** Despite different
  wire costs, their CPU profiles are similar enough that the cross-over is
  latency-dominated.
- **Budget has no effect on the cross-over Δ.** The decode budget affects
  whether sketch-split succeeds at all, not the relative cost once both paths
  are viable.

## FPR sensitivity analysis

Sweeping the false-positive rate from 0.01% to 1% at `n = 100,000` and
`L = 30ms` network latency. The "total cost" column is the modeled wall time for
one overflow bucket group:

<!-- markdownlint-disable MD013 -->

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
C_{\text{total}} = L + C_{\text{build}} + C_{\text{probe}} + (p \times n) \times C_{\text{decode one}}
$$

<!-- markdownlint-enable MD013 -->

using `C_build ≈ 0.05ms`, `C_probe ≈ 0.02ms`, and `C_decode_one ≈ 5µs` (from the
microbenchmark data in `filter_spillover.rs`).

### PinSketch baseline (no FPR)

PinSketch is **exact** — zero false positives, zero false negatives. But it
requires **multiple rounds** of sketch exchange when the decode budget is
exceeded (sketch splitting). Each round pays `L` in RTT plus wire + decode cost.
The decode operation is **superlinear** — roughly O(n^1.7) — so it dominates at
scale:

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
C_{\text{sketch}} = R \cdot L + C_{\text{decode}}(n)
$$

where `R` is the number of split rounds and `C_decode` is the cumulative decode
cost across all buckets (from `extract+decode` benchmarks in
`filter_spillover.rs`).

<!-- markdownlint-disable MD013 -->

| n         | Rounds | Total wire (KB) | Decode (ms) | Total (ms) |
| --------- | ------ | --------------- | ----------- | ---------- |
| 1,000     | 1      | 15.6            | 0.003       | 30.0       |
| 10,000    | 1      | 156.3           | 3.0         | 33.0       |
| 100,000   | 16     | 3,125.0         | 25.1        | 505.1      |
| 1,000,000 | 128    | 46,875.0        | 198.1       | 4,038.1    |

<!-- markdownlint-enable MD013 -->

The decode cost grows superlinearly (O(n^1.7)) and dominates at scale. At
`n = 100K`, sketch splitting takes ~500ms across 16 rounds; at `n = 1M`, it
exceeds 4 seconds. This is exactly what the filter strategies avoid: they pay
one extra RTT up front but eliminate the need for recursive sketch splitting.

### Bloom filter (FPR sweep)

<!-- markdownlint-disable MD013 -->

| FPR   | Wire (KB) | Build (ms) | Probe (ms) | FP count | FP decode (ms) | Total (ms) |
| ----- | --------- | ---------- | ---------- | -------- | -------------- | ---------- |
| 0.01% | 293.0     | 0.050      | 0.020      | 10       | 0.050          | 30.12      |
| 0.05% | 258.6     | 0.050      | 0.020      | 50       | 0.250          | 30.32      |
| 0.10% | 243.8     | 0.050      | 0.020      | 100      | 0.500          | 30.57      |
| 0.25% | 222.7     | 0.050      | 0.020      | 250      | 1.250          | 31.32      |
| 0.50% | 210.9     | 0.050      | 0.020      | 500      | 2.500          | 32.57      |
| 1.00% | 199.2     | 0.050      | 0.020      | 1000     | 5.000          | 35.07      |

<!-- markdownlint-enable MD013 -->

### Cuckoo filter (FPR sweep)

<!-- markdownlint-disable MD013 -->

| FPR   | Wire (KB) | Build (ms) | Probe (ms) | FP count | FP decode (ms) | Total (ms) |
| ----- | --------- | ---------- | ---------- | -------- | -------------- | ---------- |
| 0.01% | 1030.3    | 0.080      | 0.030      | 10       | 0.050          | 30.16      |
| 0.05% | 832.8     | 0.070      | 0.025      | 50       | 0.250          | 30.35      |
| 0.10% | 824.2     | 0.065      | 0.025      | 100      | 0.500          | 30.59      |
| 0.25% | 824.2     | 0.060      | 0.025      | 250      | 1.250          | 31.34      |
| 0.50% | 824.2     | 0.055      | 0.023      | 500      | 2.500          | 32.58      |
| 1.00% | 824.2     | 0.050      | 0.022      | 1000     | 5.000          | 35.07      |

<!-- markdownlint-enable MD013 -->

### Counting Quotient Filter (FPR sweep)

<!-- markdownlint-disable MD013 -->

| FPR   | Wire (KB) | Build (ms) | Probe (ms) | FP count | FP decode (ms) | Total (ms) |
| ----- | --------- | ---------- | ---------- | -------- | -------------- | ---------- |
| 0.01% | 1152.3    | 0.090      | 0.035      | 10       | 0.050          | 30.18      |
| 0.05% | 904.7     | 0.080      | 0.030      | 50       | 0.250          | 30.36      |
| 0.10% | 834.0     | 0.075      | 0.028      | 100      | 0.500          | 30.60      |
| 0.25% | 738.3     | 0.065      | 0.025      | 250      | 1.250          | 31.34      |
| 0.50% | 641.6     | 0.060      | 0.023      | 500      | 2.500          | 32.58      |
| 1.00% | 640.0     | 0.055      | 0.022      | 1000     | 5.000          | 35.08      |

<!-- markdownlint-enable MD013 -->

### Remainder-probe (FPR sweep)

<!-- markdownlint-disable MD013 -->

| FPR   | Wire (KB) | Build (ms) | Probe (ms) | FP count | FP decode (ms) | Total (ms) |
| ----- | --------- | ---------- | ---------- | -------- | -------------- | ---------- |
| 0.01% | 781.3     | 0.060      | 0.025      | 0        | 0.000          | 30.09      |
| 0.05% | 585.9     | 0.055      | 0.022      | 0        | 0.000          | 30.08      |
| 0.10% | 488.3     | 0.052      | 0.021      | 0        | 0.000          | 30.07      |
| 0.25% | 390.6     | 0.050      | 0.020      | 0        | 0.000          | 30.07      |
| 0.50% | 312.5     | 0.048      | 0.020      | 0        | 0.000          | 30.07      |
| 1.00% | 234.4     | 0.045      | 0.019      | 0        | 0.000          | 30.06      |

<!-- markdownlint-enable MD013 -->

**Note on remainder-probe:** The zero false-positive count is expected — the
remainder-probe filter is an exact hash table (no false positives by
construction, assuming no hash collisions within the remainder space). Its
advantage is zero FP decode overhead; its disadvantage is larger wire cost at
low FPR and cache-unfriendly linear probing.

### Golomb-coded set (invertible)

The GCS uses a different cost model — both sides exchange the filter, enumerate
locally, and compute the symmetric difference without a PinSketch decode step:

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->

$$
C_{\text{total}} = L + 2 \cdot C_{\text{build}}(n) + 2 \cdot n \cdot C_{\text{enumerate}}
$$

using `C_build ≈ 0.08ms` (sort + Golomb-Rice encode) and `C_enumerate ≈ 0.5µs`
(sorted binary search per element).

<!-- markdownlint-disable MD013 -->

| FPR   | Wire one-way (KB) | Wire total (KB) | Build (ms) | Enumerate (ms) | Total (ms) |
| ----- | ----------------- | --------------- | ---------- | -------------- | ---------- |
| 0.78% | 1562.5            | 3125.0          | 0.080      | 50.0           | 80.08      |
| 5.00% | 305.2             | 610.4           | 0.080      | 50.0           | 80.08      |
| 20.0% | 305.2             | 610.4           | 0.080      | 50.0           | 80.08      |

<!-- markdownlint-enable MD013 -->

\* GCS at P = 128 (FPR ≈ 0.78%) and P = 20 (FPR = 5%) from the benchmark. The
enumerate cost dominates: `n × C_enumerate = 100K × 0.5µs = 50ms`.

**Key difference from membership-only filters:** The GCS total is dominated by
the enumerate step, not the network RTT or filter build. At `n = 100K`,
enumeration costs ~50ms regardless of FPR — this is the price of invertibility.
For smaller n (≤ 10K), the enumerate cost drops below the RTT and GCS becomes
competitive.

### Cross-over observations

1. **PinSketch is cheapest in wire at every FPR.** Its 8 B/ε is fixed — no
   false-positive trade-off. The cost is paid in extra RTT rounds when the
   decode budget is exceeded.

2. **Bloom is the cheapest membership-only filter at every FPR.** Its
   space-optimal design means the smallest wire transfer, and at p ≤ 0.1% the
   false-positive decode overhead is negligible (< 0.5ms).

3. **Cuckoo's wire cost is flat for p ≤ 0.1%.** The 13-bit fingerprint floor
   means there is no benefit to relaxing FPR below 0.1% — you pay 8.44 B/ε
   regardless.

4. **CQF gains the most from relaxing FPR.** Dropping from 0.01% to 1% cuts wire
   by 44% (1152 KB → 640 KB), because the remainder width shrinks from 16 to 8
   bits.

5. **The remainder-probe baseline is cheapest in total cost at every FPR**
   because it has zero false positives. But this is misleading — its linear
   probe structure makes `C_build` and `C_probe` pessimistic for large n, and it
   cannot support deletion.

6. **At 30ms latency, all membership-only filters add < 1ms overhead at p ≤
   0.1%.** The network RTT dominates. Filter choice matters far less than the
   decision of whether to use filters at all.

7. **GCS is dominated by enumerate cost, not network latency.** At `n = 100K`,
   the ~50ms enumerate step makes GCS uncompetitive for large sets. It becomes
   interesting at small n (≤ 10K) where enumeration is fast and the symmetric
   protocol avoids the PinSketch decode entirely.

## Decision table

| Scenario                                       | Winner    |
| ---------------------------------------------- | --------- |
| Low latency (≤ 5ms), any Δ                     | PinSketch |
| High latency (≥ 20ms), Δ < 5K                  | PinSketch |
| High latency, Δ > 25K, insert-only             | Cuckoo    |
| High latency, Δ > 25K, need counting/deletion  | CQF       |
| High latency, Δ > 25K, simplest implementation | Bloom     |
| Small n (≤ 10K), need full set recovery        | GCS       |
| Unknown workload / mixed                       | Hybrid    |

The **hybrid** strategy uses a filter for small overflow buckets (≤ 2× decode
capacity) and falls back to sketch-split for large ones. It is the safest
default when the overflow distribution is unpredictable.

## Reproducibility

All benchmarks live in the `rezzy` repository. Results in this post were
generated at commit `02e8d8d`. To run the full sweep:

```bash
cd /path/to/rezzy
REZZY_FILTER_FULL_SWEEP=1 make bench
```

This runs the `filter_spillover` benchmark with:

- `n` ∈ {1K, 10K, 100K, 1M} elements
- Δ ∈ {1K, 5K, 10K, 25K, 50K, 100K}
- Latency ∈ {0, 20, 30, 40} ms
- Decode budget ∈ {1M, 4M, 8M, 16M}
- Cases: best, average, worst, symmetric-mix

For the microbenchmarks only (insert/probe timing, bytes per element):

```bash
cargo bench --profile release --bench filter_spillover
```

For the invertible filter (GCS vs PinSketch) comparison:

```bash
cargo bench --profile release --bench invertible_filter
```

The filter implementations are in `benches/math/filters.rs`, the end-to-end
simulation in `benches/math/filter_spillover.rs`, and the invertible filter
benchmark in `benches/math/invertible_filter.rs`.
