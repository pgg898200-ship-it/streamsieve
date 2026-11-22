# `streamsieve` vs `river` (ADWIN & Page-Hinkley)

> **Benchmark Date:** 2026-11-22 | **Versions:** `streamsieve 0.1.0` (AVX2+FMA) vs `river 0.27.0` | **Source:** [`benchmarks/results.json`](../../benchmarks/results.json)

## TL;DR Decision Matrix

- **Choose `streamsieve`** when monitoring **multi-channel numeric streams** (16–4,096+ channels) for AI agents/ops/quant workflows requiring: high throughput (`17×–54×` faster), zero runtime dependencies (`<25 ms` Python import / `4 ms` CLI), exact cross-channel **online e-BH FDR control**, **zero-mean burst/variance detection**, **lossless raw-tick drill-down**, or **multi-agent CRDT belief fusion & Byzantine trust calibration**.
- **Choose `river`** when you need a **general-purpose online supervised ML framework** (Hoeffding trees, online linear/logistic regression, streaming feature pipelines, online clustering, or recommender models). `streamsieve` intentionally does not train general online ML models.

## Head-to-Head Measured Comparison

| Dimension | `streamsieve 0.1.0` (`EStream`) | `river 0.27.0` (`ADWIN`) | `river 0.27.0` (`PageHinkley`) |
|---|---:|---:|---:|
| **Runtime Dependencies** | **0** (C++20 `.so` + Python stdlib) | `numpy`, `scipy`, `pandas`, Cython | `numpy`, `scipy`, `pandas`, Cython |
| **Cold Import Latency** (median of 10) | **24.90 ms** (`4.03 ms` native CLI) | `1,691.69 ms` (`numpy+scipy+river`) | `1,691.69 ms` (`numpy+scipy+river`) |
| **Throughput (`64 ch × 2,000 ticks`)** | **1.95 ms** (`65.77 M/s` batch) / `22.58 ms` (row loop) | `33.18 ms` (`3.86 M/s`) — **17.1× slower** | `105.92 ms` (`1.21 M/s`) — **54.4× slower** |
| **Null Peeking FAR — Gaussian** ($\alpha = 0.05$) | **0.010** | 0.004 (`delta=0.05`) / 0.000 (`0.002`) | 0.108 *(uncalibrated)* |
| **Null Peeking FAR — Student-$t(3)$** | **0.008** | 0.032 (`0.05`) / 0.002 (`0.002`) | 0.638 *(broken)* |
| **Null Peeking FAR — LogNormal** | **0.004** | 0.070 (`0.05`) / 0.002 (`0.002`) | 0.862 *(broken)* |
| **Null Peeking FAR — $10^{10}$ DC + 1000× Loud** | **0.006** | 0.060 (`0.05`) / 0.004 (`0.002`) | 1.000 *(100% false alarms)* |
| **256-Ch Mixed Firehose (Emp. FDR / F1 / Wall)** | **0.000 / 0.883 / 3.8 ms** (single-pass); **0.040 / 0.909 / 52.4 ms** (peeking) | 0.054 / 0.862 / 47.4 ms (`delta=0.002`) | — |
| **Zero-Mean `±3.5σ` Bursts (Burst Recall / F1)** | **0.65–0.70 / 0.897–0.900** | **0.00** / 0.667 *(blind to zero-mean bursts)* | — |
| **Multi-Channel FDR Control** | Built-in exact Wang–Ramdas online e-BH | Manual `delta` tuning per channel (no FDR guarantee) | None |
| **Multi-Agent CRDT / VOI / Byzantine Trust / MCP** | Built-in (`EvidenceLattice`, `VOISifter`, `TrustCalibrator`, 7 MCP tools) | Not in scope | Not in scope |

## Key Technical Differences

1. **Zero-Mean Burst Blindness:** `ADWIN` tests differences in sub-window means. When a telemetry channel erupts into symmetric `±3.5σ` jitter or tail bursts while its mean remains zero, `ADWIN` records **0.00 recall**. `streamsieve` combines a bounded-squash mean-shift JZS/sign e-process with a dedicated tail-hinge + dual-threshold Beta-Binomial burst e-process, catching **65–70%** of zero-mean burst channels at `0.000–0.022` empirical FDR.
2. **Scale & DC-Offset Invariance:** `PageHinkley` uses an additive threshold (`lambda`) tied to raw units, triggering **100% false alarms** on a stationary `1e10 + 1000·N(0, 1)` stream. `streamsieve`'s direct-sort median + Winsorized-MAD normalization keeps FAR at **0.006** across arbitrary offsets and scales.
3. **Cross-Channel FDR Without Hand-Tuning `delta`:** Running `ADWIN` across 256 or 4,096 channels forces manual `delta` guessing (e.g., `0.002`). `streamsieve` outputs valid e-values per channel and runs exact Wang–Ramdas online e-BH so $\operatorname{FDR} \le \alpha$ holds automatically under arbitrary cross-channel correlation and continuous peeking.
4. **Honest Trade-Off:** If you only have 1 univariate scalar stream updated once per second inside an existing `river` supervised-learning pipeline, `river.drift.ADWIN` is already in memory and integrates cleanly with `river`'s estimator API.
