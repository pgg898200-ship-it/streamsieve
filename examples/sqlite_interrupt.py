"""Pull ranked IDs from a read-only SQLite snapshot without blocking asyncio.

Pass a database file that remains unchanged for the query, a parameterized
SELECT with a stable total order and a unique string ID in column zero.
Use one generator per ranker. Give fuse_partitioned the same group for every
ranker that can return the same ID. Requires Python 3.10+.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from threading import Event

from rankfence import Page


@dataclass
class QueryStats:
    progress_calls: int = 0
    stopped_in_sql: bool = False
    rows: int = 0


async def sqlite_pages(path, sql, params=(), *, width=16, stats=None,
                       interrupt=True, progress_ops=256):
    """Close and join the dedicated worker even if its first pull is cancelled.

    Set interrupt=False for the matched experimental control. It still closes
    the cursor, but lets an already running SQL step finish first.
    """
    with ThreadPoolExecutor(max_workers=1) as worker:
        async with aclosing(_pages(path, sql, params, width, stats, interrupt, worker, progress_ops)) as pages:
            async for page in pages:
                yield page


@asynccontextmanager
async def sqlite_shard_pages(path, queries, *, width=16, stats=None, progress_ops=256):
    """Share one query-scoped worker across rankers reading an immutable shard.

    queries is a sequence of (ordered SELECT, parameters) pairs. Each reader
    has its own read-only connection, cursor and cancellation signal. SQL work
    on this shard runs serially. Other shards can run at the same time.
    Consume the returned readers inside the async with block. progress_ops is
    the approximate VM instruction interval between cancellation checks.
    """
    queries = tuple(queries)
    metrics = [None] * len(queries) if stats is None else tuple(stats)
    if len(metrics) != len(queries):
        raise ValueError("supply one QueryStats per query")
    with ThreadPoolExecutor(max_workers=1) as worker:
        async with AsyncExitStack() as stack:
            readers = []
            for (sql, params), metric in zip(queries, metrics):
                reader = _pages(path, sql, params, width, metric, True, worker, progress_ops)
                readers.append(await stack.enter_async_context(aclosing(reader)))
            yield readers


async def _pages(path, sql, params, width, stats, interrupt, worker, progress_ops):
    if type(progress_ops) is not int or not 1 <= progress_ops <= 2**31 - 1:
        raise ValueError("progress_ops must be a positive signed 32-bit integer")
    if type(width) is not int or width < 1:
        raise ValueError("width must be a positive integer")
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    stopped = Event()
    stats = QueryStats() if stats is None else stats
    db = cursor = None

    def progress():
        stats.progress_calls += 1
        return interrupt and stopped.is_set()

    def pull():
        nonlocal db, cursor
        try:
            if db is None:
                db = sqlite3.connect(uri, uri=True)
                db.set_progress_handler(progress, progress_ops)
                cursor = db.execute(sql, params)
            rows = cursor.fetchmany(width)
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) == 9 or str(error) == "interrupted":
                stats.stopped_in_sql = True
            raise
        stats.rows += len(rows)
        return Page([row[0] for row in rows], done=len(rows) < width)

    def close():
        if db is not None:
            db.close()

    loop = asyncio.get_running_loop()
    try:
        while True:
            page = await loop.run_in_executor(worker, pull)
            yield page
            if page.done:
                break
    finally:
        stopped.set()
        await loop.run_in_executor(worker, close)
