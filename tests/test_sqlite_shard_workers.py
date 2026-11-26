import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from examples.sqlite_interrupt import QueryStats, sqlite_shard_pages


class ShardWorkers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "shard.db"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE docs(id TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO docs VALUES (?)", [("a",), ("b",)])
            db.commit()

    async def test_cancel_one_reader_and_resume_its_sibling(self):
        slow = """WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1
                  FROM n WHERE x<100000000) SELECT CAST(sum(x) AS TEXT) FROM n"""
        stats = [QueryStats(), QueryStats()]
        queries = [("SELECT id FROM docs ORDER BY id", ()), (slow, ())]
        async with sqlite_shard_pages(self.path, queries, width=1, stats=stats) as (fast, long):
            self.assertEqual((await anext(fast)).ids, ["a"])
            pull = asyncio.create_task(anext(long))
            try:
                async def started():
                    while not stats[1].progress_calls:
                        await asyncio.sleep(0.001)
                await asyncio.wait_for(started(), timeout=5)
            finally:
                pull.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(pull, timeout=5)
            self.assertTrue(stats[1].stopped_in_sql)
            self.assertEqual((await anext(fast)).ids, ["b"])
            self.assertTrue((await anext(fast)).done)

    async def test_unopened_reader_does_no_sql(self):
        async with sqlite_shard_pages(self.path, [("SELECT * FROM absent", ())]):
            pass

    async def test_query_error_and_scope_exit_close_sibling(self):
        async with sqlite_shard_pages(self.path, [("SELECT id FROM docs", ()),
                                                 ("SELECT id FROM absent", ())]) as (a, b):
            await anext(a)
            with self.assertRaises(sqlite3.OperationalError):
                await anext(b)
        with self.assertRaises(StopAsyncIteration):
            await anext(a)

    async def test_invalid_callback_interval_fails_before_startup(self):
        for invalid in (0, True, 2**31):
            with self.assertRaises(ValueError):
                async with sqlite_shard_pages(self.path, [("SELECT id FROM docs", ())],
                                              progress_ops=invalid) as readers:
                    await anext(readers[0])
