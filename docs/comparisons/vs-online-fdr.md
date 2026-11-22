# `streamsieve` Online e-BH vs Benjamini–Hochberg (BH), BY & Bonferroni

> **Benchmark Date:** 2026-11-22 | **Source:** [`benchmarks/results.json`](../../benchmarks/results.json), Wang & Ramdas (2022) *False discovery rate control with e-values*

## The Multiple-Testing Problem in Streaming Telemetry

When monitoring $M$ channels simultaneously (e.g., $M = 256$ or $M = 4{,}096$ microservices, sensors, or trading signals) while peeking continuously over time, classical $p$-value multiple-testing corrections run into a mathematical wall:

| Procedure | Valid Under Continuous Peeking? | Valid Under Arbitrary Cross-Channel Dependence? | Penalty Factor for $M = 1{,}000$ | Emp. FDR on 256-Ch Peeking Firehose ($\alpha = 0.05$) |
|---|---|---|---|---:|
| **`streamsieve` Online e-BH** ($E_{(k)} \ge \frac{M}{\alpha k}$) | **Yes** (anytime-valid e-processes) | **Yes** (exact Markov bound, zero dependence assumptions) | **1×** (no $\log M$ penalty) | **0.000** (single-pass) / **0.040** (55× peeking) |
| **Benjamini–Hochberg (BH)** on peeking $p$-values | **No** (peeking breaks super-uniformity) | **No** (requires PRDS / independence) | 1× *(invalid under peeking)* | **0.321** *(6.4× over nominal $\alpha$!)* |
| **Benjamini–Yekutieli (BY)** on fixed-horizon $p$-values | **No** | Yes | $\sum_{i=1}^{1000} \frac{1}{i} \approx 7.49\times$ harsher | — *(cannot peek)* |
| **Bonferroni** ($p \le \alpha / M$) | Only with anytime $p$-values | Yes | Controls FWER, loses step-up power when $k^* > 1$ | — |

## Why Online e-BH Wins for Multi-Agent Firehoses

1. **Zero Dependence Assumptions:** Microservice latencies, cluster GPUs, and financial tickers are heavily correlated (often negatively as well as positively, violating BH's PRDS requirement). Wang & Ramdas (2022) proved that step-up **e-BH** on e-values ($\mathbb{E}_{H_0}[E_c] \le 1$) guarantees $\operatorname{FDR} \le \alpha$ under **arbitrary unknown cross-channel dependence** without paying Benjamini–Yekutieli's $\approx \ln M$ power penalty.
2. **Valid at Any Data-Dependent Stopping Time:** Because each channel's `EStream` statistic is a valid e-process ($\mathbb{E}_{H_0}[E_{c,\tau}] \le 1$ at every stopping time $\tau$), agents can query `ebh_reject(alpha)` on demand—every tick, every 10 ticks, or whenever another agent triggers an investigation—and FDR stays rigorously bounded below $\alpha$.
3. **Exact Consistency Across All APIs:** `EStream.ebh_reject()`, `EStream.topk()`, `EStream.brief()`, and `EStream.channel_status(c).rejected_ebh` all evaluate the exact same cached step-up threshold $M / (\alpha k^*)$.

## Honest Power Trade-Off

- For a **single fixed-horizon offline batch** of **independent** $p$-values (no peeking, known independence), classical BH has slightly higher power than e-BH because valid e-values satisfy $E \le 1/p$ loosely (typical e-value calibration factor vs $p$-values).
- Under **continuous peeking, heavy tails, correlated channels, or multi-agent drill-down**, BH loses FDR control (`0.321` empirical FDR in `StreamSwarmBench`), whereas `streamsieve` e-BH delivers **higher F1 (`0.883–0.909` vs `0.800`)**, **valid FDR (`0.000–0.040`)**, and **48× lower latency (`3.8 ms` vs `183.9 ms`)**.
