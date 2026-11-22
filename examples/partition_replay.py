"""Compare exact top-k strategies for shard-local RRF with disjoint namespaces.

Every source ranks items from exactly one shard. Each listing in the trace is
split by a stable document hash and re-ranked within the shard. This simulates
sharding of real rankings and changes their RRF semantics. The certificate and
all baselines below operate on identical shard-local ranks.
"""
import argparse
from collections import defaultdict
import hashlib
import heapq
import itertools
import json
import math
from pathlib import Path
import platform
import random
from statistics import median
import sys
from time import perf_counter_ns
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bench import integer_batch, load_trace, weights
from rankfence import Fence
from rankfence.partition import PartitionFence


def shard_trace(lists, nshards):
    groups = [[[] for _ in lists] for _ in range(nshards)]
    for r, source in enumerate(lists):
        for d in source:
            g = zlib.crc32(d.encode()) % nshards
            groups[g][r].append(f"{g}/{d}")
    return groups


def synthetic(profile, nshards, nrankers, depth, rng):
    hot = rng.randrange(nshards)
    groups = []
    for g in range(nshards):
        base = [f"{g}/d{i:06d}" for i in range(depth)]
        rng.shuffle(base)
        group = []
        for _ in range(nrankers):
            if profile == "agreement":
                group.append(base.copy())
            elif profile == "hot_shard" and g == hot:
                group.append(base[:10] + rng.sample(base[10:], len(base) - 10))
            else:
                group.append(rng.sample(base, depth))
        groups.append(group)
    return groups


def replay(groups, k, c, page, mode):
    sources = [s for group in groups for s in group]
    owners = [str(g) for g, group in enumerate(groups) for _ in group]
    f = Fence(len(sources), k, c) if mode == "global" else PartitionFence(owners, k, c)
    entries = 0
    for start in range(0, max(1, max(map(len, sources))), page):
        for idx, source in enumerate(sources):
            if start != 0 and start >= len(source):
                continue
            items = source[start:start + page]
            f.push(idx, items, done=start + page >= len(source))
            entries += len(items)
        output = f.ordered()
        if output is not None:
            return tuple(output), entries
    raise AssertionError("incomplete")


def local_merge(groups, k, c, page):
    # Each shard stops independently. A local membership proof identifies the
    # only possible global winners from that shard. To merge their scores it
    # waits until every candidate's missing local ranks can contribute zero.
    # The warmed integer table grants advance knowledge of maximum depth.
    depth = max(len(s) for group in groups for s in group)
    table = weights(c, depth)
    entries = 0
    pool = {}
    for group in groups:
        f = Fence(len(group), k, c)
        scores = defaultdict(int)
        seen = [set() for _ in group]
        done = [False] * len(group)
        for start in range(0, max(1, max(map(len, group))), page):
            for i, source in enumerate(group):
                if done[i]:
                    continue
                items = source[start:start + page]
                entries += len(items)
                for rank, doc in enumerate(items, start + 1):
                    scores[doc] += table[rank]
                seen[i].update(items)
                done[i] = start + page >= len(source)
                f.push(i, items, done=done[i])
            candidates = f.members()
            if candidates is not None and all(done[i] or candidates <= ids for i, ids in enumerate(seen)):
                pool.update((doc, scores[doc]) for doc in candidates)
                break
        else:
            raise AssertionError("shard scores unresolved")
    return tuple(heapq.nsmallest(k, pool, key=lambda d: (-pool[d], d))), entries


def local_bounds_scan(groups, k, c, page):
    """Recompute exact, support-aware NRA intervals at each synchronous round."""
    depth = max(len(s) for group in groups for s in group)
    table = weights(c, max(1, depth))
    scores = {}
    owners = {}
    masks = {}
    entries = 0
    for start in range(0, max(1, depth), page):
        tails = []
        for g, group in enumerate(groups):
            upper = []
            for i, source in enumerate(group):
                items = source[start:start + page]
                entries += len(items)
                for rank, doc in enumerate(items, start + 1):
                    scores[doc] = scores.get(doc, 0) + table[rank]
                    owners[doc] = g
                    masks[doc] = masks.get(doc, 0) | (1 << i)
                upper.append(table[start + page + 1] if start + page < len(source) else 0)
            tails.append(upper)

        top = heapq.nsmallest(k, scores, key=lambda d: (-scores[d], d))
        unseen = max(map(sum, tails))
        if unseen == 0:
            return tuple(top), entries
        if len(top) < k or scores[top[-1]] <= unseen:
            continue
        possible = {}
        upper_scores = {}
        for d, lo in scores.items():
            group, mask = owners[d], masks[d]
            key = (group, mask)
            if key not in possible:
                possible[key] = sum(x for i, x in enumerate(tails[group]) if not mask & (1 << i))
            upper_scores[d] = lo + possible[key]
        selected = set(top)
        challenger = min(((-v, d) for d, v in upper_scores.items() if d not in selected), default=None)
        for d in reversed(top):
            if challenger is not None and (-scores[d], d) >= challenger:
                break
            value = (-upper_scores[d], d)
            challenger = min(value, challenger) if challenger else value
        else:
            return tuple(top), entries
    raise AssertionError("no certified prefix")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key, default in [("shards", 8), ("rankers", 3), ("depth", 512), ("page", 16),
                         ("queries", 8), ("repeats", 3), ("k", 5), ("c", 60), ("seed", 119)]:
        parser.add_argument("--" + key, type=int, default=default)
    parser.add_argument("--trace", help="optional real trace to partition and rerank")
    parser.add_argument("--json", required=True)
    args = parser.parse_args()
    if min(args.shards, args.rankers, args.depth, args.page, args.queries, args.repeats, args.k) < 1 or args.c < 0:
        parser.error("invalid counts")
    rng = random.Random(args.seed)
    timing_rng = random.Random(args.seed + 1)
    if args.trace:
        cases = (("hashed_trace", q, shard_trace(lists, args.shards)) for _, q, lists in
                 load_trace(args.trace, args.queries))
    else:
        cases = ((kind, str(q), synthetic(kind, args.shards, args.rankers, args.depth, rng))
                 for kind in ("agreement", "hot_shard", "disagreement")
                 for q in range(args.queries))
    methods = {"global": lambda groups, k, c, page: replay(groups, k, c, page, "global"),
               "partition": lambda groups, k, c, page: replay(groups, k, c, page, "partition"),
               "local_merge": local_merge, "partition_integer_nra": local_bounds_scan,
               "batch": lambda groups, k, c, page: integer_batch(list(itertools.chain.from_iterable(groups)), k, c, page)}
    rows = []
    for kind, q, groups in cases:
        params = groups, args.k, args.c, args.page
        answer, total = methods["batch"](*params)
        times = defaultdict(list)
        counts = {}
        for rep in range(args.repeats + 1):
            names = list(methods)
            timing_rng.shuffle(names)
            for name in names:
                start = perf_counter_ns()
                actual, entries = methods[name](*params)
                elapsed = (perf_counter_ns() - start) / 1e6
                if actual != answer:
                    raise AssertionError((kind, q, name, answer, actual))
                if rep:
                    times[name].append(round(elapsed, 5))
                counts[name] = entries
        rows.append({"kind": kind, "query": q, "exact_top": answer, "total": total,
                     "entries": counts, "times_ms": times})
    summary = {}
    for kind, group_iter in itertools.groupby(rows, key=lambda row: row["kind"]):
        subset = list(group_iter)
        summary[kind] = {}
        for name in methods:
            samples = sorted(median(r["times_ms"][name]) for r in subset)
            summary[kind][name] = {
                "queries": len(subset),
                "queries_stopping_before_exhaustion": sum(r["entries"][name] < r["total"] for r in subset),
                "same_count_as_support_nra": sum(r["entries"][name] == r["entries"]["partition_integer_nra"]
                                                 for r in subset),
                "entries": sum(r["entries"][name] for r in subset),
                "p50_ms": round(median(samples), 4),
                "p95_ms": round(samples[math.ceil(.95 * len(samples)) - 1], 4)}
    paths = [Path(__file__), ROOT / "bench.py", ROOT / "rankfence" / "core.py", ROOT / "rankfence" / "partition.py"]
    if args.trace:
        paths += [Path(args.trace)]
    result = {"schema": 1, "config": vars(args), "platform":platform.platform(),
              "python": platform.python_version(),
              "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
              "summary": summary, "queries":rows}
    Path(args.json).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
