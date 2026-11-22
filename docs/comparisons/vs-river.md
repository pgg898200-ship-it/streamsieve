# `streamsieve` vs `river` (ADWIN & Page-Hinkley)

> **Benchmark Date:** 2026-11-22 | **Versions:** `streamsieve 0.2.0` (8-wide AVX2+FMA) vs `river 0.27.0` | **Source:** [`benchmarks/results.json`](../../benchmarks/results.json)

## TL;DR Decision Matrix

- **Choose `streamsieve`** when monitoring **multi-channel numeric streams** (16–4,096+ channels) for AI agents/ops/quant workflows requiring: high throughput (**26.9×–92.3× faster**), zero runtime dependencies (`25.3 ms` Python import / `3.87 ms` CLI), exact cross-channel **online e-BH FDR control**, **zero-mean burst/variance detection**, built-in **causal deseasonalizing & detrending** (`season_period`, `detrend_alpha`), **lossless raw-tick drill-down**, or **multi-agent CRDT belief fusion & Byzantine trust calibration**. Includes a 1-line drop-in `river.drift` adapter (`from streamsieve import EStreamDrift as ADWIN`).
- **Choose `river`** when you need a **general-purpose online supervised ML framework** (Hoeffding trees, online linear/logistic regression, streaming feature pipelines, online clustering, or recommender models). `streamsieve` intentionally does not train general online ML models.

## Head-to-Head Measured Comparison

| Dimension | `streamsieve 0.2.0` (`EStream`) | `river 0.27.0` (`ADWIN`) | `river 0.27.0` (`PageHinkley`) |
|---|---:|---:|---:|
| **Runtime Dependencies** | **0** (C++20 `.so`/`.pyd` + Python stdlib) | `numpy`, `scipy`, `pandas`, Cython | `numpy`, `scipy`, `pandas`, Cython |
| **Cold Import Latency** (median of 10) | **25.30 ms** (`3.87 ms` native CLI) | `1,791.01 ms` (`numpy+scipy+river`) | `1,791.01 ms` (`numpy+scipy+river`) |
| **Throughput (`64 ch × 2,000 ticks`)** | **1.21 ms** (`105.75 M/s` batch) / `25.76 ms` (row loop) | `32.62 ms` (`3.92 M/s`) — **26.9× slower** | `111.67 ms` (`1.15 M/s`) — **92.3× slower** |
| **Null Peeking FAR — Gaussian** ($\alpha = 0.05$) | **0.010** | 0.004 (`delta=0.05`) / 0.000 (`0.002`) | 0.108 *(uncalibrated)* |
| **Null Peeking FAR — Student-$t(3)$** | **0.008** | 0.032 (`0.05`) / 0.002 (`0.002`) | 0.638 *(broken)* |
| **Null Peeking FAR — LogNormal** | **0.004** | 0.070 (`0.05`) / 0.002 (`0.002`) | 0.862 *(broken)* |
| **Null Peeking FAR — $10^{10}$ DC + 1000× Loud** | **0.006** | 0.060 (`0.05`) / 0.004 (`0.002`) | 1.000 *(100% false alarms)* |
| **Non-Stationary Null FAR (LinearTrend / RandomWalk / Diurnal+Trend)** | **0.025 – 0.050** (`--season` / `--detrend`) | **1.000** *(100% false alarms)* | **1.000** *(100% false alarms)* |
| **256-Ch Mixed Firehose (Emp. FDR / F1 / Wall)** | **0.000 / 0.883 / 2.8 ms** (single-pass); **0.040 / 0.909 / 40.8 ms** (peeking) | 0.054 / 0.862 / 49.3 ms (`delta=0.002`) | — |
| **Zero-Mean `±3.5σ` Bursts (Burst Recall / F1)** | **0.65–0.70 / 0.897–0.900** | **0.00** / 0.667 *(blind to zero-mean bursts)* | — |
| **64-Ch Diurnal (`P=24`, `6σ`) Firehose (F1 / Wall)** | **0.799 / 1.72 ms** (`1.00` shift, `0.35` burst) | 0.618 / 11.57 ms (`0.90` shift, `0.00` burst) | — |
| **Multi-Channel FDR Control** | Built-in exact Wang–Ramdas online e-BH | Manual `delta` tuning per channel (no FDR guarantee) | None |
| **Multi-Agent CRDT / VOI / Byzantine Trust / MCP** | Built-in (`EvidenceLattice`, `VOISifter`, `TrustCalibrator`, 7 MCP tools) | Not in scope | Not in scope |

## Key Technical Differences

1. **Zero-Mean Burst Blindness:** `ADWIN` tests differences in sub-window means. When a telemetry channel erupts into symmetric `±3.5σ` jitter or tail bursts while its mean remains zero, `ADWIN` records **0.00 recall**. `streamsieve` combines a bounded-squash mean-shift JZS/sign e-process with a dedicated tail-hinge + dual-threshold Beta-Binomial burst e-process, catching **65–70%** of zero-mean burst channels at `0.000–0.022` empirical FDR.
2. **Non-Stationary Diurnal & Trending Streams:** On linear ramps, random walks, and diurnal+trend nulls, `ADWIN(0.002)` and `PageHinkley` both emit **100% false alarms**. `streamsieve`'s strictly causal per-phase median deseasonalizer (`season_period`) and first-difference/EWMA detrender (`detrend_alpha`) hold null FAR at **0.010–0.050** while beating `ADWIN` on 64-ch diurnal firehose F1 (`0.799` vs `0.618`, **6.7× faster**).
3. **Faster Even Under 55× Continuous Peeking:** On the 256-channel mixed firehose, `streamsieve` single-pass runs in **2.8 ms** (**17.6× faster** than `ADWIN`), and even 55× continuous peeking (`40.8 ms`) is faster than `ADWIN` (`49.3 ms`) while delivering higher F1 (`0.909` vs `0.862`) and `0.0` loud-channel false positives.
4. **Honest Trade-Off:** If you already run a univariate `river` supervised-learning pipeline and need online trees/classifiers/regressors, stay in `river` — or drop `from streamsieve import EStreamDrift as ADWIN` in as a 1-line drift-detector upgrade with zero extra dependencies.
