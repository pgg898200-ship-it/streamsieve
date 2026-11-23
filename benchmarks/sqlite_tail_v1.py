"""Run from checkout: python benchmarks/sqlite_tail_v1.py > results/sqlite-tail-v1.json

Warm in-memory SQLite FTS5. Includes SQL execution, ranking, Python fusion
and cursor cleanup. Excludes corpus construction, event-loop startup and
correctness checks. The sqlite3 driver is synchronous for both strategies.
"""
import asyncio
from collections import Counter
from contextlib import ExitStack, closing
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import sqlite3
import sys
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.sqlite_fts import RANKERS, SEARCH, ranked_pages, stdlib_documents
from rankfence import fuse_partitioned
from benchmarks.reference.rankseal.core import Fusion


def exhaustive(dbs, query, k):
    scores = {}
    rows = 0
    for db in dbs:
        for weights in RANKERS.values():
            with closing(db.execute(SEARCH, (query, weights))) as cursor:
                for rank, (doc,) in enumerate(cursor, 1):
                    scores[doc] = scores.get(doc, 0) + Fraction(1, 60 + rank)
                    rows += 1
    return sorted(scores, key=lambda doc: (-scores[doc], doc))[:k], rows


def rankseal_local(dbs, query, k):
    candidates = {}
    rows = 0
    for db in dbs:
        fusion = Fusion(len(RANKERS), k=k, constant=60)
        with ExitStack() as stack:
            cursors = [stack.enter_context(closing(db.execute(SEARCH, (query, weights))))
                       for weights in RANKERS.values()]
            ranks = [{} for _ in cursors]
            active = set(range(len(cursors)))
            while active:
                for source in sorted(active):
                    ids = [row[0] for row in cursors[source].fetchmany(8)]
                    rows += len(ids)
                    first = len(ranks[source]) + 1
                    ranks[source].update((doc, i) for i, doc in enumerate(ids, first))
                    fusion.push(source, ids)
                    if len(ids) < 8:
                        fusion.close(source)
                        active.remove(source)
                winners = fusion.certified()
                if winners is not None and all(doc in ranks[i] for doc in winners for i in active):
                    for doc in winners:
                        candidates[doc] = sum(Fraction(1, 60 + rank[doc])
                                              for rank in ranks if doc in rank)
                    break
    return sorted(candidates, key=lambda doc: (-candidates[doc], doc))[:k], rows


async def partition(dbs, query, k):
    counts, pulls = Counter(), Counter()
    sources = [ranked_pages(db, query, ranker, 8, counts, pulls, i)
               for i, db in enumerate(dbs) for ranker in RANKERS]
    groups = [str(i) for i in range(len(dbs)) for _ in RANKERS]
    result = await fuse_partitioned(sources, groups, k)
    return list(result.ids), sum(counts.values())


async def measure(dbs):
    rng = random.Random(23011)
    results = []
    for query in ("file", "exception", "nonexistenttoken"):
        for k in (1, 10):
            expected, _ = exhaustive(dbs, query, k)
            samples = {name: [] for name in ("partition", "rankseal_local", "exhaustive")}
            rows = {}
            for repeat in range(42):
                order = list(samples)
                rng.shuffle(order)
                for name in order:
                    start = time.perf_counter_ns()
                    if name == "partition":
                        ids, count = await partition(dbs, query, k)
                    elif name == "rankseal_local":
                        ids, count = rankseal_local(dbs, query, k)
                    else:
                        ids, count = exhaustive(dbs, query, k)
                    elapsed = (time.perf_counter_ns() - start) / 1e6
                    if ids != expected:
                        raise AssertionError((name, query, k, ids, expected))
                    rows[name] = count
                    if repeat >= 2:
                        samples[name].append(round(elapsed, 6))
            summary = {name: {f"p{p}_ms": sorted(times)[math.ceil(len(times)*p/100)-1]
                              for p in (50, 95)} for name, times in samples.items()}
            results.append(dict(query=query, k=k, rows=rows, ids=expected,
                                summary=summary, samples_ms=samples))
    return results


def main():
    with ExitStack() as stack:
        dbs = [stack.enter_context(closing(sqlite3.connect(":memory:"))) for _ in range(4)]
        for db in dbs:
            db.execute("CREATE VIRTUAL TABLE docs USING fts5(symbol UNINDEXED, name, summary, body)")
        digest = hashlib.sha256()
        documents = 0
        for doc in stdlib_documents():
            db = dbs[zlib.crc32(doc[0].encode()) % len(dbs)]
            db.execute("INSERT INTO docs VALUES (?, ?, ?, ?)", doc)
            digest.update((json.dumps(doc) + "\n").encode())
            documents += 1
        for db in dbs:
            db.commit()
            db.execute("BEGIN")
        print(json.dumps(dict(python=platform.python_version(), sqlite=sqlite3.sqlite_version,
                              machine=platform.machine(), corpus_sha256=digest.hexdigest(),
                              documents=documents, runs=asyncio.run(measure(dbs))), indent=2))


if __name__ == "__main__":
    main()
