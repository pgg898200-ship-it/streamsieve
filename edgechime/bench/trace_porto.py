"""Regenerate the committed Porto trajectory sample from the original UCI CSV."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import itertools
import json
from pathlib import Path
from urllib.request import urlopen

SOURCE = (
    "https://archive.ics.uci.edu/ml/machine-learning-databases/"
    "00339/Porto_taxi_data_test_partial_trajectories.csv"
)
SOURCE_SHA256 = "f376cca6839ec7a16c46d8619b28230114b9bba5e9b0dec7e4abae44e82b542f"
OUTPUT = Path(__file__).resolve().parent / "data" / "trace.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="use a locally downloaded UCI CSV")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if args.input:
        data = args.input.read_bytes()
    else:
        with urlopen(SOURCE, timeout=45) as response:
            data = response.read()
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA256:
        parser.error("input differs from the documented UCI source digest")

    rows = csv.DictReader(io.StringIO(data.decode("utf-8")))
    tracks = []
    for row in itertools.islice(rows, 100):
        if row["MISSING_DATA"].lower() != "false":
            continue
        locations = json.loads(row["POLYLINE"])
        if len(locations) < 2:
            continue
        track = [[round(lon * 1_000_000), round(lat * 1_000_000)] for lon, lat in locations]
        tracks.append(json.dumps(track, separators=(",", ":")))

    args.output.write_bytes(("[\n" + ",\n".join(tracks) + "\n]\n").encode("ascii"))


if __name__ == "__main__":
    main()
