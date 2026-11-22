"""Fuse shard-local SQLite FTS5 rankings of your installed Python documentation.

Run: python examples/sqlite_fts.py --shards 4 --query file --k 3 --page-size 8

Each shard is a separate database with its own BM25 term statistics. Each uses
one static read transaction and a rowid tie-break. Closing a cursor stops extra
client fetches. SQLite may already have scored or sorted all matches.
This local example uses synchronous sqlite3 calls on the event-loop thread.
"""
import argparse
import ast
import asyncio
from collections import Counter
from contextlib import ExitStack, closing
from fractions import Fraction
import json
from pathlib import Path
import sqlite3
import sys
import sysconfig
import tokenize
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rankfence import Fence, Page, fuse, fuse_partitioned, verify


# Fields in order: stable symbol ID (unindexed), name, summary, documentation.
RANKERS = {
    "name": "bm25(0, 10, 1, 1)",
    "summary": "bm25(0, 1, 10, 1)",
    "body": "bm25(0, 1, 1, 10)",
}
SEARCH = """SELECT symbol FROM docs
            WHERE docs MATCH ? AND rank MATCH ?
            ORDER BY rank ASC, rowid ASC"""


def stdlib_documents():
    root = Path(sysconfig.get_path("stdlib"))
    packages = ("asyncio", "concurrent/futures", "email", "http", "unittest", "urllib")
    paths = list(root.glob("*.py"))
    for package in packages:
        paths.extend((root / package).glob("*.py"))

    def walk(node, name, symbol):
        doc = ast.get_docstring(node)
        if doc:
            yield symbol, name, doc.split("\n", 1)[0], doc
        for child in node.body:
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{name}.{child.name}"
                yield from walk(child, qualified, f"{relative}:{child.lineno}:{qualified}")

    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        with tokenize.open(path) as src:
            tree = ast.parse(src.read(), filename=relative)
        name = relative[:-3].replace("/", ".")
        yield from walk(tree, name, relative)


async def ranked_pages(db, query, ranker, width, counts, pulls, shard):
    cursor = db.execute(SEARCH, (query, RANKERS[ranker]))
    try:
        while True:
            rows = cursor.fetchmany(width)
            counts[f"{shard}/{ranker}"] += len(rows)
            pulls[f"{shard}/{ranker}"] += 1
            done = len(rows) < width
            yield Page([row[0] for row in rows], done=done)
            if done:
                return
    finally:
        cursor.close()


def exact_scores(db, args, local_stop):
    """Read one shard, optionally stopping with exact scores for its top k."""
    fence = Fence(len(RANKERS), args.k) if local_stop else None
    seen = [set() for _ in RANKERS]
    done = [False for _ in RANKERS]
    rows = pulls = 0
    scores = {}
    with ExitStack() as stack:
        cursors = [stack.enter_context(closing(db.execute(SEARCH, (args.query, weights))))
                   for weights in RANKERS.values()]
        while not all(done):
            for i, cursor in enumerate(cursors):
                if done[i]:
                    continue
                ids = [r[0] for r in cursor.fetchmany(args.page_size)]
                pulls += 1
                rows += len(ids)
                done[i] = len(ids) < args.page_size
                for rank, doc in enumerate(ids, len(seen[i]) + 1):
                    scores[doc] = scores.get(doc, 0) + Fraction(1, 60 + rank)
                seen[i].update(ids)
                if fence:
                    fence.push(i, ids, done=done[i])
            if fence:
                winners = fence.members()
                if winners is not None and all(done[i] or winners <= seen[i] for i in range(len(cursors))):
                    return {d: scores[d] for d in winners}, rows, pulls
    return scores, rows, pulls


async def replay(dbs, args, documents):
    groups = [str(shard) for shard in range(len(dbs)) for _ in RANKERS]

    async def run(use_groups):
        counts, pulls = Counter(), Counter()
        sources = [ranked_pages(db, args.query, ranker, args.page_size, counts, pulls, shard)
                   for shard, db in enumerate(dbs) for ranker in RANKERS]
        if use_groups:
            result = await fuse_partitioned(sources, groups, args.k, c=60, receipt=True)
        else:
            result = await fuse(sources, args.k, c=60, receipt=True)
        if not verify(result.receipt, source_groups=groups if use_groups else None):
            raise AssertionError("independent receipt verification failed")
        return list(result.ids), counts, sum(pulls.values())

    global_ids, global_counts, global_pulls = await run(False)
    partition_ids, partition_counts, partition_pulls = await run(True)
    def batch(local_stop):
        scores = {}
        rows = pulls = 0
        for db in dbs:
            shard_scores, shard_rows, shard_pulls = exact_scores(db, args, local_stop)
            scores.update(shard_scores)
            rows += shard_rows
            pulls += shard_pulls
        return sorted(scores, key=lambda s: (-scores[s], s))[:args.k], rows, pulls

    expected, exhaustive_rows, exhaustive_pulls = batch(False)
    local_ids, local_rows, local_pulls = batch(True)
    if any(ids != expected for ids in (partition_ids, global_ids, local_ids)):
        raise AssertionError("full exact RRF and a streamed strategy disagree")
    return {
        "query": args.query, "k": args.k, "page_size": args.page_size,
        "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
        "shards": len(dbs), "rankers_per_shard": len(RANKERS),
        "documents_indexed": documents, "ids": expected,
        "rows_returned": {"partition": sum(partition_counts.values()),
                          "unrestricted": sum(global_counts.values()),
                          "local_exact_merge": local_rows, "exhaustive": exhaustive_rows},
        "fetchmany_calls": {"partition": partition_pulls, "unrestricted": global_pulls,
                            "local_exact_merge": local_pulls, "exhaustive": exhaustive_pulls},
        "partition_rows_by_source": dict(partition_counts),
        "receipts_verified": True, "matches_exhaustive": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="file", help="SQLite FTS5 MATCH expression")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--page-size", type=int, default=8)
    parser.add_argument("--shards", type=int, default=1)
    args = parser.parse_args()
    if min(args.k, args.page_size, args.shards) < 1:
        parser.error("--k, --page-size and --shards must be positive")
    with ExitStack() as stack:
        dbs = [stack.enter_context(closing(sqlite3.connect(":memory:"))) for _ in range(args.shards)]
        for db in dbs:
            db.execute("CREATE VIRTUAL TABLE docs USING fts5(symbol UNINDEXED, name, summary, body)")
        documents = 0
        for doc in stdlib_documents():
            shard = zlib.crc32(doc[0].encode()) % args.shards
            dbs[shard].execute("INSERT INTO docs VALUES (?, ?, ?, ?)", doc)
            documents += 1
        for db in dbs:
            db.commit()
            db.execute("BEGIN")
        print(json.dumps(asyncio.run(replay(dbs, args, documents)), indent=2))


if __name__ == "__main__":
    main()
