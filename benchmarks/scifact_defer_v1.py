"""Run against the pinned SciFact data.tar.gz. Write one paired TSV row per claim and layout."""
import asyncio
from contextlib import ExitStack, closing
import csv
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3
import sys
import tarfile
import tempfile
from time import perf_counter_ns
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.reference.rankseal.core import Fusion
from benchmarks.sqlite_interrupt_v1 import SQL, SQL_ALT
from examples.sqlite_interrupt import QueryStats, sqlite_pages
from rankfence import fuse_partitioned

SHA = "11c621288d41ac144d29b13b0f8503b3820b7d6e8b1f6ff24dff335c196d76be"
STOP = set("the and for are that from with this than which have into their between".split())


def load(archive, root):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA:
        raise ValueError("unexpected SciFact release hash")
    with tarfile.open(archive) as data:
        docs = [json.loads(line) for line in data.extractfile("data/corpus.jsonl")]
        claims = [json.loads(line) for line in data.extractfile("data/claims_dev.jsonl")]
    paths = [root / "catalog.db", root / "archive.db"]
    for i, path in enumerate(paths):
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE VIRTUAL TABLE docs USING fts5(id UNINDEXED, title, body)")
            for doc in docs:
                key = str(doc["doc_id"])
                is_archive = zlib.crc32(key.encode()) % 8 != 0
                if is_archive == i:
                    db.execute("INSERT INTO docs VALUES (?,?,?)",
                               (key, doc["title"], " ".join(doc["abstract"])))
            db.commit()
    return paths, claims


def exhaustive(specs, query):
    scores = {}
    for db, sql in specs:
        with closing(sqlite3.connect(db)) as conn:
            for rank, (key,) in enumerate(conn.execute(sql, (query,)), 1):
                scores[key] = scores.get(key, 0) + Fraction(1, 60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))[:1]


def seal_shard(specs, query):
    """Certify the local winner and its exact score before merging the two shards."""
    with ExitStack() as stack:
        db = stack.enter_context(closing(sqlite3.connect(specs[0][0])))
        if len(specs) == 1:
            row = db.execute(specs[0][1] + " LIMIT 1", (query,)).fetchone()
            return ({row[0]: Fraction(1, 61)}, 1) if row else ({}, 0)
        cursors = [stack.enter_context(closing(db.execute(sql, (query,)))) for _, sql in specs]
        fusion = Fusion(len(cursors), k=1, constant=60)
        ranks = [{} for _ in cursors]
        active = set(range(len(cursors)))
        while active:
            for i in sorted(active):
                ids = [row[0] for row in cursors[i].fetchmany(16)]
                first = len(ranks[i]) + 1
                ranks[i].update((doc, rank) for rank, doc in enumerate(ids, first))
                fusion.push(i, ids)
                if len(ids) < 16:
                    active.remove(i)
                    fusion.close(i)
            winners = fusion.certified()
            if winners is not None and all(doc in ranks[i] for doc in winners for i in active):
                scores = {doc: sum(Fraction(1, 60 + r[doc]) for r in ranks if doc in r)
                          for doc in winners}
                return scores, sum(map(len, ranks))
        raise AssertionError("RankSeal failed to resolve an exhausted shard")


async def seal(specs, query):
    left, right = await asyncio.gather(asyncio.to_thread(seal_shard, specs[:2], query),
                                       asyncio.to_thread(seal_shard, specs[2:], query))
    scores = {**left[0], **right[0]}
    return sorted(scores, key=lambda doc: (-scores[doc], doc))[:1], left[1] + right[1]


async def measure(paths, claims):
    rng = random.Random(1125)
    cases = [(claim, layout) for claim in claims for layout in ("2+1", "2+2")]
    rng.shuffle(cases)
    writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    methods = ["finish", "interrupt", "defer", "rankseal"]
    writer.writerow(["claim", "layout", "winner", "cited_hit", "run_order"] +
                    [f"{method}_{metric}" for method in methods
                     for metric in ("ms", "rows", "archive_interrupted", "archive_started")])
    for claim, layout in cases:
        tokens = set(re.findall("[a-z0-9]{3,}", claim["claim"].lower())) - STOP
        query = " OR ".join(f'"{token}"' for token in sorted(tokens))
        specs = [(paths[0], SQL), (paths[0], SQL_ALT), (paths[1], SQL)]
        if layout == "2+2":
            specs.append((paths[1], SQL_ALT))
        groups = ["catalog", "catalog"] + ["archive"] * (len(specs)-2)
        expected = exhaustive(specs, query)
        order = methods.copy()
        rng.shuffle(order)
        observations = {}
        for method in order:
            start = perf_counter_ns()
            if method == "rankseal":
                ids, rows = await seal(specs, query)
                ms = (perf_counter_ns() - start) / 1e6
                if ids != expected:
                    raise AssertionError((claim["id"], layout, method, ids, expected))
                observations[method] = (round(ms, 4), rows, 0, len(specs)-2)
                continue
            stats = [QueryStats() for _ in specs]
            started = set()

            async def observed(i):
                started.add(i)
                path, sql = specs[i]
                source = sqlite_pages(path, sql, (query,), width=16, stats=stats[i],
                                      interrupt=method != "finish")
                try:
                    async for page in source:
                        yield page
                finally:
                    await source.aclose()

            result = await fuse_partitioned([observed(i) for i in range(len(specs))], groups, 1,
                                            defer_groups=["archive"] if method == "defer" else ())
            ms = (perf_counter_ns() - start) / 1e6
            if list(result.ids) != expected:
                raise AssertionError((claim["id"], layout, method, result.ids, expected))
            observations[method] = (round(ms, 4), sum(s.rows for s in stats),
                                    sum(s.stopped_in_sql for s in stats[2:]),
                                    sum(i >= 2 for i in started))
        hit = bool(expected and int(expected[0]) in claim["cited_doc_ids"])
        writer.writerow([claim["id"], layout, next(iter(expected), ""), int(hit), ",".join(order)] +
                        [x for method in methods for x in observations[method]])


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(measure(*load(Path(sys.argv[1]), Path(tmp))))
