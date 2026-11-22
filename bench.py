"""Compare ordered RRF results, input consumed and CPU-side elapsed time.

Use --trace PATH for {"queries": [{"id": "...", "lists": [["a"], ["b"]]}]}.
A page from each non-exhausted source arrives before each certificate check.
No I/O, network, upstream computation or token latency enters the timings.
"""
import argparse
from collections import Counter
from fractions import Fraction
from itertools import islice
from functools import lru_cache
import hashlib
import heapq
import json
import math
import os
import platform
from pathlib import Path
import random
from statistics import median
import sys
from time import perf_counter_ns

from rankfence import Fence


@lru_cache(maxsize=None)
def term(c, rank):
    return Fraction(1, c + rank)


def exhaustive(lists, k, c, _page, mode="ordered"):
    scores = {}
    for source in lists:
        for rank, doc in enumerate(source, 1):
            scores[doc] = scores.get(doc, 0) + term(c, rank)
    ordered = heapq.nsmallest(k, scores, key=lambda d: (-scores[d], d))
    return tuple(ordered), sum(map(len, lists))


@lru_cache(maxsize=16)
def weights(c, depth):
    # Reused across queries of the same depth, a strong exact batch baseline.
    common = math.lcm(*range(c + 1, c + depth + 1))
    return [0] + [common // (c + rank) for rank in range(1, depth + 1)]


def integer_batch(lists, k, c, _page, mode="ordered"):
    weight = weights(c, max(map(len, lists)))
    scores = {}
    for source in lists:
        for value, doc in zip(islice(weight, 1, None), source):
            scores[doc] = scores.get(doc, 0) + value
    ordered = heapq.nsmallest(k, scores, key=lambda d: (-scores[d], d))
    return tuple(ordered), sum(map(len, lists))


def float_batch(lists, k, c, _page, mode="ordered"):
    scores = {}
    for source in lists:
        for rank, doc in enumerate(source, 1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (c + rank)
    ordered = heapq.nsmallest(k, scores, key=lambda d: (-scores[d], d))
    return tuple(ordered), sum(map(len, lists))


def pages(lists, width):
    # Include one empty round so fully empty sources can be marked exhausted.
    for start in range(0, max(1, max(map(len, lists))), width):
        yield [(i, source[start:start + width], start + width >= len(source))
               for i, source in enumerate(lists) if start == 0 or start < len(source)]


def stream(lists, k, c, page, mode="ordered"):
    fence = Fence(len(lists), k, c=c)
    read = 0
    for batch in pages(lists, page):
        for i, items, done in batch:
            fence.push(i, items, done=done)
            read += len(items)
        ordered = fence.ordered() if mode == "ordered" else fence.members()
        if ordered is not None:
            return tuple(ordered), read
    raise AssertionError("uncertified after exhaustion")


def nra(lists, k, c, page, mode="ordered"):
    return nra_scan(lists, k, page, lambda rank: term(c, rank), mode)


def integer_nra(lists, k, c, page, mode="ordered"):
    # Give the challenger the maximum depth in advance, so that its exact
    # integer table can be prepared and reused across queries.
    lookup = weights(c, max(1, max(map(len, lists))))
    return nra_scan(lists, k, page, lookup.__getitem__, mode)


def nra_scan(lists, k, page, weight_at, mode):
    """Direct no-random-access bounds, recomputed from seen items each round."""
    costs = [weight_at(1)] * len(lists)
    depths = [0] * len(lists)
    scores = {}
    masks = {}
    for batch in pages(lists, page):
        for i, items, done in batch:
            for doc in items:
                depths[i] += 1
                scores[doc] = scores.get(doc, 0) + weight_at(depths[i])
                masks[doc] = masks.get(doc, 0) | (1 << i)
            costs[i] = 0 if done else weight_at(depths[i] + 1)

        top = heapq.nsmallest(k, scores, key=lambda d: (-scores[d], d))
        unseen = sum(costs)
        if not unseen:
            return tuple(top), sum(depths)
        if len(top) < k or scores[top[-1]] <= unseen:
            continue

        possible = {m: sum(cost for i, cost in enumerate(costs) if not (m >> i) & 1)
                    for m in set(masks.values())}
        # An interval for each seen score. Only the suffix's strongest possible
        # challenger matters when testing each adjacent order from the bottom.
        selected = set(top)
        best_challenger = min(((-(scores[d] + possible[masks[d]]), d)
                               for d in scores if d not in selected), default=None)
        if mode == "members":
            if best_challenger is None or (-scores[top[-1]], top[-1]) < best_challenger:
                return tuple(top), sum(depths)
            continue
        for d in reversed(top):
            if best_challenger is not None and (-scores[d], d) >= best_challenger:
                break
            bound = (-(scores[d] + possible[masks[d]]), d)
            best_challenger = min(bound, best_challenger) if best_challenger else bound
        else:
            return tuple(top), sum(depths)
    raise AssertionError("NRA uncertified after exhaustion")


METHODS = {"rankfence": stream, "exact_batch": exhaustive, "float_batch": float_batch,
           "integer_batch": integer_batch, "fraction_nra": nra, "integer_nra": integer_nra}


def synthetic(kind, sources, depth, rng):
    shared = [f"d{i:07d}" for i in range(depth)]
    rng.shuffle(shared)
    if kind == "identical":
        return [shared.copy() for _ in range(sources)]
    if kind == "disjoint":
        return [[f"{j}:{d}" for d in shared] for j in range(sources)]
    if kind == "late_overlap":
        # Rotations separate all local leaders. Increasing the prefixes exposes
        # common IDs late and is adverse to premature certification.
        return [shared[j * depth // sources:] + shared[:j * depth // sources]
                for j in range(sources)]
    if kind == "short_window":
        return [rng.sample(shared, min(12, depth)) for _ in range(sources)]
    # Neighboring ranks swap with Gaussian jitter and ~35% of IDs occur on
    # one source only. Depth and all random state are deterministic.
    return [[d if rng.random() > 0.35 else f"{j}:{d}" for _, d in
             sorted((i + rng.gauss(0, 6), d) for i, d in enumerate(shared))]
            for j in range(sources)]


def load_trace(path, limit):
    with open(path, encoding="utf8") as file:
        rows = json.load(file)["queries"]
    if limit:
        rows = rows[:limit]
    if not rows:
        raise ValueError("trace contains no queries")
    for row in rows:
        lists = row["lists"]
        if not isinstance(lists, list) or not lists:
            raise ValueError("trace requires one or more lists per query")
        for docs in lists:
            if not isinstance(docs, list) or any(not isinstance(d, str) for d in docs):
                raise ValueError("each source must be a list of string IDs")
            if len(set(docs)) != len(docs):
                raise ValueError("duplicate ID within a source")
        yield "trace", str(row["id"]), lists


def summary(rows):
    totals = Counter()
    all_read = sum(r["total_entries"] for r in rows)
    for r in rows:
        totals.update(r["entries_read"])
    skipped = all_read - totals["rankfence"]
    extra_ms = sum(median(r["times_ms"]["rankfence"]) -
                   median(r["times_ms"]["integer_batch"]) for r in rows)
    samples = {name: sorted(median(r["times_ms"][name]) for r in rows) for name in METHODS}
    return {
        "queries": len(rows),
        "queries_certified_before_full_input": sum(
            r["entries_read"]["rankfence"] < r["total_entries"] for r in rows),
        # Linear break-even model only: equal cost per returned item, zero
        # page latency, and integer_batch as the exact reference implementation.
        "modeled_break_even_us_per_skipped_item_vs_integer_batch":
            round(max(0.0, extra_ms * 1000 / skipped), 4) if skipped else None,
        "total_entries": all_read,
        "entries_read": dict(totals),
        "entries_skipped_pct": {name: round(100 * (1 - total / all_read), 2)
                                if all_read else 0.0 for name, total in totals.items()},
        "median_query_ms": {
            name: round(median(values), 4) for name, values in samples.items()},
        "p95_query_ms": {
            name: round(values[math.ceil(len(values) * .95) - 1], 4)
            for name, values in samples.items()},
        "float_order_disagreements": sum(not row["float_matches_exact"] for row in rows),
        "same_stopping_round_as_nra": sum(row["entries_read"]["fraction_nra"] ==
                                          row["entries_read"]["rankfence"] for row in rows),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in [("sources", 4), ("depth", 256), ("queries", 3), ("page", 16),
                          ("k", 5), ("c", 60), ("repeats", 3), ("seed", 1701)]:
        parser.add_argument("--" + name, type=int, default=default)
    all_workloads = ("identical", "partial_overlap", "disjoint", "late_overlap", "short_window")
    parser.add_argument("--workloads", nargs="+", choices=all_workloads, default=all_workloads)
    parser.add_argument("--mode", choices=("ordered", "members"), default="ordered")
    parser.add_argument("--trace", metavar="JSON")
    parser.add_argument("--trace-limit", type=int, help="first N trace queries, in file order")
    parser.add_argument("--json", metavar="OUTPUT", help="save metadata and per-query raw timings")
    args = parser.parse_args()
    if min(args.sources, args.depth, args.queries, args.page, args.k, args.repeats) < 1:
        parser.error("numeric sizes and counts must be positive")
    if args.c < 0 or (args.trace_limit is not None and args.trace_limit < 1):
        parser.error("c must be nonnegative and trace-limit must be positive")
    rng = random.Random(args.seed)
    timing_rng = random.Random(args.seed ^ 0xf355)
    cases = (load_trace(args.trace, args.trace_limit) if args.trace else
             ((kind, str(i), synthetic(kind, args.sources, args.depth, rng))
              for kind in args.workloads
              for i in range(args.queries)))
    groups = {}
    for kind, topic, lists in cases:
        params = (lists, args.k, args.c, args.page, args.mode)
        reference, entries = exhaustive(*params)
        if args.mode == "members":
            reference = tuple(sorted(reference))
        # One warm-up per method per query. Then shuffle the timing order between
        # repetitions. Compare outputs and collect counters outside the timed window.
        reads, times = {}, {m: [] for m in METHODS}
        float_match = True
        for rep in range(args.repeats + 1):
            order = list(METHODS)
            timing_rng.shuffle(order)
            for name in order:
                start = perf_counter_ns()
                ordered, read = METHODS[name](*params)
                elapsed_ms = (perf_counter_ns() - start) / 1e6
                if args.mode == "members":
                    ordered = tuple(sorted(ordered))
                if name == "float_batch":
                    float_match &= ordered == reference
                elif ordered != reference:
                    raise AssertionError(f"{kind}/{topic}: {name} = {ordered}; exact = {reference}")
                if rep:
                    times[name].append(round(elapsed_ms, 6))
                reads[name] = read
        groups.setdefault(kind, []).append({"query": topic, "total_entries": entries,
            "sources": len(lists), "exact_top": list(reference), "entries_read": reads,
            "float_matches_exact": float_match, "times_ms": times})
    try:
        with open("/proc/cpuinfo") as f:
            cpu = next(line.split(":", 1)[1].strip() for line in f if line.startswith("model name"))
    except (OSError, StopIteration):
        cpu = platform.processor()
    source_files = [Path(__file__), Path(__file__).parent / "rankfence" / "core.py"]
    if args.trace:
        source_files.append(Path(args.trace))
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    result = {"schema": 1, "sha256": hashes, "timer": "perf_counter_ns", "command": [os.path.basename(sys.executable), *sys.argv],
              "python": sys.version.split()[0], "platform": platform.platform(), "cpu": cpu,
              "config": vars(args), "groups": {k: summary(v) for k, v in groups.items()},
              "queries": groups}
    print(json.dumps(result["groups"], indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf8") as f:
            json.dump(result, f, indent=2)
            f.write("\n")


if __name__ == "__main__":
    main()
