# `streamsieve` vs Multi-Agent Debate, LWW Blackboards & Naive Voting

> **Benchmark Date:** 2026-11-22 | **Source:** [`benchmarks/results.json`](../../benchmarks/results.json) (`echo`, `bits`, `trust` + `delay`, `act` probes)

## Why Multi-Agent Swarms Degrade When They "Talk" Over Streams

When 4–8 LLM or specialized agents monitor a live stream and share conclusions over a shared blackboard, chat thread, or gossip mesh, three failure modes dominate:

1. **Echo-Chamber Double-Counting (`echo`):** Agent A observes one noisy spike, posts it, and Agents B–H restate it. Adding log-odds naively inflates confidence by **5.14× ($W=4$) to 10.39× ($W=8$)**; overwriting a shared Last-Write-Wins (LWW) blackboard causes **92.8–96.5% overwrite churn**, erasing earlier findings.
2. **Prose Wire Bloat (`bits`):** Passing JSON debate transcripts across 4 agents for 256 unique findings burns **111 KB per state / 333 KB all-to-all sync** in context tokens and network bandwidth.
3. **Uncalibrated Bloviators & Byzantine Flip Collapse (`trust` + `delay`):** Under 4-epoch delayed ground truth and a mid-run reliability flip, naive voting gives equal weight to 99%-confident random agents and actively poisoned agents, while plain Exponential Weights never quarantines Byzantine nodes (`0%` quarantine) and lags after a flip.

## Head-to-Head Measured Comparison

| Probe & Metric | `streamsieve` (`EvidenceLattice` + `TrustCalibrator`) | Naive LWW Blackboard / Mean Vote | Uncalibrated Log-Odds / Plain ExpWeights | JSON Prose Debate |
|---|---:|---:|---:|---:|
| **`echo` ($W=8$): Brier Score ↓ / Skill ↑** | **0.0786 / +0.6857** | 0.1942 / +0.2232 | 0.1354 / +0.4584 | — |
| **`echo` ($W=8$): Overwrite Churn % ↓** | **0.0%** | 96.5% | 0.0% | — |
| **`echo` ($W=8$): Echo Inflation (ideal 1.00×)** | **1.00×** (36/36 echoes suppressed) | 1.00× *(loses data)* | **10.39×** *(overconfident)* | — |
| **`bits`: 256-Finding Full State Size ↓** | **10,428 B** SSLT / **13,035** Base85 | — | 29,736 B (compact JSON) | 111,143 B (**10.7× larger**) |
| **`bits`: 4-Agent All-to-All Sync Size ↓** | **32,976 B** SSLT / **41,220** Base85 | — | 89,217 B (compact JSON) | 333,429 B (**10.1× larger**) |
| **`trust` + `delay`: Overall / Post-Flip Brier ↓** | **0.1848 / 0.1840** | 0.2532 / 0.2539 (Mean Vote) | 0.1955 / 0.2179 (Plain ExpWeights) | — |
| **`trust` + `delay`: Byzantine Quarantine % ↑** | **100.0%** (mean epoch 25.1) | 0.0% | 0.0% | — |
| **`act` (NOAA Seattle, 4:1 cost): Cost ↓ / 90% CI Cov.** | **3.889 (-30.39%) / 90.01%** | 5.587 (Point-Mean) | — | — |

## Algebraic & Statistical Guarantees

- **Bit-exact Join-Semilattice CRDT (`EvidenceLattice`):** `merge(A, B) == merge(B, A)`, `merge(merge(A, B), C) == merge(A, merge(B, C))`, and `merge(A, A) == A` — verified across 600 randomized adversarial trials in both `strict_origin` and `max_reliability` modes plus a 500× echo storm with `0.0` ULP drift.
- **Vovk–Wang Admissible E-Merging:** Even when distinct agents report distinct provenance IDs from correlated sensors, cross-agent e-values merge via weighted arithmetic averaging (the essentially unique symmetric admissible e-merger under arbitrary dependence), with explicitly zero-weighted/quarantined agents strictly excluded.
- **When prose debate still helps:** Open-ended qualitative brainstorming, creative code synthesis, or hypothesis generation where no numerical evidence or quantitative ground truth exists. Pair qualitative LLM agents *with* `EvidenceLattice` + `TrustCalibrator` as the quantitative belief & verification spine.
