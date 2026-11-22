#!/usr/bin/env python3
"""Find a latency shift in 20 days of simulated hourly service metrics.

The fixture adds a sustained latency shift and intermittent database lock spikes
halfway through the trace. The monitor accounts for the 24-hour cycle, flags
the latency channel, and returns its last five unfiltered samples.

Run from the repository root with the current source installed:
    python3 examples/jsonl_diurnal_sre.py
"""

from __future__ import annotations

import json
import math
import random

from streamsieve import EStream
from streamsieve.formats import parse_jsonl_rows


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
    rows, names = parse_jsonl_rows(jsonl_payload)
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
