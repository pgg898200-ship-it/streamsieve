"""Score SQLite point-membership changes against the published Porto labels."""
import argparse
import gzip
import json
import sqlite3
from pathlib import Path

POINT_QUERY = """
SELECT id FROM zones
WHERE xmin <= :x AND :x < xmax AND ymin <= :y AND :y < ymax
"""


def score(zones, moves, module):
    db = sqlite3.connect(":memory:")
    try:
        # module is selected from the two SQLite built-ins below.
        db.execute(f"CREATE VIRTUAL TABLE zones USING {module}(id,xmin,xmax,ymin,ymax)")
        db.executemany("INSERT INTO zones VALUES (?,?,?,?,?)", (
            (i, xmin, xmax, ymin, ymax)
            for i, (xmin, ymin, xmax, ymax) in enumerate(zones)
        ))
        failures = 0
        first_failure = None
        for move in moves:
            old, new = (
                {row[0] for row in db.execute(POINT_QUERY, {"x": move["x" + k], "y": move["y" + k]})}
                for k in ("0", "1")
            )
            entered, exited = sorted(new - old), sorted(old - new)
            if entered != move["entered"] or exited != move["exited"]:
                failures += 1
                if first_failure is None:
                    first_failure = {key: move[key] for key in ("trip", "step", "entered", "exited")}
                    first_failure.update(actual_entered=entered, actual_exited=exited)
        return {"matched": len(moves) - failures, "total": len(moves), "first_mismatch": first_failure}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, help="directory with zones.json and moves.jsonl.gz")
    args = parser.parse_args()
    zones = json.loads((args.data / "zones.json").read_bytes())
    with gzip.open(args.data / "moves.jsonl.gz", "rt", encoding="utf-8") as stream:
        moves = [json.loads(line) for line in stream]
    if not moves:
        parser.error("expected a nonempty labeled replay")
    for box in zones:
        if any(type(v) is not int or not -(2**31) <= v < 2**31 for v in box):
            parser.error("rtree_i32 requires signed 32-bit integer bounds")
    results = {module: score(zones, moves, module) for module in ("rtree", "rtree_i32")}
    print(json.dumps({"sqlite": sqlite3.sqlite_version, **results}, indent=2))
    return int(results["rtree_i32"]["matched"] != len(moves))


if __name__ == "__main__":
    raise SystemExit(main())
