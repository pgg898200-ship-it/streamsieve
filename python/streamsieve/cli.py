"""Command-line interface for streamsieve (sift, mcp, bench, verify, version)."""

from __future__ import annotations

import argparse
from array import array
import json
import random
import sys
import time
from typing import Sequence

from ._native import has_avx2, version
from .causality import verify_estream_causality
from .lattice import EvidenceLattice
from .mcp_server import _parse_csv_rows, _parse_json_stream, serve_stdio
from .monitor import EStream
from .sifter import VOISifter
from .trust import TrustCalibrator


def _cmd_version(_args: argparse.Namespace) -> int:
    simd = "AVX2+FMA" if has_avx2() else "scalar"
    print(f"streamsieve {version()} ({simd})")
    return 0


def _cmd_mcp(_args: argparse.Namespace) -> int:
    return serve_stdio()


def _cmd_sift(args: argparse.Namespace) -> int:
    if args.file == "-":
        raw_text = sys.stdin.read()
    else:
        with open(args.file, "r", encoding="utf-8") as fh:
            raw_text = fh.read()

    stripped = raw_text.lstrip()
    if not stripped:
        print("streamsieve: empty input stream", file=sys.stderr)
        return 1

    if stripped.startswith("["):
        rows, names = _parse_json_stream(stripped)
    else:
        rows, names = _parse_csv_rows(raw_text)

    if not rows or not rows[0]:
        print("streamsieve: no numeric rows found", file=sys.stderr)
        return 1

    n_ch = len(rows[0])
    use_names = names if names and len(names) == n_ch else None

    t0 = time.perf_counter()
    with EStream(
        n_ch,
        warmup=args.warmup,
        ring_capacity=args.ring_capacity,
        alpha=args.alpha,
        names=use_names,
    ) as est:
        est.update_batch(rows)
        brief = est.brief(max_channels=args.top, names=use_names, alpha=args.alpha)
    elapsed_ms = (time.perf_counter() - t0) * 1e3

    if args.as_json:
        out = dict(brief)
        out["raw_bytes"] = len(raw_text.encode("utf-8"))
        out["elapsed_ms"] = round(elapsed_ms, 3)
        print(json.dumps(out, separators=(",", ":")))
    else:
        print(brief.to_compact_text())
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    n_ch = args.channels
    n_ticks = args.ticks
    total = n_ch * n_ticks

    # Deterministic fast triangular unit-scale noise matching native C++ bench
    data = array("d", [0.0]) * total
    rng = 0x9E3779B97F4A7C15
    mask64 = (1 << 64) - 1
    scale = 2.4494897 / 9007199254740992.0
    for i in range(total):
        rng ^= (rng >> 12) & mask64
        rng ^= (rng << 25) & mask64
        rng ^= (rng >> 27) & mask64
        u1 = ((rng * 2685821657736338717) & mask64) >> 11
        rng ^= (rng >> 12) & mask64
        rng ^= (rng << 25) & mask64
        rng ^= (rng >> 27) & mask64
        u2 = ((rng * 2685821657736338717) & mask64) >> 11
        data[i] = (u1 + u2 - 9007199254740992.0) * scale

    shift_ch = min(8, n_ch)
    shift_start = 500 if n_ticks > 600 else max(32, n_ticks // 3)
    for t in range(min(shift_start, n_ticks), n_ticks):
        base = t * n_ch
        for c in range(shift_ch):
            data[base + c] += 1.5

    with EStream(n_ch, warmup=32, ring_capacity=0, alpha=0.05) as est:
        t0 = time.perf_counter()
        est.update_batch(data)
        rejected = est.ebh_reject(0.05)
        elapsed_s = time.perf_counter() - t0

    mpts_s = (total / max(elapsed_s, 1e-12)) / 1e6
    simd = "AVX2+FMA" if has_avx2() else "scalar"
    if args.as_json:
        print(
            json.dumps(
                {
                    "version": version(),
                    "simd": simd,
                    "channels": n_ch,
                    "ticks": n_ticks,
                    "elapsed_ms": round(elapsed_s * 1e3, 3),
                    "mpts_per_sec": round(mpts_s, 2),
                    "ebh_rejected": len(rejected),
                },
                separators=(",", ":"),
            )
        )
    else:
        print(
            f"streamsieve {version()} ({simd}) | {n_ch} ch x {n_ticks} ticks = "
            f"{total / 1e6:.2f}M channel-ticks in {elapsed_s * 1e3:.3f} ms "
            f"({mpts_s:.2f}M ch-ticks/s) | e-BH rejected={len(rejected)}"
        )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    rng = random.Random(42)
    checks: dict[str, bool] = {}

    # 1. Bit-exact zero-lookahead causality check on EStream
    mat = [[rng.gauss(0.0, 1.0) for _ in range(8)] for _ in range(96)]
    for t in range(48, 96):
        mat[t][0] += 1.8
        if t % 5 == 0:
            mat[t][1] += 3.5
    checks["estream_bit_exact_causality"] = verify_estream_causality(
        mat, probe_ticks=[0, 15, 31, 32, 50, 80], warmup=32
    )

    # 2. EvidenceLattice CRDT commutativity + idempotence + base85 roundtrip
    with EvidenceLattice("strict_origin") as a, EvidenceLattice("strict_origin") as b:
        a.observe("hyp_x", "ev_1", 1.4, agent_id=0, timestamp_ns=10)
        b.observe("hyp_x", "ev_2", -0.3, agent_id=1, timestamp_ns=20)
        b.observe("hyp_x", "ev_1", 1.4, agent_id=2, timestamp_ns=30)
        with a.merged(b) as ab, b.merged(a) as ba, EvidenceLattice.from_base85(ab.to_base85()) as rt:
            sab = ab.query("hyp_x")
            sba = ba.query("hyp_x")
            srt = rt.query("hyp_x")
            checks["lattice_crdt_and_wire"] = (
                sab.total_log_odds == sba.total_log_odds == srt.total_log_odds
                and sab.merged_e_value == sba.merged_e_value == srt.merged_e_value
                and sab.unique_evidence == 2
            )

    # 3. VOISifter non-overlapping multi-agent allocation
    with VOISifter(32, n_sites=4) as sf:
        sf.observe_ticks(2, [0.0, -0.1, 3.8, 0.1, 3.6, 0.0, -0.2, 0.1])
        acts = sf.allocate(budget_per_agent=2, n_agents=4)
        targets = [(a.tool_kind, a.target_id) for a in acts]
        checks["voi_non_overlapping_allocation"] = (
            len(acts) == 8
            and len(targets) == len(set(targets))
            and all(a.target_id != 2 for a in acts if a.tool_kind == "channel_mean")
        )

    # 4. TrustCalibrator Ville quarantine of poisoned agent
    with TrustCalibrator(3) as tc:
        for t in range(50):
            y = 1.0 if (t % 2 == 0) else 0.0
            good = 0.85 if y > 0.5 else 0.15
            rand_p = 0.99 if ((t * 7) % 3 == 0) else 0.01
            pois = 0.01 if y > 0.5 else 0.99
            tc.predict_prob(t, [good, rand_p, pois])
            tc.resolve(t, y)
        ags = tc.agents()
        checks["trust_ville_quarantine"] = (
            ags[0].weight > 0.75 and not ags[0].quarantined and ags[2].quarantined and ags[2].weight == 0.0
        )

    all_ok = all(checks.values())
    if args.as_json:
        print(json.dumps({"ok": all_ok, "checks": checks}, separators=(",", ":")))
    else:
        for name, ok in checks.items():
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print(f"streamsieve verify: {'ALL CHECKS PASSED' if all_ok else 'FAILED'}")
    return 0 if all_ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="streamsieve",
        description="SIMD C++20 multi-agent streaming inference, e-process sifting & CRDT fusion.",
    )
    sub = parser.add_subparsers(dest="command")

    p_sift = sub.add_parser("sift", help="Sift CSV/TSV/JSON numeric stream into compact e-BH brief")
    p_sift.add_argument("file", nargs="?", default="-", help="Input file path or '-' for stdin")
    p_sift.add_argument("--alpha", type=float, default=0.05, help="e-BH FDR level (default: 0.05)")
    p_sift.add_argument("--top", type=int, default=10, help="Max channels in brief (default: 10)")
    p_sift.add_argument("--warmup", type=int, default=32, help="Causal warmup ticks (default: 32)")
    p_sift.add_argument("--ring-capacity", type=int, default=256, help="Raw ring buffer capacity")
    p_sift.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON brief")
    p_sift.set_defaults(func=_cmd_sift)

    p_mcp = sub.add_parser("mcp", help="Run JSON-RPC 2.0 MCP server over stdio")
    p_mcp.set_defaults(func=_cmd_mcp)

    p_bench = sub.add_parser("bench", help="Run AVX2 streaming throughput benchmark")
    p_bench.add_argument("--channels", type=int, default=1024, help="Number of channels (default: 1024)")
    p_bench.add_argument("--ticks", type=int, default=2000, help="Number of ticks (default: 2000)")
    p_bench.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON stats")
    p_bench.set_defaults(func=_cmd_bench)

    p_verify = sub.add_parser("verify", help="Run causality, CRDT, VOI, and quarantine self-checks")
    p_verify.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON report")
    p_verify.set_defaults(func=_cmd_verify)

    p_ver = sub.add_parser("version", help="Print native runtime version & SIMD capability")
    p_ver.set_defaults(func=_cmd_version)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
