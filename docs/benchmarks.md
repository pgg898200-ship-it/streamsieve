# `StreamSwarmBench` — Verified Empirical Benchmark Report

- **Date (UTC):** `2026-11-22T16:36:43Z`
- **Engine:** `streamsieve 0.1.0` (C++20, `-O3 -march=native -ffp-contract=off`, AVX2+FMA enabled)
- **Hardware:** `Intel(R) Xeon(R) CPU @ 2.30GHz` (2 logical vCPUs), Linux `6.18.57-x86_64`, glibc `2.39`
- **Incumbent Versions:** `river 0.27.0`, `scipy 1.18.1`, `numpy 2.5.5`, Python `3.12.16`
- **Raw Machine-Readable Data:** [`benchmarks/results.json`](../benchmarks/results.json) (total wall runtime: `34.49 s`)

## Reproduce Every Number

```bash
# 1. Build native AVX2 core + CLI and run C++ / ASan+UBSan checks
make all test-cpp test-san

# 2. Run zero-dependency Python unit + adversarial breaker tests (53 tests)
PYTHONPATH=python python3 -m unittest discover -s tests -p "test_*.py" -v

# 3. Run full StreamSwarmBench against river, scipy, and numpy
PYTHONPATH=python .bench-venv/bin/python benchmarks/run_all.py
```

---

## 1. Throughput, Incumbent Speedups & Startup Latency

Source script: [`benchmarks/bench_throughput.py`](../benchmarks/bench_throughput.py).

### 1.1 Native AVX2 CLI & Zero-Copy Python `update_batch`

| Mode | Shape (`ch × ticks`) | Channel-Ticks | Median Latency (ms) | Throughput (M ch-ticks/s) |
|---|---:|---:|---:|---:|
| Native CLI (`./build/streamsieve bench`, internal) | `256 × 16,000` | 4,096,000 | **25.481** | **160.75** |
| Native CLI (`./build/streamsieve bench`, internal) | `1,024 × 4,000` | 4,096,000 | **36.027** | **113.69** |
| Native CLI (`./build/streamsieve bench`, internal) | `4,096 × 2,000` | 8,192,000 | **87.942** | **93.15** |
| Python zero-copy `EStream.update_batch` | `256 × 4,000` | 1,024,000 | **11.772** | **86.99** |
| Python zero-copy `EStream.update_batch` + `ebh_reject` | `256 × 4,000` | 1,024,000 | **13.792** | **74.24** |
| Python zero-copy `EStream.update_batch` | `1,024 × 2,000` | 2,048,000 | **43.972** | **46.58** |
| Python zero-copy `EStream.update_batch` + `ebh_reject` | `1,024 × 2,000` | 2,048,000 | **51.751** | **39.57** |

### 1.2 Matched Head-to-Head Throughput vs Incumbents

| Benchmark | Matrix | Median (ms) | Throughput (M ch-ticks/s) | Speedup vs Incumbent |
|---|---|---:|---:|---:|
| `streamsieve.EStream.update_batch` (zero-copy NumPy) | `64 × 2,000` | **1.946** | **65.77** | Reference (`17.05×` vs ADWIN, `54.42×` vs PH) |
| `streamsieve.EStream.update_row` (Python loop) | `64 × 2,000` | 22.579 | 5.67 | `1.47×` vs ADWIN, `4.69×` vs PH |
| `river.drift.ADWIN(delta=0.002)` | `64 × 2,000` | 33.180 | 3.858 | 1.00× |
| `river.drift.PageHinkley()` | `64 × 2,000` | 105.915 | 1.209 | 1.00× |
| `streamsieve` (`update_batch` + exact online e-BH) | `256 × 4,000` | **13.696** | **74.77** | **5.65×** vs NumPy/SciPy |
| NumPy/SciPy causal rolling z-score + BH | `256 × 4,000` | 77.355 | 13.24 | 1.00× |

### 1.3 Coordination Primitives & Cold-Start Latency

| Operation | Measured Performance |
|---|---:|
| `EvidenceLattice.observe` (uint64 IDs) | **121,292 ops/s** (`8.25 µs/op`) |
| `EvidenceLattice.observe` (string IDs, FNV-1a/SplitMix64 hashed) | **72,276 ops/s** (`13.84 µs/op`) |
| `EvidenceLattice.merge` (100-item CRDT in-place) | **315,753 merges/s** (`3.17 µs/merge`) |
| `EvidenceLattice` wire deserialize + merge (100 items, `4,188 B` SSLT / `5,235` B85 chars) | **30,342 merges/s** (`41.88 B/item`) |
| `TrustCalibrator` (4 agents, `predict_prob` + `resolve`) | **24,434 ops/s** (`40.93 µs/op`) |
| `TrustCalibrator` (4 agents, `predict_scalar` + `resolve`) | **20,657 ops/s** (`48.41 µs/op`) |
| Cold-start CLI `./build/streamsieve version` (median / p95 of 10) | **4.03 ms** / `4.32 ms` |
| Cold-start `python3 -c "import streamsieve"` (median / p95 of 10) | **24.90 ms** / `26.33 ms` (**67.9× faster**) |
| Cold-start `python3 -c "import numpy, scipy.stats, river.drift"` (median of 10) | `1,691.69 ms` |

---

## 2. Continuous-Peeking Calibration & 256-Channel Mixed Firehose FDR

Source script: [`benchmarks/bench_peeking_fdr.py`](../benchmarks/bench_peeking_fdr.py).

### 2.1 Experiment A — Continuous-Peeking Null Type-I Error

**Protocol:** 500 independent stationary null streams × $T = 600$ ticks, peeked every 10 ticks, nominal $\alpha = 0.05$.

| Stationary Null Distribution | `streamsieve` ($E \ge 20$) | Naive `\|z\| > 3` | Peeking Welch `ttest` | `ADWIN(0.05)` | `ADWIN(0.002)` | `PageHinkley` |
|---|---:|---:|---:|---:|---:|---:|
| Gaussian $\mathcal{N}(0, 1)$ | **0.010** | 0.216 | 0.476 | 0.004 | 0.000 | 0.108 |
| Heavy-tailed Student-$t(3)$ | **0.008** | 0.670 | 0.520 | 0.032 | 0.002 | 0.638 |
| Skewed LogNormal$(0, 1)$ | **0.004** | 0.834 | 0.534 | 0.070 | 0.002 | 0.862 |
| $10^{10}$ DC + $1000\times$ Loud $\mathcal{N}(0, 1)$ | **0.006** | 0.196 | 0.542 | 0.060 | 0.004 | 1.000 |

### 2.2 Experiment B — 256-Channel Mixed Firehose (One-Sided `+3.5σ` Bursts)

**Protocol:** 256 channels × 600 ticks, changepoint $t^* = 180$, 5 seeds: 4 sustained mean shifts, 4 rare 18% `+3.5σ` burst channels, 32 loud 5×-variance null channels, 216 normal null channels. Target FDR $\alpha = 0.05$.

| Method | Recall (Shift) | Recall (Burst) | Loud FPs (/32) | Emp. FDR | Precision | F1 | Wall (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `streamsieve_ebh` (single-pass) | **1.00** | 0.60 | **0.0** | **0.000** | **1.000** | **0.883** | **3.80** |
| `streamsieve_ebh_peeking` (every 10 ticks) | **1.00** | **0.75** | **0.0** | **0.040** | **0.960** | **0.909** | 52.45 |
| `river_adwin_0.002` | 1.00 | 0.60 | 0.4 | 0.054 | 0.946 | 0.862 | 47.44 |
| `peeking_welch_bh` | 1.00 | 1.00 | 1.2 | 0.321 | 0.680 | 0.800 | 183.92 |
| `naive_z3` | 0.20 | 0.30 | 7.6 | 0.966 | 0.034 | 0.060 | 7.85 |

### 2.3 Experiment B2 (Diagnostic) — Zero-Mean Symmetric `±3.5σ` Bursts

**Protocol:** Identical to Experiment B except the 4 burst channels inject symmetric zero-mean `±3.5σ` spikes (so the channel mean stays exactly 0.0 before and after $t^* = 180$).

| Method | Recall (Shift) | Recall (Burst) | Loud FPs (/32) | Emp. FDR | F1 | Wall (ms) |
|---|---:|---:|---:|---:|---:|---:|
| `streamsieve_ebh` (single-pass) | **1.00** | **0.65** | **0.0** | **0.000** | **0.897** | **3.60** |
| `streamsieve_ebh_peeking` (every 10 ticks) | **1.00** | **0.70** | **0.0** | **0.022** | **0.900** | 52.25 |
| `river_adwin_0.002` | 1.00 | **0.00** | 0.0 | 0.000 | 0.667 | 48.43 |
| `peeking_welch_bh` | 1.00 | **0.00** | 0.0 | 0.298 | 0.578 | 170.22 |
| `naive_z3` | 0.50 | 0.30 | 6.6 | 0.941 | 0.102 | 5.32 |

*Takeaway:* Mean-only detectors (`ADWIN`, Welch $t$-test) are mathematically blind (`0.00` recall) to zero-mean variance/tail bursts; `streamsieve`'s dual-resolution tail-hinge JZS + Beta-Binomial e-process catches them at `0.65–0.70` recall with `0.000–0.022` FDR and `0.0` loud-channel false positives.

---

## 3. Multi-Agent Streaming Probes (All 8 Mechanisms + Real NOAA Data)

Source script: [`benchmarks/bench_multiagent_probes.py`](../benchmarks/bench_multiagent_probes.py).

### 3.1 `echo` — Echo-Chamber Reprint Storm (200 trials)

| Swarm | Method | Brier ↓ | Brier Skill ↑ | Overwrite Churn % ↓ | Echo Inflation (ideal = 1.00×) |
|---|---|---:|---:|---:|---:|
| $W = 4$ (20 msgs, 4 unique) | **`EvidenceLattice`** | **0.0707** | **+0.7173** | **0.0%** | **1.00×** (16.0 echoes suppressed) |
| $W = 4$ | Naive LWW Blackboard | 0.1724 | +0.3102 | 92.8% | 1.00× |
| $W = 4$ | Uncalibrated Log-Odds Sum | 0.0978 | +0.6088 | 0.0% | 5.14× |
| $W = 8$ (40 msgs, 4 unique) | **`EvidenceLattice`** | **0.0786** | **+0.6857** | **0.0%** | **1.00×** (36.0 echoes suppressed) |
| $W = 8$ | Naive LWW Blackboard | 0.1942 | +0.2232 | 96.5% | 1.00× |
| $W = 8$ | Uncalibrated Log-Odds Sum | 0.1354 | +0.4584 | 0.0% | 10.39× |

### 3.2 `sift` + `voi` — 2,048-Channel Bursty Firehose (256 sites × 8 ch, 120 epochs, 10 seeds)

| Swarm | Method | Brier Skill ↑ | Recall@Top ↑ | Loud FPs ↓ | Inter-Agent Dup % ↓ |
|---|---|---:|---:|---:|---:|
| $W = 1$ (budget 4/ep) | **`VOISifter`** | **+0.2245** | **0.3333** | 0.10 | **0.00%** |
| $W = 1$ | Round-Robin (Bayes) | +0.0521 | 0.0667 | 0.00 | — |
| $W = 1$ | Naive Top-Raw-Mean | -8.1304 | 0.0000 | 48.80 | — |
| $W = 4$ (budget 4/agent/ep) | **`VOISifter`** | **+0.7018** | **0.7833** | 0.50 | **0.11%** |
| $W = 4$ | Round-Robin (Bayes) | -0.0097 | 0.0667 | 0.00 | — |
| $W = 4$ | Naive Top-Raw-Mean | -32.4213 | 0.0000 | 195.90 | — |

### 3.3 `trust` + `delay` — 4-Agent Swarm (4-Epoch Delay + Trust Flip @ Epoch 60, 20 seeds)

| Method | Overall Brier ↓ | Post-Flip Brier ↓ | Byzantine Quarantine % ↑ | Mean Quarantine Epoch | Final Bloviator Platt Slope |
|---|---:|---:|---:|---:|---:|
| **`TrustCalibrator`** | **0.1848** | **0.1840** | **100.0%** | 25.1 | `0.203` |
| Plain Exponential Weights | 0.1955 | 0.2179 | 0.0% | — | — |
| Unweighted Mean Vote | 0.2532 | 0.2539 | 0.0% | — | — |

### 3.4 `act` — Real NOAA Seattle `temp_max` (1,461 days, 4:1 Asymmetric Cost, 1-Day Delay)

- **Dataset:** [`examples/noaa_weather.csv`](../examples/noaa_weather.csv) (`1,431` post-warmup evaluated days).
- **`TrustCalibrator` Bayes-Optimal 80th-Pct Action:** mean asymmetric cost = **`3.8893`** vs Point-Mean baseline = `5.5874` (**30.39% cost reduction**; full-series `30.00%`).
- **Multi-Rate Delayed AgACI 90% Prediction Interval:** **`90.01%`** empirical coverage (full-series `89.99%`, target `90.0%`, mean interval width `9.79 °C`).

### 3.5 `bits` — Wire Sync Budget (8 Hypotheses × 32 Unique Findings = 256 Items)

| Payload Representation | Full 256-Finding State | 4-Agent All-to-All Sync | Ratio vs JSON Debate |
|---|---:|---:|---:|
| **`EvidenceLattice` (`SSLT` v1 binary)** | **10,428 B** | **32,976 B** | **10.66×** smaller (state) / **10.11×** (sync) |
| **`EvidenceLattice` (Base85 text-safe)** | **13,035 chars** | **41,220 chars** | **8.53×** smaller (state) / **8.09×** (sync) |
| Compact JSON (structured) | 29,736 B | 89,217 B | 3.74× |
| JSON Multi-Agent Prose Debate | 111,143 B | 333,429 B | 1.00× |

### 3.6 `stop` — Anytime-Valid Sequential Stopping (200 $H_0$ + 200 $H_1$ trials, $T = 400$, $\alpha = 0.05$)

| Method | $H_0$ False-Stop Rate (target ≤ 0.05) | $H_1$ Power | Mean Stop Tick ($H_1$) | Sample Savings vs Fixed |
|---|---:|---:|---:|---:|
| **`streamsieve` ($E \ge 20$)** | **0.015** | **1.000** | **120.0** | **70.0%** |
| Naive Peeking Welch `ttest` | 0.440 *(invalid!)* | 1.000 | 101.2 | — |
| Fixed-Horizon `ttest` ($T = 400$) | 0.050 | 1.000 | 400.0 | 0.0% |

---

## 4. Token Compression & Bit-Exact Lossless Drill-Down

Source script: [`benchmarks/bench_token_compression.py`](../benchmarks/bench_token_compression.py).

| Scale | Raw CSV Bytes | Raw Tokens (`char/4`) | Brief Tokens (`char/4`) | Token Reduction | Wall (ms) | e-BH Prec / Rec | Loud FPs | `fetch_raw` Max Abs Err |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `64 ch × 500 ticks` | 239,984 | 59,996 | **318** | **99.47%** (`188.7×`) | 1.54 | `1.00 / 0.75` | **0** | **`0.0`** (1,842 ticks verified) |
| `256 ch × 1,000 ticks` | 1,921,952 | 480,488 | **322** | **99.93%** (`1,492.2×`) | 6.72 | `1.00 / 1.00` | **0** | **`0.0`** (5,203 ticks verified) |
| `1,024 ch × 2,000 ticks` | 15,377,411 | 3,844,353 | **325** | **99.99%** (`11,828.8×`) | 72.26 | `1.00 / 1.00` | **0** | **`0.0`** (10,565 ticks verified) |

---

## 5. Honest Caveats & Methodology Notes

1. **Batch vs per-row Python calls:** `update_batch()` passes a single contiguous `double*` buffer into AVX2 C++20 (`65.8–87.0 M ch-ticks/s`). Python loops calling `update_row()` one tick at a time incur `ctypes` marshaling overhead (`5.67 M ch-ticks/s`, still `1.47×` faster than `river.ADWIN`).
2. **Frequent peeking wall time:** Single-pass `update_batch + ebh_reject` on 256×600 takes **3.80 ms** (`12.5×` faster than `river.ADWIN`). Calling `ebh_reject()` 55 times per 600 ticks takes `52.45 ms` (comparable to `ADWIN`'s `47.44 ms`, while delivering higher F1 `0.909` vs `0.862`, lower FDR `0.040` vs `0.054`, and `0.0` loud FPs).
3. **Short-horizon rare bursts:** On `64 ch × 500 ticks`, `ebh_recall` is `0.75` (`1.00` shift recall, `0.50` rare-burst recall) and rises to `1.00 / 1.00` at `1,000` and `2,000` ticks as pre/post burst counts accumulate.
4. **Hardware variance:** Numbers reflect a 2-vCPU virtualized Intel Xeon @ 2.30 GHz with AVX2+FMA. Bare-metal AVX2/AVX-512 workstations typically achieve higher native throughput.
