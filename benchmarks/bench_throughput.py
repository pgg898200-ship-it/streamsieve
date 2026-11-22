"""StreamSwarmBench — Native AVX2 & Python Throughput, Incumbent Speedups & Cold-Start Benchmark.

Measures wall-clock latency and throughput (median of >=5 timed runs after warmup) across:
  1. streamsieve native C++20 AVX2 CLI (`./build/streamsieve bench`) at (256,16000), (1024,4000), (4096,2000)
  2. streamsieve Python `EStream.update_batch` (zero-copy float64 np.ndarray) at (256,4000) and (1024,2000)
  3. river 0.27.0 (`river.drift.ADWIN` and `river.drift.PageHinkley`) vs streamsieve on identical (64,2000) matrix
  4. numpy/scipy vectorized causal rolling z-score + BH (`scipy.stats.false_discovery_control`) on identical (256,4000) matrix
  5. `EvidenceLattice` single observe ops/s, 100-item CRDT merge ops/s, and SSLT wire bytes/round-trip
  6. `TrustCalibrator` predict_prob+resolve and predict_scalar+resolve ops/s
  7. Cold-start latency (median of 10 runs): native CLI `./build/streamsieve version` vs `python -c "import streamsieve"`
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from typing import Any, Callable

import numpy as np
from river import drift as river_drift
from scipy import special as sp_special
from scipy import stats as sp_stats

import streamsieve
from streamsieve import EStream, EvidenceLattice, TrustCalibrator

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_BIN = REPO_ROOT / "build" / "streamsieve"

_CLI_BENCH_RE = re.compile(
    r"in\s+([0-9.]+)\s+ms\s+\(([0-9.]+)M\s+ch-ticks/s\)\s+\|\s+e-BH\s+rejected=(\d+)"
)


def _median_timed(fn: Callable[[], Any], *, warmup: int = 1, repeats: int = 5) -> dict[str, float]:
    """Run warmup iterations, then return median/min/p95 wall time in milliseconds."""
    for _ in range(warmup):
        fn()
    times_ms: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times_ms.append((time.perf_counter() - t0) * 1000.0)
    times_sorted = sorted(times_ms)
    p95_idx = min(len(times_sorted) - 1, int(round(0.95 * (len(times_sorted) - 1))))
    return {
        "median_ms": float(statistics.median(times_sorted)),
        "min_ms": float(times_sorted[0]),
        "p95_ms": float(times_sorted[p95_idx]),
        "repeats": float(repeats),
    }


def _make_benchmark_matrix(n_ticks: int, n_channels: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    mat = rng.standard_normal((n_ticks, n_channels)).astype(np.float64)
    cp = n_ticks // 3
    n_shift = min(8, n_channels)
    mat[cp:, :n_shift] += 1.6
    return np.ascontiguousarray(mat, dtype=np.float64)


def bench_native_cli() -> list[dict[str, Any]]:
    """Benchmark native C++20 AVX2 CLI `./build/streamsieve bench` across three scales."""
    shapes = [(256, 16000), (1024, 4000), (4096, 2000)]
    results: list[dict[str, Any]] = []

    for n_ch, n_ticks in shapes:
        cmd = [str(CLI_BIN), "bench", "--channels", str(n_ch), "--ticks", str(n_ticks)]
        # Warmup run
        subprocess.run(cmd, check=True, capture_output=True, text=True)

        internal_ms: list[float] = []
        internal_m_rate: list[float] = []
        wall_ms: list[float] = []
        rejected_last = 0

        repeats = 5
        for _ in range(repeats):
            t0 = time.perf_counter()
            proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
            wall_ms.append((time.perf_counter() - t0) * 1000.0)
            m = _CLI_BENCH_RE.search(proc.stdout)
            if not m:
                raise RuntimeError(f"Unexpected CLI bench output: {proc.stdout!r}")
            internal_ms.append(float(m.group(1)))
            internal_m_rate.append(float(m.group(2)))
            rejected_last = int(m.group(3))

        med_int_ms = float(statistics.median(internal_ms))
        med_wall_ms = float(statistics.median(wall_ms))
        ch_ticks = n_ch * n_ticks
        results.append(
            {
                "n_channels": n_ch,
                "n_ticks": n_ticks,
                "channel_ticks": ch_ticks,
                "internal_median_ms": round(med_int_ms, 3),
                "internal_ch_ticks_per_sec": round(ch_ticks / (med_int_ms / 1000.0), 1),
                "internal_million_ch_ticks_per_sec": round(
                    float(statistics.median(internal_m_rate)), 2
                ),
                "subprocess_wall_median_ms": round(med_wall_ms, 3),
                "ebh_rejected": rejected_last,
                "repeats": repeats,
            }
        )
    return results


def bench_python_batch() -> list[dict[str, Any]]:
    """Benchmark Python `EStream.update_batch` zero-copy on float64 NumPy matrices."""
    shapes = [(256, 4000), (1024, 2000)]
    results: list[dict[str, Any]] = []

    for idx, (n_ch, n_ticks) in enumerate(shapes):
        mat = _make_benchmark_matrix(n_ticks, n_ch, seed=101 + idx)
        ch_ticks = n_ch * n_ticks

        def run_batch_only() -> None:
            with EStream(n_ch, warmup=32, ring_capacity=256, alpha=0.05) as est:
                est.update_batch(mat)

        def run_batch_and_ebh() -> int:
            with EStream(n_ch, warmup=32, ring_capacity=256, alpha=0.05) as est:
                est.update_batch(mat)
                return len(est.ebh_reject(0.05))

        t_only = _median_timed(run_batch_only, warmup=2, repeats=7)
        t_ebh = _median_timed(run_batch_and_ebh, warmup=2, repeats=7)

        results.append(
            {
                "n_channels": n_ch,
                "n_ticks": n_ticks,
                "channel_ticks": ch_ticks,
                "update_batch_median_ms": round(t_only["median_ms"], 3),
                "update_batch_ch_ticks_per_sec": round(
                    ch_ticks / (t_only["median_ms"] / 1000.0), 1
                ),
                "update_batch_million_ch_ticks_per_sec": round(
                    (ch_ticks / (t_only["median_ms"] / 1000.0)) / 1e6, 2
                ),
                "update_batch_plus_ebh_median_ms": round(t_ebh["median_ms"], 3),
                "update_batch_plus_ebh_million_ch_ticks_per_sec": round(
                    (ch_ticks / (t_ebh["median_ms"] / 1000.0)) / 1e6, 2
                ),
                "repeats": 7,
            }
        )
    return results


def bench_river_matched() -> dict[str, Any]:
    """Compare streamsieve vs river 0.27.0 ADWIN & PageHinkley on the EXACT same (64, 2000) matrix."""
    n_ch, n_ticks = 64, 2000
    ch_ticks = n_ch * n_ticks
    mat = _make_benchmark_matrix(n_ticks, n_ch, seed=202)
    # Pre-convert columns to Python float lists so river pays zero NumPy indexing overhead
    cols_list = [mat[:, c].tolist() for c in range(n_ch)]

    def run_streamsieve_batch() -> int:
        with EStream(n_ch, warmup=32, ring_capacity=256, alpha=0.05) as est:
            est.update_batch(mat)
            return len(est.ebh_reject(0.05))

    def run_streamsieve_row() -> int:
        with EStream(n_ch, warmup=32, ring_capacity=256, alpha=0.05) as est:
            for t in range(n_ticks):
                est.update_row(mat[t])
            return len(est.ebh_reject(0.05))

    def run_river_adwin() -> int:
        detections = 0
        for c in range(n_ch):
            det = river_drift.ADWIN(delta=0.002)
            for v in cols_list[c]:
                det.update(v)
                if det.drift_detected:
                    detections += 1
        return detections

    def run_river_page_hinkley() -> int:
        detections = 0
        for c in range(n_ch):
            det = river_drift.PageHinkley()
            for v in cols_list[c]:
                det.update(v)
                if det.drift_detected:
                    detections += 1
        return detections

    t_ss_batch = _median_timed(run_streamsieve_batch, warmup=2, repeats=7)
    t_ss_row = _median_timed(run_streamsieve_row, warmup=1, repeats=5)
    t_adwin = _median_timed(run_river_adwin, warmup=1, repeats=5)
    t_ph = _median_timed(run_river_page_hinkley, warmup=1, repeats=5)

    r_ss_batch = ch_ticks / (t_ss_batch["median_ms"] / 1000.0)
    r_ss_row = ch_ticks / (t_ss_row["median_ms"] / 1000.0)
    r_adwin = ch_ticks / (t_adwin["median_ms"] / 1000.0)
    r_ph = ch_ticks / (t_ph["median_ms"] / 1000.0)

    return {
        "n_channels": n_ch,
        "n_ticks": n_ticks,
        "channel_ticks": ch_ticks,
        "streamsieve_update_batch": {
            "median_ms": round(t_ss_batch["median_ms"], 3),
            "ch_ticks_per_sec": round(r_ss_batch, 1),
            "million_ch_ticks_per_sec": round(r_ss_batch / 1e6, 2),
        },
        "streamsieve_update_row": {
            "median_ms": round(t_ss_row["median_ms"], 3),
            "ch_ticks_per_sec": round(r_ss_row, 1),
            "million_ch_ticks_per_sec": round(r_ss_row / 1e6, 2),
        },
        "river_adwin": {
            "median_ms": round(t_adwin["median_ms"], 3),
            "ch_ticks_per_sec": round(r_adwin, 1),
            "million_ch_ticks_per_sec": round(r_adwin / 1e6, 3),
            "speedup_streamsieve_batch_vs_adwin": round(r_ss_batch / r_adwin, 2),
            "speedup_streamsieve_row_vs_adwin": round(r_ss_row / r_adwin, 2),
        },
        "river_page_hinkley": {
            "median_ms": round(t_ph["median_ms"], 3),
            "ch_ticks_per_sec": round(r_ph, 1),
            "million_ch_ticks_per_sec": round(r_ph / 1e6, 3),
            "speedup_streamsieve_batch_vs_ph": round(r_ss_batch / r_ph, 2),
            "speedup_streamsieve_row_vs_ph": round(r_ss_row / r_ph, 2),
        },
    }


def bench_numpy_scipy_matched() -> dict[str, Any]:
    """Compare streamsieve vs vectorized NumPy/SciPy causal rolling z-score + BH on (256, 4000)."""
    n_ch, n_ticks = 256, 4000
    ch_ticks = n_ch * n_ticks
    mat = _make_benchmark_matrix(n_ticks, n_ch, seed=303)
    win = 64

    def run_streamsieve() -> int:
        with EStream(n_ch, warmup=32, ring_capacity=256, alpha=0.05) as est:
            est.update_batch(mat)
            return len(est.ebh_reject(0.05))

    def run_numpy_scipy_rolling_z_bh() -> int:
        # Strictly causal rolling window W=64 via prefix sums (vectorized in NumPy)
        # For each tick t >= win, mean & std use strictly [t - win, t)
        pad = np.zeros((1, n_ch), dtype=np.float64)
        cs = np.vstack([pad, np.cumsum(mat, axis=0)])
        cs2 = np.vstack([pad, np.cumsum(mat * mat, axis=0)])
        win_sum = cs[win:n_ticks] - cs[: n_ticks - win]
        win_sum2 = cs2[win:n_ticks] - cs2[: n_ticks - win]
        mu = win_sum / float(win)
        var = np.maximum((win_sum2 - (win_sum * win_sum) / float(win)) / float(win - 1), 1e-12)
        z = np.abs(mat[win:] - mu) / np.sqrt(var)
        # Per-channel max |z| -> two-sided normal p-value + exact scipy BH adjustment
        max_z = z.max(axis=0)
        pvals = sp_special.erfc(max_z / np.sqrt(2.0))
        adj = sp_stats.false_discovery_control(np.clip(pvals, 0.0, 1.0), method="bh")
        return int(np.sum(adj < 0.05))

    t_ss = _median_timed(run_streamsieve, warmup=2, repeats=7)
    t_np = _median_timed(run_numpy_scipy_rolling_z_bh, warmup=2, repeats=7)

    r_ss = ch_ticks / (t_ss["median_ms"] / 1000.0)
    r_np = ch_ticks / (t_np["median_ms"] / 1000.0)

    return {
        "n_channels": n_ch,
        "n_ticks": n_ticks,
        "channel_ticks": ch_ticks,
        "streamsieve_update_batch_ebh": {
            "median_ms": round(t_ss["median_ms"], 3),
            "ch_ticks_per_sec": round(r_ss, 1),
            "million_ch_ticks_per_sec": round(r_ss / 1e6, 2),
        },
        "numpy_scipy_causal_rolling_z_bh": {
            "median_ms": round(t_np["median_ms"], 3),
            "ch_ticks_per_sec": round(r_np, 1),
            "million_ch_ticks_per_sec": round(r_np / 1e6, 2),
        },
        "speedup_streamsieve_vs_numpy_scipy": round(r_ss / r_np, 2),
    }


def bench_lattice() -> dict[str, Any]:
    """Benchmark EvidenceLattice observe ops/s, 100-item CRDT merge ops/s, and SSLT wire size."""
    n_obs = 5000
    rng = np.random.default_rng(404)
    llrs = rng.normal(0.5, 1.0, size=n_obs).tolist()

    # 1. Single observes/s (integer IDs — pure C-API/ctypes speed)
    def run_obs_int() -> None:
        with EvidenceLattice("strict_origin") as lat:
            for i in range(n_obs):
                lat.observe(i & 7, i, llrs[i], agent_id=i & 3, reliability=0.9, timestamp_ns=1000 + i)

    # 2. Single observes/s (string IDs — includes provenance hashing)
    str_hyps = [f"hyp_{i & 7}" for i in range(n_obs)]
    str_provs = [f"evt:sha256:{i:06d}" for i in range(n_obs)]

    def run_obs_str() -> None:
        with EvidenceLattice("strict_origin") as lat:
            for i in range(n_obs):
                lat.observe(
                    str_hyps[i], str_provs[i], llrs[i], agent_id=i & 3, reliability=0.9, timestamp_ns=1000 + i
                )

    t_obs_int = _median_timed(run_obs_int, warmup=1, repeats=7)
    t_obs_str = _median_timed(run_obs_str, warmup=1, repeats=7)

    # 3. 100-item CRDT merges/s (50% overlapping provenance IDs)
    lat_a = EvidenceLattice("strict_origin")
    lat_b = EvidenceLattice("strict_origin")
    for i in range(100):
        lat_a.observe(i & 7, i, llrs[i], agent_id=0, reliability=0.85, timestamp_ns=1000 + i)
    for i in range(50, 150):
        lat_b.observe(i & 7, i, llrs[i], agent_id=1, reliability=0.90, timestamp_ns=2000 + i)

    wire_bytes_100 = lat_a.to_bytes()
    wire_b85_100 = lat_a.to_base85()

    n_merges = 500

    def run_merges() -> None:
        for _ in range(n_merges):
            # Deserialize fresh copy of A? No — measure direct in-place CRDT merge into fresh-size dst
            dst = EvidenceLattice.from_bytes(wire_bytes_100)
            dst.merge(lat_b)
            dst.close()

    # Also measure pure in-place merge() without deserialization overhead
    dst_reusable = EvidenceLattice.from_bytes(wire_bytes_100)
    dst_reusable.merge(lat_b)  # warm structure; subsequent idempotent merges still scan all 100 items

    def run_merges_inplace() -> None:
        for _ in range(n_merges):
            dst_reusable.merge(lat_b)

    t_merge_roundtrip = _median_timed(run_merges, warmup=1, repeats=7)
    t_merge_inplace = _median_timed(run_merges_inplace, warmup=1, repeats=7)

    lat_a.close()
    lat_b.close()
    dst_reusable.close()

    return {
        "observe_int_ids_ops_per_sec": round(n_obs / (t_obs_int["median_ms"] / 1000.0), 1),
        "observe_int_ids_us_per_op": round((t_obs_int["median_ms"] * 1000.0) / n_obs, 3),
        "observe_str_ids_ops_per_sec": round(n_obs / (t_obs_str["median_ms"] / 1000.0), 1),
        "observe_str_ids_us_per_op": round((t_obs_str["median_ms"] * 1000.0) / n_obs, 3),
        "merge_100_items_inplace_ops_per_sec": round(
            n_merges / (t_merge_inplace["median_ms"] / 1000.0), 1
        ),
        "merge_100_items_inplace_us_per_merge": round(
            (t_merge_inplace["median_ms"] * 1000.0) / n_merges, 3
        ),
        "merge_100_items_deser_plus_merge_ops_per_sec": round(
            n_merges / (t_merge_roundtrip["median_ms"] / 1000.0), 1
        ),
        "wire_100_items_sslt_bytes": len(wire_bytes_100),
        "wire_100_items_base85_chars": len(wire_b85_100),
        "bytes_per_evidence_item": round(len(wire_bytes_100) / 100.0, 2),
    }


def bench_trust() -> dict[str, Any]:
    """Benchmark TrustCalibrator predict+resolve ops/s (both binary prob and continuous scalar)."""
    n_steps = 2000
    rng = np.random.default_rng(505)
    probs = np.clip(rng.uniform(0.1, 0.9, size=(n_steps, 4)), 0.01, 0.99).astype(np.float64)
    ys_bin = (rng.random(n_steps) < 0.5).astype(np.float64).tolist()
    means = rng.normal(15.0, 3.0, size=(n_steps, 4)).astype(np.float64)
    stds = np.full((n_steps, 4), 2.0, dtype=np.float64)
    ys_real = rng.normal(15.0, 3.0, size=n_steps).tolist()

    def run_prob() -> None:
        with TrustCalibrator(4) as tc:
            for t in range(n_steps):
                tc.predict_prob(t, probs[t], underage_cost=4.0, overage_cost=1.0)
                tc.resolve(t, ys_bin[t])

    def run_scalar() -> None:
        with TrustCalibrator(4) as tc:
            for t in range(n_steps):
                tc.predict_scalar(t, means[t], stds[t], underage_cost=4.0, overage_cost=1.0)
                tc.resolve(t, ys_real[t])

    t_prob = _median_timed(run_prob, warmup=1, repeats=7)
    t_scalar = _median_timed(run_scalar, warmup=1, repeats=7)

    return {
        "n_agents": 4,
        "steps_per_run": n_steps,
        "predict_prob_plus_resolve_ops_per_sec": round(
            n_steps / (t_prob["median_ms"] / 1000.0), 1
        ),
        "predict_prob_plus_resolve_us_per_op": round((t_prob["median_ms"] * 1000.0) / n_steps, 2),
        "predict_scalar_plus_resolve_ops_per_sec": round(
            n_steps / (t_scalar["median_ms"] / 1000.0), 1
        ),
        "predict_scalar_plus_resolve_us_per_op": round(
            (t_scalar["median_ms"] * 1000.0) / n_steps, 2
        ),
    }


def bench_cold_start() -> dict[str, Any]:
    """Benchmark cold-start latency (median of 10 runs): native CLI vs Python imports."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "python")

    def run_cli() -> None:
        subprocess.run([str(CLI_BIN), "version"], check=True, capture_output=True)

    def run_py_streamsieve() -> None:
        subprocess.run(
            [sys.executable, "-c", "import streamsieve"],
            check=True,
            capture_output=True,
            env=env,
        )

    def run_py_incumbents() -> None:
        subprocess.run(
            [sys.executable, "-c", "import numpy, scipy.stats, river.drift"],
            check=True,
            capture_output=True,
            env=env,
        )

    t_cli = _median_timed(run_cli, warmup=2, repeats=10)
    t_py = _median_timed(run_py_streamsieve, warmup=2, repeats=10)
    t_inc = _median_timed(run_py_incumbents, warmup=1, repeats=5)

    return {
        "cli_version_median_ms": round(t_cli["median_ms"], 3),
        "cli_version_p95_ms": round(t_cli["p95_ms"], 3),
        "python_import_streamsieve_median_ms": round(t_py["median_ms"], 3),
        "python_import_streamsieve_p95_ms": round(t_py["p95_ms"], 3),
        "python_import_numpy_scipy_river_median_ms": round(t_inc["median_ms"], 3),
        "speedup_cli_vs_python_import": round(t_py["median_ms"] / t_cli["median_ms"], 2),
        "speedup_streamsieve_import_vs_incumbents": round(
            t_inc["median_ms"] / t_py["median_ms"], 2
        ),
        "repeats": 10,
    }


def run_benchmark() -> dict[str, Any]:
    """Run all throughput and latency benchmarks and return structured dict."""
    return {
        "native_cli_avx2": bench_native_cli(),
        "python_update_batch": bench_python_batch(),
        "river_matched": bench_river_matched(),
        "numpy_scipy_matched": bench_numpy_scipy_matched(),
        "evidence_lattice": bench_lattice(),
        "trust_calibrator": bench_trust(),
        "cold_start": bench_cold_start(),
    }


if __name__ == "__main__":
    import json

    out = run_benchmark()
    print(json.dumps(out, indent=2))
