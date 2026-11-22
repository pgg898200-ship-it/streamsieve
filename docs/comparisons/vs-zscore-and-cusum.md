# `streamsieve` vs Rolling Z-Scores, Welch $t$-Tests & CUSUM

> **Benchmark Date:** 2026-11-22 | **Versions:** `streamsieve 0.2.0` vs `numpy 2.5.5` + `scipy 1.18.1` | **Source:** [`benchmarks/results.json`](../../benchmarks/results.json)

## Why Classical Fixed-Horizon Tests Break Under Continuous Monitoring

The most common telemetry alerting code written by engineers and AI agents computes either a rolling `|z| > 3` threshold, a repeated two-sample Welch $t$-test (`scipy.stats.ttest_ind`), or an unnormalized CUSUM. Under continuous monitoring ("peeking" every 10 ticks), **all three violate their nominal false-alarm guarantees**:

- **Optional stopping inflation:** Checking a fixed-horizon $p < 0.05$ test 60 times inflates Type-I error on a pure Gaussian null from **5% to 47.6%**.
- **Heavy-tail / skew explosion:** Rolling `|z| > 3` assumes Gaussian tails; on Student-$t(3)$ it alarms **67.0%** of null streams, and on LogNormal it alarms **83.4%**.
- **Non-stationary breakdown:** On 24-tick diurnal waves, linear trends, and random walks, peeking Welch $t$-tests emit **83.5%–100.0%** false alarms.
- **Multiple-testing collapse:** Applying Benjamini–Hochberg (`BH`) to peeking $p$-values across 256 channels still suffers **32.1% empirical FDR** (6.4× above nominal 5%).

## Head-to-Head Measured Comparison

| Metric | `streamsieve 0.2.0` (`EStream` + e-BH) | Naive Rolling `\|z\| > 3` | Peeking Welch `ttest` + BH | Fixed-Horizon `ttest` ($T=400$) |
|---|---:|---:|---:|---:|
| **Throughput (`256 ch × 4,000 ticks`)** | **7.58 ms** (`135.18 M/s`) — **10.55× faster** | — | `79.92 ms` (`12.81 M/s` NumPy/SciPy z+BH) | — |
| **Null Peeking FAR — Gaussian** ($\alpha=0.05$) | **0.010** | 0.216 | 0.476 | 0.050 *(single peek only)* |
| **Null Peeking FAR — Student-$t(3)$** | **0.008** | 0.670 | 0.520 | — |
| **Null Peeking FAR — LogNormal** | **0.004** | 0.834 | 0.534 | — |
| **Null Peeking FAR — $10^{10}$ DC + 1000×** | **0.006** | 0.196 | 0.542 | — |
| **Non-Stationary Null FAR (Diurnal / Trend / RW / Both)** | **0.010 – 0.050** (`--season` / `--detrend`) | 0.000 – 0.645 | **0.835 – 1.000** | — |
| **256-Ch Mixed Firehose (Emp. FDR / F1)** | **0.000 / 0.883** (`2.8 ms`); peeking **0.040 / 0.909** (`40.8 ms`) | 0.966 / 0.059 (`5.6 ms`) | 0.321 / 0.799 (`183.0 ms`) | — |
| **256-Ch Zero-Mean `±3.5σ` Bursts (Burst Recall)** | **0.65–0.70** | 0.30 *(with 94.1% FDR)* | **0.00** *(blind to zero-mean bursts)* | — |
| **64-Ch Diurnal (`P=24`, `6σ`) Firehose (F1 / Wall)** | **0.799 / 1.72 ms** | — | 0.667 / 125.49 ms (**73× slower**) | — |
| **Sequential Stopping ($H_0$ FAR / $H_1$ Power / Stop Tick)** | **0.015 / 1.00 / 120.0 (70.0% saved)** | — | 0.440 / 1.00 / 101.2 *(invalid)* | 0.050 / 1.00 / 400.0 *(0% saved)* |

## When Classical Tests Are Still Appropriate

- **Fixed-sample offline A/B analysis** where you collect exactly $N$ observations in advance and run a single non-sequential test once at the end (`scipy.stats.ttest_ind` has slightly higher fixed-sample power because it does not pay the anytime-valid optional-stopping tax).
- **Known parametric in-control/out-of-control distributions** (e.g., exact Gaussian process with known $\mu_0, \sigma_0$ and known shift size $\delta$) where a tuned Lorden-optimal parametric CUSUM achieves minimum worst-case detection delay.

For **real-world, distribution-unknown, multi-channel streaming with continuous peeking**, `streamsieve` gives **10.55×–73× higher speed** and **strict anytime-valid FDR control** with zero NumPy/SciPy dependencies.
