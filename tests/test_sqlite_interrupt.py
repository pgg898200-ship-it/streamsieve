import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from examples.sqlite_interrupt import QueryStats, sqlite_pages


class SQLiteSource(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "snapshot.db"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE docs(id TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO docs VALUES (?)", [("a",), ("b",)])
            db.commit()

    async def test_exact_page_boundary_and_eof(self):
        pages = [page async for page in sqlite_pages(
            self.path, "SELECT id FROM docs ORDER BY id", width=1)]
        self.assertEqual([(list(p.ids), p.done) for p in pages],
                         [(["a"], False), (["b"], False), ([], True)])

    async def test_query_error_propagates(self):
        with self.assertRaises(sqlite3.OperationalError):
            await anext(sqlite_pages(self.path, "SELECT id FROM missing"))

    async def test_cancel_running_sql_before_first_page(self):
        stats = QueryStats()
        sql = """WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n
                 WHERE x < 100000000) SELECT CAST(sum(x) AS TEXT) FROM n"""
        pages = sqlite_pages(self.path, sql, stats=stats)
        pull = asyncio.create_task(anext(pages))
        try:
            async def started():
                while not stats.progress_calls:
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(started(), timeout=5)
        finally:
            pull.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(pull, timeout=5)
        self.assertTrue(stats.stopped_in_sql)
        self.assertEqual(stats.rows, 0)
        await pages.aclose()
