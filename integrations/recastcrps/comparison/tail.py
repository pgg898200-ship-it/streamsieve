"""Measure individual Python edit + CRPS calls and an empty timed block."""
from collections import Counter
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import sys
import time

from recastcrps import Ensemble


def summarize(samples):
    ordered = sorted(samples)
    def percentile(p):
        return ordered[math.ceil(p * len(ordered)) - 1]
    return {
        'samples': len(ordered), 'p50_ns': percentile(.50), 'p95_ns': percentile(.95),
        'p99_ns': percentile(.99), 'p999_ns': percentile(.999), 'max_ns': ordered[-1],
        'duration_ns_counts': sorted(Counter(samples).items()),
    }


def main():
    rng = random.Random(95722)
    support = range(-2048, 2049)
    size = 8192
    state = Ensemble(support, size)
    for i in range(size):
        state.set(i, rng.choice(support), rng.randrange(1, 9))
    warmup, samples, block_size = 5000, 20000, 500
    edits = [(rng.randrange(size), rng.choice(support), rng.randrange(1, 9),
              rng.randrange(-2400, 2401)) for _ in range(samples + warmup)]
    for slot, x, w, y in edits[:warmup]:
        state.set(slot, x, w)
        state.score(y)

    timer = time.perf_counter_ns
    measured, empty = [], []
    checksum = 0.0
    for start in range(warmup, warmup + samples, block_size):
        block = edits[start:start + block_size]
        # Alternate order in successive blocks to distribute clock/environment drift.
        for control in ((True, False) if ((start - warmup) // block_size) % 2 else (False, True)):
            if control:
                for slot, x, w, y in block:
                    before = timer()
                    after = timer()
                    empty.append(after - before)
            else:
                for slot, x, w, y in block:
                    before = timer()
                    state.set(slot, x, w)
                    score = state.score(y)
                    after = timer()
                    measured.append(after - before)
                    checksum += score
    root = Path(__file__).resolve().parents[1]
    cpu_info = Path('/proc/cpuinfo')
    cpu = platform.processor() or platform.machine()
    if cpu_info.exists():
        cpu = next((line.split(':', 1)[1].strip() for line in cpu_info.read_text().splitlines()
                    if line.startswith('model name')), cpu)
    return {
        'members': size, 'declared_support': [support.start, support.stop - 1],
        'positive_weight_range': [1, 8], 'seed': 95722, 'warmup_edits': warmup,
        'block_size': block_size, 'gc_enabled': gc.isenabled(), 'cpu': cpu,
        'affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
        'platform': platform.platform(), 'python': sys.version, 'timer': vars(time.get_clock_info('perf_counter')),
        'source_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                          for name in ('include/recastcrps.hpp', 'python/recastcrps/module.cpp')},
        'score_checksum': checksum, 'percentiles': 'nearest rank, raw wall ns, no subtraction',
        'update_and_score': summarize(measured), 'empty_timer': summarize(empty),
    }


if __name__ == '__main__':
    print(json.dumps(main(), separators=(',', ':'), allow_nan=False))
