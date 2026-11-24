"""Pull ranked IDs from a read-only SQLite snapshot without blocking asyncio.

Pass a database file that remains unchanged for the query, a parameterized
SELECT with a stable total order and a unique string ID in column zero.
Use one generator per ranker. Give fuse_partitioned the same group for every
ranker that can return the same ID. Requires Python 3.10+.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
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
                       interrupt=True):
    """Close and join the dedicated worker even if its first pull is cancelled.

    Set interrupt=False for the matched experimental control. It still closes
    the cursor, but lets an already running SQL step finish first.
    """
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
                db.set_progress_handler(progress, 256)
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
    with ThreadPoolExecutor(max_workers=1) as worker:
        try:
            while True:
                page = await loop.run_in_executor(worker, pull)
                yield page
                if page.done:
                    break
        finally:
            stopped.set()
            await loop.run_in_executor(worker, close)
