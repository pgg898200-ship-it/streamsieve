"""Run the RankSeal Fusion release candidate against RankFence on the same reads.

Pass a local RankSeal checkout. Its core source must match the benchmark pin.
Both packages use unit weights, standard RRF with c=60, and ascending ID ties.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bench import integer_batch, load_trace, pages, stream, weights
from partition_replay import replay, shard_trace
from rankfence import PartitionFence
from rankfence.retire import retirable_sources


RANKSEAL_CORE_SHA256 = "6b0603d10ef7aa4524558e06799e3e3c541eb52d94fbc1e82317f2de08804e6f"


def load_rankseal(checkout):
    root = Path(checkout).resolve()
    source = root / "rankseal" / "core.py"
    if hashlib.sha256(source.read_bytes()).hexdigest() != RANKSEAL_CORE_SHA256:
        raise ValueError(f"RankSeal core does not match the published comparison pin: {source}")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    try:
        from rankseal.core import Fusion
        if Path(sys.modules[Fusion.__module__].__file__).resolve() != source:
            raise ValueError("imported RankSeal from a different checkout")
    finally:
        sys.path.pop(0)
    return Fusion


def rankseal_stream(Fusion, lists, k, c, page):
    state = Fusion(len(lists), k=k, constant=c)
    read = 0
    for batch in pages(lists, page):
        for i, items, done in batch:
            state.push(i, items)
            read += len(items)
            if done:
                state.close(i)
        result = state.certified()
        if result is not None:
            return tuple(result), read
    raise AssertionError("RankSeal did not certify after exhaustion")


def rankseal_local(Fusion, groups, k, c, page):
    # Top k globally is contained in the union of top k per disjoint shard.
    # Independently certify those candidates and wait until their scores are
    # exact to support a cross-shard merge. This wrapper uses sorted reads only.
    depth = max(len(s) for group in groups for s in group)
    scale = weights(c, depth)
    candidates = {}
    reads = 0
    for group in groups:
        state = Fusion(len(group), k=k, constant=c)
        seen = [set() for _ in group]
        done = [False] * len(group)
        ranks = [0] * len(group)
        scores = defaultdict(int)
        for batch in pages(group, page):
            for i, items, end in batch:
                state.push(i, items)
                seen[i].update(items)
                reads += len(items)
                for doc in items:
                    ranks[i] += 1
                    scores[doc] += scale[ranks[i]]
                if end:
                    state.close(i)
                    done[i] = True
            winners = state.certified()
            if winners is not None and all(
                done[i] or all(d in seen[i] for d in winners) for i in range(len(group))
            ):
                candidates.update((d, scores[d]) for d in winners)
                break
        else:
            raise AssertionError("local score unresolved")
    return tuple(heapq.nsmallest(k, candidates, key=lambda d: (-candidates[d], d))), reads


def partition_retire(groups, k, c, page):
    # Skip future pulls from proven-losing groups. Their unseen suffixes stay
    # open mathematically, so they can never be mistaken for exhausted data.
    owners = [str(i) for i, group in enumerate(groups) for _ in group]
    sources = list(itertools.chain.from_iterable(groups))
    state = PartitionFence(owners, k, c)
    retired = set()
    count = 0
    for start in range(0, max(1, max(map(len, sources))), page):
        for i, source in enumerate(sources):
            if i in retired or (start and start >= len(source)):
                continue
            items = source[start:start + page]
            state.push(i, items, done=start + page >= len(source))
            count += len(items)
        result = state.ordered()
        if result is not None:
            return result, count
        retired.update(retirable_sources(state))
    raise AssertionError("incomplete after every active source finished")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rankseal", required=True, help="path to verified RankSeal checkout")
    p.add_argument("--trace", required=True)
    p.add_argument("--json", required=True)
    p.add_argument("--queries", type=int, default=100)
    p.add_argument("--shards", type=int, default=8)
    p.add_argument("--page", type=int, default=16)
    p.add_argument("--repeats", type=int, default=3)
    args = p.parse_args()
    if min(args.queries, args.shards, args.page, args.repeats) < 1:
        p.error("counts must be positive")
    fusion = load_rankseal(args.rankseal)
    rng = random.Random(13)
    rows = []
    for _, q, flat in load_trace(args.trace, args.queries):
        for kind in ("flat", "hashed_shards"):
            groups = [flat] if kind == "flat" else shard_trace(flat, args.shards)
            sources = list(itertools.chain.from_iterable(groups))
            oracle, available = integer_batch(sources, 5, 60, args.page)
            methods = {
                "rankfence": lambda: stream(sources, 5, 60, args.page),
                "rankseal": lambda: rankseal_stream(fusion, sources, 5, 60, args.page),
            }
            if kind == "hashed_shards":
                methods.update({
                    "partitionfence": lambda: replay(groups, 5, 60, args.page, "partition"),
                    "partition_retire": lambda: partition_retire(groups, 5, 60, args.page),
                    "rankseal_local_merge": lambda: rankseal_local(fusion, groups, 5, 60, args.page)})
            counts, times = {}, defaultdict(list)
            for rep in range(args.repeats + 1):
                order = list(methods)
                rng.shuffle(order)
                for name in order:
                    start = perf_counter_ns()
                    output, reads = methods[name]()
                    ms = (perf_counter_ns() - start) / 1e6
                    if output != oracle:
                        raise AssertionError((q, kind, name, output, oracle))
                    counts[name] = reads
                    if rep:
                        times[name].append(round(ms, 5))
            rows.append({"case": kind, "topic": q, "entries_available": available,
                         "exact_top": oracle, "entries": counts, "time_ms": times})
    summary = {}
    for kind in ("flat", "hashed_shards"):
        group = [r for r in rows if r["case"] == kind]
        summary[kind] = {}
        for name in group[0]["time_ms"]:
            samples = sorted(median(r["time_ms"][name]) for r in group)
            summary[kind][name] = {"queries": len(group),
                "entries": sum(r["entries"][name] for r in group),
                "early": sum(r["entries"][name] < r["entries_available"] for r in group),
                "p50_ms": round(median(samples), 4),
                "p95_ms": round(samples[math.ceil(len(samples) * .95) - 1], 4)}
    files = [Path(__file__), ROOT / "examples" / "partition_replay.py", ROOT / "bench.py",
             ROOT / "rankfence" / "core.py", ROOT / "rankfence" / "partition.py",
             ROOT / "rankfence" / "retire.py", Path(args.trace)]
    result = {"rankseal_core_sha256": RANKSEAL_CORE_SHA256,
              "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
              "config": {"queries": args.queries, "shards": args.shards, "page": args.page,
                         "k": 5, "c": 60, "repeats": args.repeats},
              "platform": platform.platform(), "python": platform.python_version(),
              "summary": summary, "queries": rows}
    Path(args.json).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
