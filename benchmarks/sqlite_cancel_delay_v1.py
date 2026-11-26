"""Measure cancellation through worker cleanup after SQL starts, with two check intervals."""
import asyncio
import csv
from pathlib import Path
import random
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.sqlite_interrupt import QueryStats, sqlite_shard_pages

SQL = """WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n
         WHERE x<100000000) SELECT CAST(sum(x) AS TEXT) FROM n"""


async def trial(path, interval):
    stats = QueryStats()
    async with sqlite_shard_pages(path, [(SQL, ())], stats=[stats],
                                  progress_ops=interval) as (source,):
        task = asyncio.create_task(anext(source))
        try:
            async def wait_for_sql():
                while stats.progress_calls < 2:
                    await asyncio.sleep(0.0001)
            await asyncio.wait_for(wait_for_sql(), timeout=5)
            await asyncio.sleep(0.001)
        finally:
            start = perf_counter_ns()
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.CancelledError:
                pass
    elapsed = (perf_counter_ns() - start) / 1e6
    if not stats.stopped_in_sql or stats.rows:
        raise AssertionError("expected SQLITE_INTERRUPT before any row")
    return round(elapsed, 4)


async def measure(path):
    rng = random.Random(2602)
    writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    writer.writerow(["pair", "order", "interval", "cancel_to_close_ms"])
    for pair in range(102):
        order = rng.sample([256, 4096], 2)
        for index, interval in enumerate(order):
            elapsed = await trial(path, interval)
            if pair >= 2:
                writer.writerow([pair - 2, index, interval, elapsed])


if __name__ == "__main__":
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.db"
        sqlite3.connect(path).close()
        asyncio.run(measure(path))
