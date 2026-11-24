"""Run: python benchmarks/sqlite_interrupt_v1.py > results/sqlite-interrupt-v1.json"""
import asyncio
from contextlib import closing
from fractions import Fraction
import json
import platform
import random
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.sqlite_interrupt import QueryStats, sqlite_pages
from rankfence import fuse_partitioned

SQL = """SELECT id FROM docs WHERE docs MATCH ?
         ORDER BY bm25(docs, 0, 1, 4), rowid"""
SQL_ALT = SQL.replace("0, 1, 4", "0, 4, 1")


def build(path, group, size):
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE VIRTUAL TABLE docs USING fts5(id UNINDEXED, title, body)")
        db.executemany("INSERT INTO docs VALUES (?, ?, ?)",
                       ((f"{group}/{i}", "alpha " * (i % 7 + 1), "alpha " * (i % 7 + 1))
                        for i in range(size)))
        db.commit()


def oracle(specs):
    scores = {}
    for path, sql, term in specs:
        with closing(sqlite3.connect(path)) as db:
            for rank, (doc,) in enumerate(db.execute(sql, (term,)), 1):
                scores[doc] = scores.get(doc, 0) + Fraction(1, 60 + rank)
    return sorted(scores, key=lambda doc: (-scores[doc], doc))[:1]


async def bench(root):
    paths = [root / "catalog.db", root / "archive.db"]
    build(paths[0], "catalog", 70)
    build(paths[1], "archive", 30000)
    groups = ["catalog", "catalog", "archive"]
    rng = random.Random(1124)
    samples = []
    for label, terms in [("catalog agreement", ["alpha"]*3),
                         ("archive required", ["missing", "missing", "alpha"]),
                         ("no hits", ["missing"]*3)]:
        specs = list(zip([paths[0], paths[0], paths[1]], [SQL, SQL_ALT, SQL], terms))
        expected = oracle(specs)
        for repeat in range(42):
            order = [True, False]
            rng.shuffle(order)
            for interrupt in order:
                start = perf_counter_ns()
                stats = [QueryStats() for _ in specs]
                sources = [sqlite_pages(p, sql, (term,), width=16, stats=s, interrupt=interrupt)
                           for (p, sql, term), s in zip(specs, stats)]
                result = await fuse_partitioned(sources, groups, 1)
                ms = (perf_counter_ns() - start) / 1e6
                if list(result.ids) != expected:
                    raise AssertionError((result.ids, expected))
                if repeat >= 2:
                    samples.append([label, repeat-2, interrupt, round(ms, 4),
                                    stats[-1].progress_calls, stats[-1].stopped_in_sql,
                                    sum(s.rows for s in stats), expected])
    return samples


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(json.dumps(dict(python=platform.python_version(), sqlite=sqlite3.sqlite_version,
                              columns=["case", "pair", "interrupt", "ms", "archive_progress_calls",
                                       "archive_interrupted", "total_rows", "ids"],
                              samples=asyncio.run(bench(Path(directory)))), separators=(",", ":")))
