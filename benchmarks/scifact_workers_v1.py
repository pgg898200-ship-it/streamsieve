"""Compare workers per ranker and per shard under one and four concurrent clients."""
import asyncio
from contextlib import AsyncExitStack
import csv
from pathlib import Path
import random
import re
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.scifact_defer_v1 import SQL, SQL_ALT, STOP, exhaustive, load, seal
from examples.sqlite_interrupt import sqlite_pages, sqlite_shard_pages
from rankfence import fuse_partitioned


async def run(method, paths, query):
    specs = [(path, sql) for path in paths for sql in (SQL, SQL_ALT)]
    if method == "rankseal":
        ids, _ = await seal(specs, query)
        return ids
    async with AsyncExitStack() as stack:
        interval = 4096 if method.endswith("_4096") else 256
        if method.startswith("per_shard"):
            readers = []
            for path in paths:
                shard = sqlite_shard_pages(path, [(sql, (query,)) for sql in (SQL, SQL_ALT)],
                                           progress_ops=interval)
                readers.extend(await stack.enter_async_context(shard))
        else:
            readers = [sqlite_pages(p, sql, (query,), progress_ops=interval) for p, sql in specs]
        result = await fuse_partitioned(readers, ["catalog"]*2 + ["archive"]*2, 1)
    return list(result.ids)


async def measure(paths, claims):
    rng = random.Random(1126)
    methods = ["per_ranker", "per_ranker_4096", "per_shard", "per_shard_4096", "rankseal"]
    cases = []
    specs = [(p, sql) for p in paths for sql in (SQL, SQL_ALT)]
    for claim in claims:
        tokens = set(re.findall("[a-z0-9]{3,}", claim["claim"].lower())) - STOP
        query = " OR ".join(f'"{token}"' for token in sorted(tokens))
        cases.append((claim["id"], query, exhaustive(specs, query)))
    rng.shuffle(cases)
    # Every claim runs once at each load. Interleave blocks of both loads.
    blocks = [cases[i:i+n] for n in (1, 4) for i in range(0, len(cases), n)]
    rng.shuffle(blocks)
    out = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    out.writerow(["block", "clients", "order", "method", "claim", "winner", "ms", "block_ms"])
    for block, cases_in_block in enumerate(blocks):
        order = rng.sample(methods, len(methods))
        for method in order:
            start = perf_counter_ns()

            async def one(case):
                claim, query, expected = case
                ids = await run(method, paths, query)
                elapsed = (perf_counter_ns() - start) / 1e6
                if ids != expected:
                    raise AssertionError((method, claim, ids, expected))
                return claim, ids[0] if ids else "", round(elapsed, 4)

            rows = await asyncio.gather(*(one(case) for case in cases_in_block))
            wall_ms = round((perf_counter_ns() - start) / 1e6, 4)
            for row in rows:
                out.writerow([block, len(rows), ",".join(order), method, *row, wall_ms])


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        asyncio.run(measure(*load(Path(sys.argv[1]), Path(directory))))
