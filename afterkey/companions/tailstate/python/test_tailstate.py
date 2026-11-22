import copy
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from tailstate import Cursor, EpochMismatch, Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from reconnect import connect_worker, snapshot, sync_page


class TailstateTests(unittest.TestCase):
    def test_coalescing_with_stale_hint_and_tombstones(self):
        with Store(3) as s:
            self.assertIsNone(s.get(0))
            s.put(0, 8)
            s.put(1, 9)
            first = s.page(limit=1)
            s.put(0, (1 << 64) - 1)
            s.delete(1)
            s.delete(2)
            second = s.page(first.cursor)
            self.assertEqual([r.key for r in second.changes], [0, 1, 2])
            self.assertEqual(s.get(0).value, (1 << 64) - 1)
            self.assertTrue(s.get(1).deleted)
            self.assertEqual(s.page(second.cursor).changes, ())
            self.assertEqual(second.cursor.seq, s.head)

    def test_indexed_numeric_and_overwritten_cursor(self):
        with Store(300, indexed=True) as s:
            for key in range(300):
                s.put(key, key)
            cursor = s.page(Cursor(s.epoch, 100), 1).cursor
            self.assertEqual((cursor.seq, cursor.key), (101, 100))
            s.delete(100)
            result = s.page(cursor, 300)
            self.assertEqual([r.key for r in result.changes], list(range(101, 300)) + [100])
            self.assertTrue(result.changes[-1].deleted)

    def test_bounds_epoch_lifetime(self):
        with Store(2) as s, Store(2) as other:
            for key in (-1, 2, 1 << 32):
                with self.assertRaises(ValueError):
                    s.put(key, 42)
            for value in (-1, 1 << 64):
                with self.assertRaises(ValueError):
                    s.put(0, value)
            with self.assertRaises(ValueError):
                s.page(limit=0)
            with self.assertRaises(ValueError):
                s.page(Cursor(s.epoch, 1))
            with self.assertRaises(EpochMismatch):
                other.page(s.page().cursor)
            s.close()
            with self.assertRaises(RuntimeError):
                s.page()
            with self.assertRaises(TypeError):
                copy.copy(s)

    def test_transaction_rollback_worker_reconnect_and_epoch_reset(self):
        with tempfile.TemporaryDirectory() as temp, Store(3) as publisher:
            path = Path(temp) / "replica.sqlite"
            publisher.put(0, 9)
            with closing(connect_worker(path)) as db:
                self.assertEqual(sync_page(db, publisher, 1), 1)
                publisher.put(1, 4)
                db.execute("""CREATE TEMP TRIGGER fail BEFORE INSERT ON progress
                              BEGIN SELECT RAISE(ABORT, 'simulated error'); END""")
                with self.assertRaises(sqlite3.IntegrityError):
                    sync_page(db, publisher, 2)
                self.assertEqual(snapshot(db), {0: 9})
            with closing(connect_worker(path)) as db:
                self.assertEqual(sync_page(db, publisher, 2), 1)
                self.assertEqual(snapshot(db), {0: 9, 1: 4})
                with Store(3) as restarted:
                    restarted.put(2, 13)
                    restarted.put(2, 37)  # Equal numeric heads, different epochs.
                    self.assertEqual(sync_page(db, restarted, 2), 1)
                    self.assertEqual(snapshot(db), {2: 37})


if __name__ == "__main__":
    unittest.main()
