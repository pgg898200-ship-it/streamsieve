"""Reconnect a worker using a transactional SQLite replica and cursor.

Run from the repository root with: python3 examples/reconnect.py
This demo uses one SQLite writer per worker. The publisher keeps state in RAM.
A new publisher creates a new epoch and must reload its own authoritative state.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from tailstate import Cursor, EpochMismatch, Store


def connect_worker(path):
    db = sqlite3.connect(path)
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS state (key INTEGER PRIMARY KEY, value TEXT)")
        # Decimal strings preserve unsigned 64-bit values beyond SQLite's integer range.
        db.execute("""CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY CHECK(id = 1), epoch TEXT, seq TEXT, key INTEGER
        )""")
    return db


def sync_page(db, publisher, limit):
    """Commit a page and checkpoint together, including any epoch reset."""
    with db:
        saved = db.execute("SELECT epoch, seq, key FROM progress WHERE id = 1").fetchone()
        cursor = Cursor(saved[0], int(saved[1]), saved[2]) if saved else None
        try:
            page = publisher.page(cursor, limit)
        except EpochMismatch:
            db.execute("DELETE FROM state")
            page = publisher.page(None, limit)
        for change in page.changes:
            if change.deleted:
                db.execute("DELETE FROM state WHERE key = ?", (change.key,))
            else:
                db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)",
                           (change.key, str(change.value)))
        db.execute("INSERT OR REPLACE INTO progress VALUES (1, ?, ?, ?)",
                   (page.cursor.epoch, str(page.cursor.seq), page.cursor.key))
    return len(page.changes)


def snapshot(db):
    return {key: int(value) for key, value in db.execute("SELECT key, value FROM state")}


def main():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "worker.sqlite"
        with Store(8) as publisher:
            publisher.put(0, 10)
            publisher.put(1, 20)
            with closing(connect_worker(path)) as worker:
                sync_page(worker, publisher, 1)
                print("Before disconnect:", snapshot(worker))

            publisher.put(1, 21)
            publisher.put(1, 22)
            publisher.delete(0)

            # Fresh SQLite connection represents the restarted worker.
            with closing(connect_worker(path)) as worker:
                rows = sync_page(worker, publisher, 8)
                assert snapshot(worker) == {1: 22}
                print(f"After reconnect ({rows} changes):", snapshot(worker))

        # Restarted publisher exposes a new authoritative state and epoch.
        with Store(8) as restarted, closing(connect_worker(path)) as worker:
            restarted.put(2, 30)
            sync_page(worker, restarted, 8)
            assert snapshot(worker) == {2: 30}
            print("After publisher restart:", snapshot(worker))


if __name__ == "__main__":
    main()
