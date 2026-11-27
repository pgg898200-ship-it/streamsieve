from collections import defaultdict
from fractions import Fraction
from itertools import permutations, product
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from examples.range_top1 import certified_first, probe_possible, sqlite_first


class RangeProof(unittest.TestCase):
    def test_against_exhaustive_weighted_fusion(self):
        # Interleaved and separated IDs exercise both outcomes on equal mass.
        for local, remote in [(("a", "b"), ("c", "d")), (("b", "d"), ("a", "c"))]:
            def rankings(ids):
                return [r for n in range(3) for r in permutations(ids, n)]
            for a, b, c, d in product(rankings(local), rankings(local),
                                      rankings(remote), rankings(remote)):
                for weights in [(1, 1, 1, 1), (1, 2, 1, 1), (1, 1, 2, 1)]:
                    heads = [(next(iter(r), None), w) for r, w in zip((a, b), weights)]
                    answer = certified_first(heads, [(sum(weights[2:]), min(remote))])
                    if answer is not None:
                        scores = defaultdict(Fraction)
                        for weight, ranking in zip(weights, [a, b, c, d]):
                            for rank, doc in enumerate(ranking, 1):
                                scores[doc] += Fraction(weight, 60 + rank)
                        self.assertEqual(answer, min(scores, key=lambda doc: (-scores[doc], doc)))

    def test_unknown_floor_and_multiple_rivals(self):
        self.assertIsNone(certified_first([("b", 2)], [(1, None), (2, None)]))
        self.assertIsNone(certified_first([("b", 2)], [(2, "a")]))
        self.assertEqual(certified_first([("b", 2)], [(1, None), (2, "c")]), "b")

    def test_impossible_probe_is_bypassed(self):
        self.assertFalse(probe_possible("b", "a"))
        self.assertFalse(probe_possible(None, None))
        self.assertTrue(probe_possible("a", "b"))
        self.assertTrue(probe_possible(None, "b"))

    def test_invalid_weights(self):
        with self.assertRaises(ValueError):
            certified_first([("a", True)], [])
        with self.assertRaises(ValueError):
            certified_first([("a", 1)], [(0, "z")])


class RangeSQLite(unittest.IsolatedAsyncioTestCase):
    async def test_unopened_archive_and_fallback(self):
        with TemporaryDirectory() as tmp:
            paths = [Path(tmp) / "primary", Path(tmp) / "secondary"]
            def build(path, key):
                db = sqlite3.connect(path)
                try:
                    db.execute("CREATE TABLE docs(id TEXT)")
                    db.execute("INSERT INTO docs VALUES (?)", (key,))
                    db.commit()
                finally:
                    db.close()
            build(paths[0], "b")
            queries = [("SELECT id FROM docs ORDER BY id", ())] * 2
            # This fixture's unopened archive represents an immutable range >= z.
            self.assertEqual(await sqlite_first(paths, queries, archive_floor="z"), (["b"], True))
            self.assertFalse(paths[1].exists())
            build(paths[1], "a")
            self.assertEqual(await sqlite_first(paths, queries, archive_floor="a"), (["a"], False))

    async def test_metadata_bypass_does_not_issue_limit_one(self):
        from unittest.mock import patch
        with TemporaryDirectory() as tmp:
            paths = [Path(tmp) / "secondary-first", Path(tmp) / "primary-first"]
            for path, key in zip(paths, ["b", "a"]):
                db = sqlite3.connect(path)
                try:
                    db.execute("CREATE TABLE docs(id TEXT)")
                    db.execute("INSERT INTO docs VALUES (?)", (key,))
                    db.commit()
                finally:
                    db.close()
            with patch("examples.range_top1.probe_catalog", side_effect=AssertionError("probed")):
                ids, skipped = await sqlite_first(paths, [("SELECT id FROM docs ORDER BY id", ())],
                                                  catalog_floor="b", archive_floor="a")
            self.assertEqual((ids, skipped), (["a"], False))
