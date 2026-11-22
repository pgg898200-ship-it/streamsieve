"""StreamSwarmBench Master Runner — Executes all 4 benchmark suites, saves results.json, prints Markdown."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from typing import Any

# Ensure benchmarks/ sibling modules are importable regardless of CWD
BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import numpy as np
import river
import scipy

import streamsieve
import bench_multiagent_probes
import bench_peeking_fdr
import bench_throughput
import bench_token_compression

RESULTS_PATH = BENCH_DIR / "results.json"


def _detect_cpu_model() -> str:
    try:
        out = subprocess.check_output(["lscpu"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if line.lower().startswith("model name:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def collect_metadata() -> dict[str, Any]:
    return {
        "benchmark_suite": "StreamSwarmBench",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": _detect_cpu_model(),
        "cpu_count_logical": os.cpu_count(),
        "python_version": platform.python_version(),
        "packages": {
            "streamsieve": streamsieve.version(),
            "streamsieve_avx2": streamsieve.has_avx2(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "river": river.__version__,
        },
    }


def _print_markdown_summary(data: dict[str, Any]) -> None:
    meta = data["metadata"]
    pkgs = meta["packages"]
    print("# StreamSwarmBench Results\n")
    print(
        f"- **Engine**: `streamsieve {pkgs['streamsieve']}` (AVX2+FMA: `{pkgs['streamsieve_avx2']}`) "
        f"| **CPU**: {meta['cpu_model']} ({meta['cpu_count_logical']} vCPUs)"
    )
    print(
        f"- **Incumbents**: `river {pkgs['river']}`, `scipy {pkgs['scipy']}`, `numpy {pkgs['numpy']}` "
        f"| **Python**: {meta['python_version']} | **Total Runtime**: {data['timings_sec']['total']:.2f}s\n"
    )

    # 1. Throughput & Latency
    tp = data["throughput"]
    print("## 1. Throughput, Incumbent Speedups & Cold-Start\n")
    print("### Native C++20 AVX2 CLI & Python Zero-Copy `update_batch`")
    print("| Mode | Shape (ch x ticks) | Channel-Ticks | Median Latency (ms) | Throughput (M ch-ticks/s) |")
    print("|---|---:|---:|---:|---:|")
    for row in tp["native_cli_avx2"]:
        print(
            f"| Native CLI (`streamsieve bench`) | {row['n_channels']} x {row['n_ticks']} "
            f"| {row['channel_ticks']:,} | {row['internal_median_ms']:.3f} "
            f"| {row['internal_million_ch_ticks_per_sec']:.2f} |"
        )
    for row in tp["python_update_batch"]:
        print(
            f"| Python `EStream.update_batch` | {row['n_channels']} x {row['n_ticks']} "
            f"| {row['channel_ticks']:,} | {row['update_batch_median_ms']:.3f} "
            f"| {row['update_batch_million_ch_ticks_per_sec']:.2f} |"
        )

    rv = tp["river_matched"]
    ns = tp["numpy_scipy_matched"]
    print("\n### Matched Incumbent Speedups")
    print("| Comparison | Matrix | Median (ms) | Throughput (M ch-ticks/s) | Speedup vs Incumbent |")
    print("|---|---|---:|---:|---:|")
    print(
        f"| `streamsieve.EStream.update_batch` | {rv['n_channels']}x{rv['n_ticks']} "
        f"| {rv['streamsieve_update_batch']['median_ms']:.3f} "
        f"| {rv['streamsieve_update_batch']['million_ch_ticks_per_sec']:.2f} | 1.00x (ref) |"
    )
    print(
        f"| `streamsieve.EStream.update_row` | {rv['n_channels']}x{rv['n_ticks']} "
        f"| {rv['streamsieve_update_row']['median_ms']:.3f} "
        f"| {rv['streamsieve_update_row']['million_ch_ticks_per_sec']:.2f} | — |"
    )
    print(
        f"| `river.drift.ADWIN(delta=0.002)` | {rv['n_channels']}x{rv['n_ticks']} "
        f"| {rv['river_adwin']['median_ms']:.3f} "
        f"| {rv['river_adwin']['million_ch_ticks_per_sec']:.3f} "
        f"| **{rv['river_adwin']['speedup_streamsieve_batch_vs_adwin']:.1f}x** (batch) |"
    )
    print(
        f"| `river.drift.PageHinkley()` | {rv['n_channels']}x{rv['n_ticks']} "
        f"| {rv['river_page_hinkley']['median_ms']:.3f} "
        f"| {rv['river_page_hinkley']['million_ch_ticks_per_sec']:.3f} "
        f"| **{rv['river_page_hinkley']['speedup_streamsieve_batch_vs_ph']:.1f}x** (batch) |"
    )
    print(
        f"| `streamsieve` (update_batch + e-BH) | {ns['n_channels']}x{ns['n_ticks']} "
        f"| {ns['streamsieve_update_batch_ebh']['median_ms']:.3f} "
        f"| {ns['streamsieve_update_batch_ebh']['million_ch_ticks_per_sec']:.2f} "
        f"| **{ns['speedup_streamsieve_vs_numpy_scipy']:.2f}x** vs NumPy/SciPy |"
    )
    print(
        f"| NumPy/SciPy causal rolling z + BH | {ns['n_channels']}x{ns['n_ticks']} "
        f"| {ns['numpy_scipy_causal_rolling_z_bh']['median_ms']:.3f} "
        f"| {ns['numpy_scipy_causal_rolling_z_bh']['million_ch_ticks_per_sec']:.2f} | 1.00x (ref) |"
    )

    lat = tp["evidence_lattice"]
    tr = tp["trust_calibrator"]
    cs = tp["cold_start"]
    print("\n### Primitives & Cold-Start Latency")
    print(
        f"- **EvidenceLattice**: {lat['observe_int_ids_ops_per_sec']:,.0f} obs/s (int IDs), "
        f"{lat['observe_str_ids_ops_per_sec']:,.0f} obs/s (str IDs), "
        f"{lat['merge_100_items_inplace_ops_per_sec']:,.0f} 100-item CRDT merges/s, "
        f"wire={lat['wire_100_items_sslt_bytes']} B SSLT ({lat['wire_100_items_base85_chars']} B85 chars)"
    )
    print(
        f"- **TrustCalibrator (4 agents)**: {tr['predict_prob_plus_resolve_ops_per_sec']:,.0f} prob predict+resolve/s, "
        f"{tr['predict_scalar_plus_resolve_ops_per_sec']:,.0f} scalar predict+resolve/s"
    )
    print(
        f"- **Cold-Start (median of 10)**: CLI `./build/streamsieve version` = **{cs['cli_version_median_ms']:.2f} ms** "
        f"| `import streamsieve` = **{cs['python_import_streamsieve_median_ms']:.2f} ms** "
        f"| `import numpy, scipy.stats, river.drift` = {cs['python_import_numpy_scipy_river_median_ms']:.2f} ms\n"
    )

    # 2. Peeking & FDR
    pf = data["peeking_fdr"]
    exp_a = pf["experiment_a_peeking_null_far"]
    print("## 2. Continuous Peeking Null Type-I Error & 256-Ch Mixed Firehose FDR\n")
    print("### Experiment A — Null FAR (500 trials x T=600, peeking every 10 ticks, nominal alpha=0.05)")
    print("| Distribution | `streamsieve` | Naive `|z|>3` | Peeking `ttest` | `ADWIN(0.05)` | `ADWIN(0.002)` | `PageHinkley` |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for dist, row in exp_a["far_by_distribution"].items():
        print(
            f"| {dist} | **{row['streamsieve_estream']:.3f}** | {row['naive_rolling_z3']:.3f} "
            f"| {row['peeking_ttest_ind']:.3f} | {row['river_adwin_0.05']:.3f} "
            f"| {row['river_adwin_0.002']:.3f} | {row['river_page_hinkley']:.3f} |"
        )

    exp_b = pf["experiment_b_mixed_firehose"]
    print("\n### Experiment B — 256-Ch Mixed Firehose (4 shifts + 4 rare 18% +3.5σ bursts + 32 loud 5x + 216 nulls, 5 seeds)")
    print("| Method | Recall (Shift) | Recall (Burst) | FP Loud (/32) | Emp. FDR | Precision | F1 | Wall (ms) |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for m, r in exp_b["methods"].items():
        print(
            f"| `{m}` | {r['recall_shift']:.2f} | {r['recall_burst']:.2f} | {r['fp_loud']:.1f} "
            f"| {r['empirical_fdr']:.3f} | {r['precision']:.3f} | {r['f1']:.3f} | {r['wall_ms']:.1f} |"
        )

    exp_b2 = pf["experiment_b2_diagnostic_symmetric_bursts"]
    print("\n### Experiment B2 (Diagnostic) — Zero-Mean Symmetric ±3.5σ Bursts (5 seeds)")
    print("| Method | Recall (Shift) | Recall (Burst) | FP Loud (/32) | Emp. FDR | F1 |")
    print("|---|---:|---:|---:|---:|---:|")
    for m, r in exp_b2["methods"].items():
        print(
            f"| `{m}` | {r['recall_shift']:.2f} | {r['recall_burst']:.2f} | {r['fp_loud']:.1f} "
            f"| {r['empirical_fdr']:.3f} | {r['f1']:.3f} |"
        )

    # 3. Multi-Agent Probes
    mp = data["multiagent_probes"]
    print("\n## 3. Multi-Agent Streaming Probes (All 8 Mechanisms + Real NOAA Weather)\n")
    print("### `echo` — Echo-Chamber Reprint Storm (200 trials)")
    print("| Swarm | Method | Brier | Brier Skill | Overwrite Churn % | Echo Inflation |")
    print("|---|---|---:|---:|---:|---:|")
    for w_key, w_val in mp["echo"].items():
        for m_name, m_label in [
            ("evidence_lattice", "`EvidenceLattice`"),
            ("naive_lww_blackboard", "Naive LWW Blackboard"),
            ("uncalibrated_log_odds_sum", "Uncalibrated Log-Odds Sum"),
        ]:
            m = w_val[m_name]
            print(
                f"| {w_key} | {m_label} | {m['brier_score']:.4f} | {m['brier_skill_vs_prior']:+.4f} "
                f"| {m['overwrite_churn_pct']:.1f}% | {m['echo_inflation_factor']:.2f}x |"
            )

    print("\n### `sift` + `voi` — 2,048-Channel Bursty Firehose (256 sites x 8 ch, 10 seeds)")
    print("| Swarm | Method | Brier Skill | Recall@Top | Loud FPs | Inter-Agent Dup % |")
    print("|---|---|---:|---:|---:|---:|")
    for w_key, w_val in mp["sift_and_voi"].items():
        v = w_val["voi_sifter"]
        rr = w_val["uniform_round_robin_bayes"]
        nr = w_val["naive_top_raw_mean"]
        print(
            f"| {w_key} | `VOISifter` | **{v['brier_skill']:+.3f}** | **{v['recall_at_top_active']:.3f}** "
            f"| {v['false_flags_loud']:.2f} | {v['inter_agent_dup_rate_pct']:.2f}% |"
        )
        print(
            f"| {w_key} | Round-Robin (Bayes) | {rr['brier_skill']:+.3f} | {rr['recall_at_top_active']:.3f} "
            f"| {rr['false_flags_loud']:.2f} | — |"
        )
        print(
            f"| {w_key} | Naive Top-Raw-Mean | {nr['brier_skill']:+.3f} | {nr['recall_at_top_active']:.3f} "
            f"| {nr['false_flags_loud']:.2f} | — |"
        )

    td = mp["trust_and_delay"]
    print("\n### `trust` + `delay` — 4-Agent Swarm (4-Epoch Delay + Trust Flip @ Epoch 60, 20 seeds)")
    print("| Method | Overall Brier | Post-Flip Brier | Byzantine Quarantine % |")
    print("|---|---:|---:|---:|")
    for k, lbl in [
        ("trust_calibrator", "`TrustCalibrator`"),
        ("plain_exponential_weights", "Plain Exponential Weights"),
        ("unweighted_mean_vote", "Unweighted Mean Vote"),
    ]:
        r = td[k]
        print(
            f"| {lbl} | {r['overall_brier']:.4f} | {r['post_flip_brier']:.4f} "
            f"| {r['byzantine_quarantine_rate_pct']:.1f}% |"
        )

    act = mp["act_noaa_seattle"]
    print("\n### `act` — Real NOAA Seattle `temp_max` (4:1 Asymmetric Cost, 1-Day Delay)")
    print(
        f"- **`TrustCalibrator` Optimal 80th-Pct Action**: cost = **{act['trust_calibrator_optimal_action']['mean_asymmetric_cost']:.3f}** "
        f"vs Point-Mean Baseline = {act['point_mean_action_baseline']['mean_asymmetric_cost']:.3f} "
        f"(**{act['asymmetric_cost_reduction_pct']:.2f}% cost reduction**)"
    )
    print(
        f"- **Delayed AgACI 90% CI Coverage**: **{act['trust_calibrator_optimal_action']['empirical_ci_coverage_pct']:.2f}%** "
        f"(target: 90.0%, mean width: {act['trust_calibrator_optimal_action']['mean_ci_width_deg_c']:.2f} °C)"
    )

    bits = mp["bits_wire_budget"]["full_256_item_state"]
    sw_bits = mp["bits_wire_budget"]["swarm_4_agent_all_to_all_sync"]
    print("\n### `bits` — Wire Budget (8 Hypotheses x 32 Unique Findings Across 4 Agents)")
    print(
        f"- **Full 256-finding state**: SSLT binary = **{bits['sslt_binary_bytes']:,} B**, "
        f"Base85 = **{bits['sslt_base85_chars']:,} chars** vs Compact JSON = {bits['compact_json_bytes']:,} B "
        f"vs JSON Debate = {bits['json_debate_bytes']:,} B "
        f"(**{bits['sslt_vs_json_debate_compression_x']:.1f}x** smaller binary / "
        f"**{bits['base85_vs_json_debate_compression_x']:.1f}x** smaller Base85)"
    )
    print(
        f"- **4-Agent All-to-All Sync**: SSLT = **{sw_bits['sslt_binary_bytes']:,} B** vs "
        f"JSON Debate = {sw_bits['json_debate_bytes']:,} B "
        f"(**{sw_bits['sslt_vs_json_debate_compression_x']:.1f}x** wire reduction)"
    )

    st = mp["stop_sequential"]
    print("\n### `stop` — Anytime-Valid Sequential Stopping (200 H0 + 200 H1 trials, T=400, alpha=0.05)")
    print("| Method | H0 False-Stop Rate | H1 Power | Mean Stop Tick (H1) |")
    print("|---|---:|---:|---:|")
    print(
        f"| `streamsieve` (e >= 20) | **{st['streamsieve_anytime_stop']['h0_false_stop_rate']:.3f}** "
        f"| {st['streamsieve_anytime_stop']['h1_power']:.3f} "
        f"| **{st['streamsieve_anytime_stop']['h1_mean_stop_tick']:.1f}** "
        f"({st['streamsieve_anytime_stop']['sample_savings_vs_fixed_pct']:.1f}% saved vs fixed) |"
    )
    print(
        f"| Naive Peeking `ttest` | {st['naive_peeking_ttest']['h0_false_stop_rate']:.3f} "
        f"| {st['naive_peeking_ttest']['h1_power']:.3f} | {st['naive_peeking_ttest']['h1_mean_stop_tick']:.1f} |"
    )
    print(
        f"| Fixed-Horizon `ttest` | {st['fixed_horizon_ttest']['h0_false_stop_rate']:.3f} "
        f"| {st['fixed_horizon_ttest']['h1_power']:.3f} | {st['fixed_horizon_ttest']['h1_mean_stop_tick']:.1f} |"
    )

    # 4. Token Compression & Bit-Exact Recovery
    tc = data["token_compression"]
    print("\n## 4. Token Compression & Bit-Exact Lossless Recovery\n")
    print("| Scale | Raw CSV Bytes | Raw Tokens (char/4) | Brief Tokens (char/4) | Token Reduction | Wall (ms) | e-BH Prec / Rec | Loud FPs | `fetch_raw` Max Err |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in tc["scales"]:
        print(
            f"| `{s['scenario']}` | {s['raw_csv_bytes']:,} | {s['raw_tokens_char4']:,} "
            f"| {s['brief_tokens_char4']:,} | **{s['token_reduction_char4_pct']:.2f}%** ({s['compression_ratio_char4_x']:.0f}x) "
            f"| {s['wall_ms']:.2f} | {s['ebh_precision']:.2f} / {s['ebh_recall']:.2f} "
            f"| {s['ebh_fp_loud']} | `{s['lossless_drilldown_max_abs_err']:.1f}` (exact) |"
        )


def main() -> None:
    t_total_start = time.perf_counter()
    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    res_throughput = bench_throughput.run_benchmark()
    timings["bench_throughput"] = round(time.perf_counter() - t0, 3)

    t0 = time.perf_counter()
    res_peeking = bench_peeking_fdr.run_benchmark()
    timings["bench_peeking_fdr"] = round(time.perf_counter() - t0, 3)

    t0 = time.perf_counter()
    res_probes = bench_multiagent_probes.run_benchmark()
    timings["bench_multiagent_probes"] = round(time.perf_counter() - t0, 3)

    t0 = time.perf_counter()
    res_compression = bench_token_compression.run_benchmark()
    timings["bench_token_compression"] = round(time.perf_counter() - t0, 3)

    timings["total"] = round(time.perf_counter() - t_total_start, 3)

    payload = {
        "metadata": collect_metadata(),
        "timings_sec": timings,
        "throughput": res_throughput,
        "peeking_fdr": res_peeking,
        "multiagent_probes": res_probes,
        "token_compression": res_compression,
    }

    RESULTS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _print_markdown_summary(payload)
    print(f"\nSaved structured results to: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
