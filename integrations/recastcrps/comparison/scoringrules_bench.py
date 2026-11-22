"""Mutable fixed-grid CRPS versus the scoringrules 0.12.0 public snapshot API."""
import argparse
import hashlib
import importlib.metadata as md
import json
import math
import os
import platform
import random
import statistics
import sys
from pathlib import Path
from time import perf_counter_ns

import numpy as np
import scoringrules as sr
from recastcrps import Ensemble


# This support is known from the tick format before generating any forecasts.
SUPPORT = range(-2048, 2049)


def inputs(size, steps, seed, weighted):
    rng = random.Random(seed + size)
    values = [rng.choice(SUPPORT) for _ in range(size)]
    weights = [rng.randrange(1, 9) if weighted else 1 for _ in values]
    edits = [(rng.randrange(size), rng.choice(SUPPORT),
              rng.randrange(1, 9) if weighted else 1, rng.randrange(-2400, 2401))
             for _ in range(steps)]
    return values, weights, edits


def run_online(values, weights, edits, weighted, recast):
    start = perf_counter_ns()
    if recast:
        ensemble = Ensemble(SUPPORT, len(values))
        for slot, (x, w) in enumerate(zip(values, weights)):
            ensemble.set(slot, x, w)
    else:
        forecasts = np.asarray(values, dtype=np.float64)
        mass = np.asarray(weights, dtype=np.float64) if weighted else None
    initialized = perf_counter_ns()
    output = []
    if recast:
        for slot, x, w, y in edits:
            ensemble.set(slot, x, w)
            output.append(ensemble.score(y))
    else:
        for slot, x, w, y in edits:
            forecasts[slot] = x
            if weighted:
                mass[slot] = w
            output.append(float(sr.crps_ensemble(y, forecasts, ens_w=mass, estimator='qd')))
    done = perf_counter_ns()
    return initialized - start, done - initialized, np.asarray(output)


def run_batch(values, weights, edits, weighted):
    # Lower-bound offline throughput: callers who precompute or already have
    # the full matrix only pay score_ns. Others also pay preparation_ns.
    start = perf_counter_ns()
    forecasts = np.asarray(values, dtype=np.float64)
    mass = np.asarray(weights, dtype=np.float64) if weighted else None
    matrix = np.empty((len(edits), len(values)), dtype=np.float64)
    masses = np.empty_like(matrix) if weighted else None
    targets = np.empty(len(edits), dtype=np.float64)
    for i, (slot, x, w, y) in enumerate(edits):
        forecasts[slot] = x
        matrix[i] = forecasts
        targets[i] = y
        if weighted:
            mass[slot] = w
            masses[i] = mass
    prepared = perf_counter_ns()
    output = sr.crps_ensemble(targets, matrix, ens_w=masses, estimator='qd')
    done = perf_counter_ns()
    return prepared - start, done - prepared, output


def errors(values, reference):
    delta = values - reference
    nonzero = reference != 0
    rel = delta[nonzero] / np.abs(reference[nonzero])
    absolute = float(np.max(np.abs(delta)))
    if not np.allclose(values, reference, atol=1e-9, rtol=1e-12):
        raise AssertionError(f'CRPS mismatch: max |delta| = {absolute:g}')
    return {'max_abs': absolute, 'log10_max_abs': math.log10(absolute) if absolute else None,
            'min_signed_delta': float(delta.min()), 'max_signed_delta': float(delta.max()),
            'min_signed_rel_nonzero': float(rel.min()) if rel.size else None,
            'max_signed_rel_nonzero': float(rel.max()) if rel.size else None,
            'zero_reference_count': int(np.count_nonzero(reference == 0))}


def cpu_name():
    path = Path('/proc/cpuinfo')
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith('model name'):
                return line.split(':', 1)[1].strip()
    return platform.processor() or platform.machine()


def benchmark(args):
    if md.version('scoringrules') != '0.12.0':
        raise RuntimeError('This protocol targets scoringrules 0.12.0')
    libs = ['numpy', 'scipy', 'scoringrules', 'numba', 'llvmlite', 'array-api-compat']
    modes = [('recast', None, False), ('numpy_online', False, False),
             ('numba_online', True, False), ('numpy_batch', False, True),
             ('numba_batch', True, True)]
    result = {'python': sys.version, 'platform': platform.platform(), 'cpu': cpu_name(),
              'affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
              'packages': {p: md.version(p) for p in libs}, 'options': vars(args).copy(),
              'thread_settings': {k: os.getenv(k) for k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMBA_NUM_THREADS')},
              'support': [SUPPORT.start, SUPPORT.stop - 1], 'rows': []}
    source_root = Path(__file__).resolve().parents[1]
    result['source_sha256'] = {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
                               for name in ('include/recastcrps.hpp', 'python/recastcrps/module.cpp')}
    for weighted in (False, True):
        for size in args.sizes:
            steps = args.small_batch_steps if size == 8 else args.steps
            values, weights, edits = inputs(size, steps, args.seed, weighted)
            data = (values, weights, edits, weighted)
            # Reference uses the quantile decomposition on a stack of snapshots.
            # All timed paths must agree at every edit with this reference.
            sr.set_config(numba=False)
            _, _, reference = run_batch(*data)
            runs = {key: {'setup_or_snapshot_ns': [], 'score_loop_ns': []} for key, _, _ in modes}
            rng = random.Random(args.seed + size)
            order = list(modes)
            for trial in range(args.repeats + 1):
                rng.shuffle(order)
                for name, numba_mode, batched in order:
                    if numba_mode is not None:
                        sr.set_config(numba=numba_mode)
                    prep, elapsed, scores = (run_batch(*data) if batched else
                                             run_online(*data, recast=name == 'recast'))
                    diff = errors(scores, reference)
                    if trial:  # one full untimed warm-up per method and shape
                        record = runs[name]
                        record['setup_or_snapshot_ns'].append(prep)
                        record['score_loop_ns'].append(elapsed)
                        record['delta_vs_numpy_batch_qd'] = diff
            for record in runs.values():
                record['median_ns_per_edit_score'] = statistics.median(record['score_loop_ns']) / len(edits)
            result['rows'].append({'members': size, 'edits': steps, 'integer_weighted': weighted, 'methods': runs})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', nargs='+', type=int, default=[8, 32, 512, 8192])
    parser.add_argument('--steps', type=int, default=128)
    parser.add_argument('--small-batch-steps', type=int, default=2048,
                        help='number of scored revisions in the eight-member offline throughput control')
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--seed', type=int, default=95722)
    args = parser.parse_args()
    if min(args.steps, args.small_batch_steps, args.repeats, *args.sizes) < 1:
        parser.error('sizes, steps and repeats must all be positive')
    print(json.dumps(benchmark(args), indent=2, allow_nan=False))
