"""StreamSwarmBench — Context-Window Token Compression & Lossless Bit-Exact Drill-Down Benchmark.

Tests realistic multivariate telemetry firehoses at three scales:
  - (64 ch x 500 ticks)
  - (256 ch x 1000 ticks)
  - (1024 ch x 2000 ticks)

Each firehose plants sustained shifts, rare high-SNR bursts, and 5x loud null channels.
Measures:
  - Raw CSV bytes and approximate BPE tokens (chars/4 and GPT/cl100k-style regex token count)
  - streamsieve compact text brief bytes & tokens (plus JSON brief for reference)
  - Exact % token reduction and compression multiplier
  - End-to-end wall-clock ms (batch ingest + e-BH + brief formatting)
  - Exact e-BH precision, recall (overall, shift, burst), and loud-channel false positives
  - Strict assertion of 100% bit-exact lossless raw tick recovery (`max_abs_err == 0.0`)
    via `EStream.fetch_raw(handle)` on every flagged channel handle.
"""

from __future__ import annotations

import io
import json
import re
import time
from typing import Any

import numpy as np

from streamsieve import EStream, parse_channel_handle

# Lightweight GPT/cl100k-style word/number/punctuation regex tokenizer approximation
_TOKEN_RE = re.compile(r"[A-Za-z_]+|\d+(?:\.\d+)?|[^\sA-Za-z0-9_]")


def _count_tokens_char4(text: str) -> int:
    return max(1, int(round(len(text) / 4.0)))


def _count_tokens_regex(text: str) -> int:
    return len(_TOKEN_RE.findall(text))


def _serialize_csv_fast(mat: np.ndarray, names: list[str]) -> str:
    """Serialize float64 telemetry matrix to standard realistic 4-decimal CSV text."""
    buf = io.StringIO()
    buf.write(",".join(names) + "\n")
    np.savetxt(buf, mat, fmt="%.4f", delimiter=",")
    return buf.getvalue()


def _run_scale(n_ch: int, n_ticks: int, seed: int) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    cp = int(n_ticks * 0.35)

    n_shift = 4
    n_burst = 4
    n_loud = max(8, n_ch // 8)

    names = [f"svc_{i:04d}" for i in range(n_ch)]
    scales = np.ones(n_ch, dtype=np.float64)
    loud_start = n_shift + n_burst
    loud_end = loud_start + n_loud
    scales[loud_start:loud_end] = 5.0  # 5x loud null channels

    mat = rng.standard_normal((n_ticks, n_ch)) * scales
    # Plant sustained mean shifts (+1.6 sigma) on channels 0..3
    mat[cp:, 0:n_shift] += 1.6
    # Plant rare 20% +3.8 sigma bursts on channels 4..7
    bmask = rng.random((n_ticks - cp, n_burst)) < 0.20
    mat[cp:, n_shift : n_shift + n_burst] += bmask * 3.8
    mat = np.ascontiguousarray(mat, dtype=np.float64)

    shift_set = set(range(0, n_shift))
    burst_set = set(range(n_shift, n_shift + n_burst))
    true_set = shift_set | burst_set
    loud_set = set(range(loud_start, loud_end))

    # Measure raw CSV size & token estimates
    csv_text = _serialize_csv_fast(mat, names)
    raw_bytes = len(csv_text.encode("utf-8"))
    raw_tok_char4 = _count_tokens_char4(csv_text)
    raw_tok_regex = _count_tokens_regex(csv_text)

    # Timed end-to-end streamsieve sifting + brief generation (ring_capacity=n_ticks for full lossless recovery)
    t0 = time.perf_counter()
    with EStream(n_ch, warmup=32, ring_capacity=n_ticks, alpha=0.05, names=names) as est:
        est.update_batch(mat)
        brief = est.brief(max_channels=10, names=names, alpha=0.05)
        brief_text = brief.to_compact_text()
        wall_ms = (time.perf_counter() - t0) * 1000.0

        brief_json = json.dumps(brief, separators=(",", ":"))
        rejected = set(brief.rejected_channels)

        # Verify 100% bit-exact lossless raw tick recovery via fetch_raw(handle)
        max_abs_err = 0.0
        ticks_verified = 0
        handles_verified = 0
        handles_to_check = [s.handle for s in brief.statuses if s.rejected_ebh]
        if not handles_to_check:
            handles_to_check = list(brief.handles[:3])

        for handle in handles_to_check:
            ch_id, h_start, h_end = parse_channel_handle(handle)
            raw_pairs = est.fetch_raw(handle)
            assert len(raw_pairs) > 0, f"Empty fetch_raw for handle {handle}"
            if h_start is not None:
                assert raw_pairs[0][0] >= h_start
            if h_end is not None:
                assert raw_pairs[-1][0] <= h_end
            for tick_1idx, val in raw_pairs:
                expected = float(mat[tick_1idx - 1, ch_id])
                err = abs(val - expected)
                if err > max_abs_err:
                    max_abs_err = err
                ticks_verified += 1
            handles_verified += 1

        assert max_abs_err == 0.0, f"Bit-exact drill-down failed: max_abs_err={max_abs_err}"

    brief_bytes = len(brief_text.encode("utf-8"))
    brief_tok_char4 = _count_tokens_char4(brief_text)
    brief_tok_regex = _count_tokens_regex(brief_text)

    json_bytes = len(brief_json.encode("utf-8"))
    json_tok_char4 = _count_tokens_char4(brief_json)

    tp = len(rejected & true_set)
    fp = len(rejected - true_set)
    precision = (tp / float(len(rejected))) if rejected else 1.0
    recall = tp / float(len(true_set))
    recall_shift = len(rejected & shift_set) / float(len(shift_set))
    recall_burst = len(rejected & burst_set) / float(len(burst_set))
    fp_loud = len(rejected & loud_set)

    return {
        "scenario": f"{n_ch}ch_x_{n_ticks}ticks",
        "n_channels": n_ch,
        "n_ticks": n_ticks,
        "channel_ticks": n_ch * n_ticks,
        "raw_csv_bytes": raw_bytes,
        "raw_tokens_char4": raw_tok_char4,
        "raw_tokens_regex": raw_tok_regex,
        "brief_text_bytes": brief_bytes,
        "brief_tokens_char4": brief_tok_char4,
        "brief_tokens_regex": brief_tok_regex,
        "brief_json_bytes": json_bytes,
        "brief_json_tokens_char4": json_tok_char4,
        "token_reduction_char4_pct": round((1.0 - brief_tok_char4 / float(raw_tok_char4)) * 100.0, 4),
        "token_reduction_regex_pct": round((1.0 - brief_tok_regex / float(raw_tok_regex)) * 100.0, 4),
        "compression_ratio_char4_x": round(raw_tok_char4 / float(brief_tok_char4), 1),
        "compression_ratio_regex_x": round(raw_tok_regex / float(brief_tok_regex), 1),
        "wall_ms": round(wall_ms, 3),
        "ebh_n_rejected": len(rejected),
        "ebh_precision": round(precision, 4),
        "ebh_recall": round(recall, 4),
        "ebh_recall_shift": round(recall_shift, 4),
        "ebh_recall_burst": round(recall_burst, 4),
        "ebh_fp_loud": fp_loud,
        "ebh_total_fp": fp,
        "lossless_drilldown_handles_verified": handles_verified,
        "lossless_drilldown_ticks_verified": ticks_verified,
        "lossless_drilldown_max_abs_err": max_abs_err,
        "lossless_bit_exact": max_abs_err == 0.0,
    }


def run_benchmark() -> dict[str, Any]:
    """Run token compression & bit-exact lossless recovery across 3 firehose scales."""
    scales = [
        (64, 500, 9001),
        (256, 1000, 9002),
        (1024, 2000, 9003),
    ]
    return {
        "scales": [_run_scale(n_ch, n_ticks, seed) for n_ch, n_ticks, seed in scales],
    }


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), indent=2))
