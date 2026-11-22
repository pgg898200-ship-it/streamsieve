"""Resume paged reads of a native Afterkey/StreamSieve score feed.

From the repository root (C++20 and a C compiler required):

    c++ -O2 -std=c++20 -Icsrc -Iafterkey/include afterkey/examples/streamsieve.cpp csrc/streamsieve_capi.cpp -o /tmp/streamsieve-afterkey
    python3 -m pip install https://pgg-tailstate.tancurl3.chatgpt.site/tailstate.tar.gz
    /tmp/streamsieve-afterkey --score-feed | python3 examples/tailstate_scores.py

Afterkey emits complete change batches for the native producer. The Python
bridge holds absolute scores, and serves pages of two changes to a worker
backed by SQLite. The worker closes after its first page, then reopens after
several producer checkpoints. Its scores and cursor commit in one transaction.
Both libraries are maintained by PGG. Use an event log if each transient score
or alert must be observed.
"""
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from tailstate import Cursor, Store


def apply_page(db, publisher):
    """Commit the page's effects and its acknowledgement together."""
    saved = db.execute("SELECT epoch, seq, key FROM ack").fetchone()
    with db:
        cursor = None
        if saved and saved[0] == publisher.epoch:
            cursor = Cursor(saved[0], int(saved[1]), saved[2])
        elif saved:
            db.execute("DELETE FROM scores")
        page = publisher.page(cursor, limit=2)
        for row in page.changes:
            if row.deleted:
                db.execute("DELETE FROM scores WHERE key = ?", (row.key,))
            else:
                db.execute("INSERT OR REPLACE INTO scores VALUES (?, ?)",
                           (row.key, row.value.to_bytes(8, "big")))
        db.execute("DELETE FROM ack")
        db.execute("INSERT INTO ack VALUES (?, ?, ?)",
                   (page.cursor.epoch, str(page.cursor.seq), page.cursor.key))
    return len(page.changes)


def replay(lines, path):
    label, count = next(lines).split()
    if label != "channels":
        raise ValueError("expected channel count at start of score feed")
    with Store(int(count), indexed=True) as store:
        checkpoints = 0
        expected = {}
        for line in lines:
            op, *fields = line.split()
            if op == "put":
                key, value = map(int, fields)
                store.put(key, value)
            elif op == "checkpoint":
                checkpoints += 1
                if checkpoints == 1:
                    db = sqlite3.connect(path)
                    try:
                        db.execute("CREATE TABLE scores(key INTEGER PRIMARY KEY, bits BLOB)")
                        db.execute("CREATE TABLE ack(epoch TEXT, seq TEXT, key INTEGER)")
                        acknowledged = apply_page(db, store)
                    finally:
                        db.close()
            elif op == "expect":
                key, value = map(int, fields)
                expected[key] = value
            else:
                raise ValueError(f"unrecognized score feed record {op!r}")

        if checkpoints < 2 or len(expected) != int(count):
            raise ValueError("incomplete StreamSieve score feed")
        db = sqlite3.connect(path)
        try:
            received = 0
            while True:
                size = apply_page(db, store)
                if size == 0:
                    break
                received += size
            actual = {key: int.from_bytes(bits, "big") for key, bits in
                      db.execute("SELECT key, bits FROM scores")}
            cursor = db.execute("SELECT seq FROM ack").fetchone()[0]
            if actual != expected or int(cursor) != store.head:
                raise RuntimeError("resumed replica differs from native detector")
        finally:
            db.close()
        return {"afterkey_checkpoints": checkpoints, "bridge_updates": store.head,
                "initial_page_rows": acknowledged, "reconnect_rows": received,
                "verified_channels": len(actual)}


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(json.dumps(replay(iter(sys.stdin), Path(directory) / "worker.sqlite")))
