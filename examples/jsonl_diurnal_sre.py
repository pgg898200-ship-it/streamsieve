#!/usr/bin/env python3
"""Zero-dependency SRE observability cookbook: diurnal JSONL telemetry -> e-BH brief -> raw drill-down.

Demonstrates:
  1. Generating realistic diurnal (24h periodic) + trending microservice metrics in JSONL format.
  2. Sifting with ``season_period=24`` so normal 24-hour traffic waves produce 0 false alarms
     while real latency shifts and tail-burst spikes are caught with anytime-valid e-BH FDR control.
  3. Lossless raw-tick drill-down on the exact original (un-filtered) telemetry via ``fetch_raw(handle)``.

Run:
    python3 examples/jsonl_diurnal_sre.py
"""

from __future__ import annotations

import json
import math
import random

from streamsieve import EStream
from streamsieve.mcp_server import _parse_jsonl_rows


def main() -> None:
    rng = random.Random(42)
    n_ticks = 480  # 20 days of hourly metrics
    period = 24
    cp = 240  # Incident starts on day 10

    lines: list[str] = []
    for t in range(n_ticks):
        diurnal = 50.0 + 18.0 * math.sin(2.0 * math.pi * t / period) + 5.0 * math.cos(4.0 * math.pi * t / period)
        record = {
            "ts": f"day-{t // 24:02d}T{t % 24:02d}:00Z",
            "host": "edge-cluster-a",
            "api_p99_ms": round(diurnal + rng.gauss(0.0, 2.0) + (5.5 if t >= cp else 0.0), 3),
            "db_lock_ms": round(
                diurnal + rng.gauss(0.0, 2.0) + (9.0 if (t >= cp and rng.random() < 0.20) else 0.0),
                3,
            ),
            "cpu_util": round(diurnal + rng.gauss(0.0, 2.0), 3),
            "mem_rss_mb": round(diurnal + rng.gauss(0.0, 2.0), 3),
            "net_rx_mbps": round(diurnal + rng.gauss(0.0, 2.0), 3),
            "disk_iops": round(diurnal + rng.gauss(0.0, 2.0), 3),
        }
        lines.append(json.dumps(record))

    jsonl_payload = "\n".join(lines)
    rows, names = _parse_jsonl_rows(jsonl_payload)
    assert names is not None

    with EStream(len(names), warmup=32, season_period=period, ring_capacity=64, alpha=0.05, names=names) as est:
        est.update_batch(rows)
        brief = est.brief(max_channels=6)
        compact = brief.to_compact_text()

        raw_bytes = len(jsonl_payload.encode("utf-8"))
        brief_bytes = len(compact.encode("utf-8"))
        print(f"Ingested {len(rows)} JSONL lines ({raw_bytes:,} B) -> {brief_bytes:,} B brief ({raw_bytes / brief_bytes:.1f}x smaller)\n")
        print(compact)

        for st in brief.statuses:
            if st.rejected_ebh:
                raw_tail = est.fetch_raw(st.handle, limit=5)
                print(f"\nLossless drill-down for {st.name} ({st.handle}), last 5 raw ticks:")
                print(" ", [(tk, round(val, 3)) for tk, val in raw_tail])


if __name__ == "__main__":
    main()
