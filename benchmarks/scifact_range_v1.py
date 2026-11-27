"""Compare a top-one ID-range preflight with eager execution on matched SciFact queries."""
import asyncio
from contextlib import closing
import csv
import json
from pathlib import Path
import random
import re
import sqlite3
import sys
import tarfile
from tempfile import TemporaryDirectory
from time import perf_counter_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.scifact_defer_v1 import SQL, SQL_ALT, STOP, exhaustive, load, seal
from examples.range_top1 import certified_first, probe_catalog, probe_possible, sqlite_first


def range_paths(archive, root):
    with tarfile.open(archive) as data:
        docs = sorted((json.loads(line) for line in data.extractfile("data/corpus.jsonl")),
                      key=lambda doc: str(doc["doc_id"]))
    paths = []
    for name, group in [("catalog", docs[:670]), ("archive", docs[670:])]:
        path = root / f"range-{name}.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE VIRTUAL TABLE docs USING fts5(id UNINDEXED, title, body)")
            db.executemany("INSERT INTO docs VALUES (?,?,?)",
                           ((str(d["doc_id"]), d["title"], " ".join(d["abstract"])) for d in group))
            db.commit()
        paths.append(path)
    return paths


async def measure(archive, root):
    hashed, claims = load(archive, root)
    layouts = {"hash": hashed, "range": range_paths(archive, root)}
    floors = {}
    for layout, paths in layouts.items():
        floors[layout] = []
        for path in paths:
            with closing(sqlite3.connect(path)) as db:
                floors[layout].append(db.execute("SELECT min(id COLLATE BINARY) FROM docs").fetchone()[0])
    rng = random.Random(1127)
    cases = [(layout, claim) for layout in layouts for claim in claims]
    rng.shuffle(cases)
    writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    writer.writerow(["layout", "claim", "order", "method", "ms", "probed", "skipped", "winner", "cited"])
    for layout, claim in cases:
        paths = layouts[layout]
        tokens = set(re.findall("[a-z0-9]{3,}", claim["claim"].lower())) - STOP
        term = " OR ".join(f'"{token}"' for token in sorted(tokens))
        queries = [(q, (term,)) for q in (SQL, SQL_ALT)]
        specs = [(p, q) for p in paths for q, _ in queries]
        expected = exhaustive(specs, term)
        methods = ["fence", "fence_range", "fence_auto", "seal", "seal_range", "seal_auto"]
        rng.shuffle(methods)
        for i, method in enumerate(methods):
            start = perf_counter_ns()
            skipped = False
            catalog_min, archive_min = floors[layout]
            catalog_floor = catalog_min if method.endswith("_auto") else None
            enabled = method.endswith(("_auto", "_range"))
            probed = enabled and probe_possible(catalog_floor, archive_min)
            if method.startswith("fence"):
                ids, skipped = await sqlite_first(paths, queries, catalog_floor=catalog_floor,
                                                  archive_floor=archive_min, probe=enabled)
            else:
                first = None
                if probed:
                    heads = await probe_catalog(paths[0], queries)
                    first = certified_first([(doc, 1) for doc in heads], [(2, archive_min)])
                if first is None:
                    ids, _ = await seal(specs, term)
                else:
                    ids, skipped = [first], True
            ms = (perf_counter_ns() - start) / 1e6
            if ids != expected:
                raise AssertionError((claim["id"], layout, method, ids, expected))
            hit = bool(ids and int(ids[0]) in claim["cited_doc_ids"])
            writer.writerow([layout, claim["id"], i, method, round(ms, 4), int(probed), int(skipped),
                             ids[0] if ids else "", int(hit)])


if __name__ == "__main__":
    with TemporaryDirectory() as tmp:
        asyncio.run(measure(Path(sys.argv[1]), Path(tmp)))
