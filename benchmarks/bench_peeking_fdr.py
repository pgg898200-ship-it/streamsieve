"""StreamSwarmBench — Continuous Peeking Type-I Error & 256-Channel Mixed-Firehose FDR Benchmark.

Experiment A — Continuous Peeking Null Type-I Error:
  500 independent null trials x T=600 ticks, peeking every 10 ticks at nominal alpha=0.05
  across 4 distributions: Gaussian, Student-t(3), LogNormal, and 1e10_DC+1000xLoud.
  Compares:
    - streamsieve.EStream (anytime-valid Ville e >= 1/alpha = 20.0)
    - Naive causal rolling z-score (|z| > 3.0, rolling W=64)
    - Peeking two-sample scipy.stats.ttest_ind (Welch, p < 0.05)
    - river.drift.ADWIN(delta=0.05)
    - river.drift.ADWIN(delta=0.002)
    - river.drift.PageHinkley()

Experiment B — 256-Channel Mixed Firehose (5 fresh seeds, T=600, cp=180):
  4 sustained shifts (+1.5 sigma) + 4 rare 18% +3.5 sigma bursts + 32 loud 5x null channels
  + 216 normal null channels.
  Compares:
    - streamsieve.EStream (e-BH, alpha=0.05)
    - Naive |z| > 3.0 (causal rolling W=64, peeking every 10 ticks)
    - Peeking Welch t-test + BH (alpha=0.05, peeking every 10 ticks)
    - river.drift.ADWIN(delta=0.002)

Experiment B2 (Diagnostic) — Zero-Mean Symmetric +-3.5 sigma Bursts:
  Same 256-channel setup except the 4 burst channels emit zero-mean +-3.5 sigma spikes (18%),
  highlighting the trade-off between streamsieve's dual-resolution tail detector and ADWIN's
  mean-only shift detector.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
from river import drift as river_drift
from scipy import stats as sp_stats

from streamsieve import EStream


def run_experiment_a(
    n_trials: int = 500,
    n_ticks: int = 600,
    peek_every: int = 10,
    alpha: float = 0.05,
    seed: int = 202601,
) -> dict[str, Any]:
    """Run Experiment A: Continuous peeking null Type-I error across 4 distributions."""
    rng = np.random.default_rng(seed)
    e_thresh = 1.0 / alpha

    distributions: dict[str, np.ndarray] = {
        "Gaussian": rng.standard_normal((n_ticks, n_trials)),
        "Student-t(3)": rng.standard_t(df=3, size=(n_ticks, n_trials)),
        "LogNormal": rng.lognormal(mean=0.0, sigma=1.0, size=(n_ticks, n_trials)),
        "1e10_DC+1000xLoud": 1e10 + 1000.0 * rng.standard_normal((n_ticks, n_trials)),
    }

    per_dist: dict[str, dict[str, float]] = {}

    for dist_name, mat in distributions.items():
        x_mat = np.ascontiguousarray(mat, dtype=np.float64)

        # 1. streamsieve.EStream (per-channel e-values are strictly independent;
        #    checking e_value >= 1/alpha on each of 500 channels is bit-exact identical
        #    to 500 independent single-channel EStream(1) tests)
        hit_ss = np.zeros(n_trials, dtype=bool)
        with EStream(n_trials, warmup=32, ring_capacity=64, alpha=alpha) as est:
            for start in range(0, n_ticks, peek_every):
                end = min(n_ticks, start + peek_every)
                est.update_batch(x_mat[start:end])
                if end >= 40:
                    for st in est.topk(n_trials):
                        if st.e_value < e_thresh:
                            break
                        hit_ss[st.channel] = True

        # 2. Naive causal rolling z-score (|z| > 3.0, rolling W=64, strictly causal [t-65:t-1])
        hit_z = np.zeros(n_trials, dtype=bool)
        for t in range(40, n_ticks + 1, peek_every):
            win = x_mat[max(0, t - 65) : t - 1]
            mu = win.mean(axis=0)
            sd = win.std(axis=0, ddof=1)
            z = np.abs((x_mat[t - 1] - mu) / np.maximum(sd, 1e-12))
            hit_z |= z > 3.0

        # 3. Peeking two-sample Welch t-test (p < 0.05) every 10 ticks
        hit_tt = np.zeros(n_trials, dtype=bool)
        for t in range(40, n_ticks + 1, peek_every):
            mid = t // 2
            _, pvals = sp_stats.ttest_ind(x_mat[:mid], x_mat[mid:t], axis=0, equal_var=False)
            hit_tt |= np.nan_to_num(pvals, nan=1.0) < alpha

        # 4. river.drift.ADWIN(delta=0.05)
        hit_adwin_05 = 0
        # 5. river.drift.ADWIN(delta=0.002)
        hit_adwin_002 = 0
        # 6. river.drift.PageHinkley()
        hit_ph = 0

        for j in range(n_trials):
            col = x_mat[:, j].tolist()

            det_05 = river_drift.ADWIN(delta=0.05)
            for v in col:
                det_05.update(v)
                if det_05.drift_detected:
                    hit_adwin_05 += 1
                    break

            det_002 = river_drift.ADWIN(delta=0.002)
            for v in col:
                det_002.update(v)
                if det_002.drift_detected:
                    hit_adwin_002 += 1
                    break

            det_ph = river_drift.PageHinkley()
            for v in col:
                det_ph.update(v)
                if det_ph.drift_detected:
                    hit_ph += 1
                    break

        per_dist[dist_name] = {
            "streamsieve_estream": round(float(hit_ss.mean()), 4),
            "naive_rolling_z3": round(float(hit_z.mean()), 4),
            "peeking_ttest_ind": round(float(hit_tt.mean()), 4),
            "river_adwin_0.05": round(hit_adwin_05 / float(n_trials), 4),
            "river_adwin_0.002": round(hit_adwin_002 / float(n_trials), 4),
            "river_page_hinkley": round(hit_ph / float(n_trials), 4),
        }

    return {
        "n_trials": n_trials,
        "n_ticks": n_ticks,
        "peek_every": peek_every,
        "nominal_alpha": alpha,
        "far_by_distribution": per_dist,
    }


def _score_firehose(
    flagged: set[int],
    shift_set: set[int],
    burst_set: set[int],
    loud_set: set[int],
    wall_ms: float,
) -> dict[str, float]:
    true_set = shift_set | burst_set
    tp = len(flagged & true_set)
    fp = len(flagged - true_set)
    rec_shift = len(flagged & shift_set) / float(len(shift_set))
    rec_burst = len(flagged & burst_set) / float(len(burst_set))
    fp_loud = float(len(flagged & loud_set))
    fdr = (fp / float(len(flagged))) if flagged else 0.0
    precision = (tp / float(len(flagged))) if flagged else 1.0
    recall = tp / float(len(true_set))
    f1 = (2.0 * precision * recall / (precision + recall)) if (precision + recall) > 0.0 else 0.0
    return {
        "recall_shift": rec_shift,
        "recall_burst": rec_burst,
        "fp_loud": fp_loud,
        "total_fp": float(fp),
        "empirical_fdr": fdr,
        "precision": precision,
        "f1": f1,
        "wall_ms": wall_ms,
    }


def _run_mixed_firehose_suite(
    *,
    n_seeds: int = 5,
    base_seed: int = 202610,
    symmetric_bursts: bool = False,
) -> dict[str, Any]:
    n_ch, n_ticks, cp = 256, 600, 180
    alpha = 0.05
    shift_set = set(range(0, 4))
    burst_set = set(range(4, 8))
    loud_set = set(range(8, 40))

    method_names = [
        "streamsieve_ebh",
        "streamsieve_ebh_peeking",
        "naive_z3",
        "peeking_welch_bh",
        "river_adwin_0.002",
    ]
    accum: dict[str, list[dict[str, float]]] = {m: [] for m in method_names}

    for s_idx in range(n_seeds):
        rng = np.random.default_rng(base_seed + s_idx)
        scales = np.ones(n_ch, dtype=np.float64)
        scales[8:40] = 5.0  # 32 loud 5x null channels

        x_mat = rng.standard_normal((n_ticks, n_ch)) * scales
        # 4 sustained mean shifts (+1.5 sigma) after cp=180
        x_mat[cp:, 0:4] += 1.5
        # 4 rare 18% 3.5 sigma burst channels after cp=180
        burst_mask = rng.random((n_ticks - cp, 4)) < 0.18
        if symmetric_bursts:
            signs = rng.choice([-1.0, 1.0], size=(n_ticks - cp, 4))
            x_mat[cp:, 4:8] += burst_mask * signs * 3.5
        else:
            x_mat[cp:, 4:8] += burst_mask * 3.5
        x_mat = np.ascontiguousarray(x_mat, dtype=np.float64)

        # 1a. streamsieve.EStream (e-BH, alpha=0.05) — single-pass AVX2 batch + e-BH
        t0 = time.perf_counter()
        with EStream(n_ch, warmup=32, ring_capacity=64, alpha=alpha) as est:
            est.update_batch(x_mat)
            flagged_ss_batch = set(est.ebh_reject(alpha))
        ms_ss_batch = (time.perf_counter() - t0) * 1000.0
        accum["streamsieve_ebh"].append(
            _score_firehose(flagged_ss_batch, shift_set, burst_set, loud_set, ms_ss_batch)
        )

        # 1b. streamsieve.EStream (e-BH, alpha=0.05) — continuous peeking every 10 ticks
        t0 = time.perf_counter()
        flagged_ss_peek: set[int] = set()
        with EStream(n_ch, warmup=32, ring_capacity=64, alpha=alpha) as est:
            for start in range(0, n_ticks, 10):
                est.update_batch(x_mat[start : start + 10])
                if start + 10 >= 60:
                    flagged_ss_peek.update(est.ebh_reject(alpha))
        ms_ss_peek = (time.perf_counter() - t0) * 1000.0
        accum["streamsieve_ebh_peeking"].append(
            _score_firehose(flagged_ss_peek, shift_set, burst_set, loud_set, ms_ss_peek)
        )

        # 2. Naive causal rolling |z| > 3.0 (W=64, peeking every 10 ticks)
        t0 = time.perf_counter()
        hit_z = np.zeros(n_ch, dtype=bool)
        for t in range(40, n_ticks + 1, 10):
            win = x_mat[max(0, t - 65) : t - 1]
            mu = win.mean(axis=0)
            sd = win.std(axis=0, ddof=1)
            z = np.abs((x_mat[t - 1] - mu) / np.maximum(sd, 1e-12))
            hit_z |= z > 3.0
        ms_z = (time.perf_counter() - t0) * 1000.0
        accum["naive_z3"].append(
            _score_firehose(set(np.flatnonzero(hit_z).tolist()), shift_set, burst_set, loud_set, ms_z)
        )

        # 3. Peeking Welch t-test + BH (alpha=0.05, peeking every 10 ticks from t=60..600)
        t0 = time.perf_counter()
        hit_bh = np.zeros(n_ch, dtype=bool)
        for t in range(60, n_ticks + 1, 10):
            mid = t // 2
            _, pvals = sp_stats.ttest_ind(x_mat[:mid], x_mat[mid:t], axis=0, equal_var=False)
            pvals = np.clip(np.nan_to_num(pvals, nan=1.0), 0.0, 1.0)
            adj = sp_stats.false_discovery_control(pvals, method="bh")
            hit_bh |= adj < alpha
        ms_bh = (time.perf_counter() - t0) * 1000.0
        accum["peeking_welch_bh"].append(
            _score_firehose(
                set(np.flatnonzero(hit_bh).tolist()), shift_set, burst_set, loud_set, ms_bh
            )
        )

        # 4. river.drift.ADWIN(delta=0.002)
        t0 = time.perf_counter()
        flagged_adw: set[int] = set()
        for c in range(n_ch):
            adw = river_drift.ADWIN(delta=0.002)
            for v in x_mat[:, c].tolist():
                adw.update(v)
                if adw.drift_detected:
                    flagged_adw.add(c)
                    break
        ms_adw = (time.perf_counter() - t0) * 1000.0
        accum["river_adwin_0.002"].append(
            _score_firehose(flagged_adw, shift_set, burst_set, loud_set, ms_adw)
        )

    summary: dict[str, dict[str, float]] = {}
    for m, rows in accum.items():
        summary[m] = {
            k: round(float(np.mean([r[k] for r in rows])), 4) for k in rows[0].keys()
        }

    return {
        "n_channels": n_ch,
        "n_ticks": n_ticks,
        "changepoint_tick": cp,
        "n_seeds": n_seeds,
        "burst_type": "symmetric_+-3.5sigma" if symmetric_bursts else "onesided_+3.5sigma",
        "layout": {
            "sustained_shifts": 4,
            "rare_bursts_18pct": 4,
            "loud_5x_nulls": 32,
            "normal_nulls": 216,
        },
        "methods": summary,
    }


def run_benchmark() -> dict[str, Any]:
    """Run Experiments A, B, and diagnostic B2 and return structured dict."""
    return {
        "experiment_a_peeking_null_far": run_experiment_a(),
        "experiment_b_mixed_firehose": _run_mixed_firehose_suite(
            n_seeds=5, base_seed=202610, symmetric_bursts=False
        ),
        "experiment_b2_diagnostic_symmetric_bursts": _run_mixed_firehose_suite(
            n_seeds=5, base_seed=202620, symmetric_bursts=True
        ),
    }


if __name__ == "__main__":
    import json

    out = run_benchmark()
    print(json.dumps(out, indent=2))
