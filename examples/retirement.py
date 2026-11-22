"""Measure actual async cursor lifetimes in a controlled shard-straggler scenario.

Run python examples/retirement.py from the checkout root. This experiment
changes only per-shard pruning, keeping whole-answer early exit enabled.
"""
import argparse
import asyncio
import json
import platform
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rankfence import Page, fuse_partitioned, verify


async def trial(args, prune):
    started = {}
    released = {}
    cancelled = set()
    final_page_at = []
    pages = [0] * args.cold_shards

    async def fixed(ids):
        yield Page(ids, done=True)

    async def slow_winner():
        await asyncio.sleep(args.winner_ms / 1000)
        final_page_at.append(perf_counter())
        yield Page(["b"], done=True)

    async def cold(shard):
        started[shard] = perf_counter()
        try:
            for page in range(1000):
                await asyncio.sleep(args.page_ms / 1000)
                pages[shard] += 1
                yield Page([f"cold-{shard}/{page}"], done=page == 999)
        except asyncio.CancelledError:
            cancelled.add(shard)
            raise
        finally:
            released[shard] = perf_counter()

    sources = [fixed(["a", "b"]), fixed(["b", "a"]), slow_winner()]
    sources.extend(cold(i) for i in range(args.cold_shards))
    groups = ["winning"] * 3 + [f"shard-{i}" for i in range(args.cold_shards)]
    begin = perf_counter()
    result = await fuse_partitioned(sources, groups, 2, prune=prune, receipt=True)
    elapsed_ms = 1000 * (perf_counter() - begin)
    if result.ids != ("b", "a") or not verify(result.receipt):
        raise AssertionError("incorrect answer or invalid receipt")
    if set(started) != set(released) or len(released) != args.cold_shards:
        raise AssertionError("leaked or unopened cursor")
    return {
        "prune": prune, "elapsed_ms": round(elapsed_ms, 3),
        "cold_pages_yielded": sum(pages), "cold_pulls_cancelled": len(cancelled),
        "cold_closed_before_deciding_page": sum(t < final_page_at[0] for t in released.values()),
        "cold_cursor_ms": round(1000 * sum(t - started[i] for i, t in released.items()), 3),
        "ids": result.ids, "receipt_verified": True,
    }


async def benchmark(args):
    rows = []
    for r in range(args.repeat):
        for prune in ((False, True) if r % 2 == 0 else (True, False)):
            rows.append(await trial(args, prune))
    columns = ["elapsed_ms", "cold_pages_yielded", "cold_pulls_cancelled",
               "cold_closed_before_deciding_page", "cold_cursor_ms"]
    return {
        "experiment": "single-process synthetic cooperative async cursors",
        "python": platform.python_version(), "platform": platform.platform(),
        "settings": vars(args), "runs": rows,
        "medians": {str(prune).lower(): {
            key: median(row[key] for row in rows if row["prune"] == prune)
            for key in columns} for prune in (False, True)},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold-shards", type=int, default=29)
    parser.add_argument("--winner-ms", type=float, default=40)
    parser.add_argument("--page-ms", type=float, default=3)
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()
    if min(args.cold_shards, args.winner_ms, args.page_ms, args.repeat) <= 0:
        parser.error("all parameters must be positive")
    print(json.dumps(asyncio.run(benchmark(args)), indent=2))
