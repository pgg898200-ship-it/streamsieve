"""Reproduce the pinned four-hour Intel Lab trace with the Python standard library."""

import argparse
import csv
import hashlib
import io
import sys
from datetime import time
from pathlib import Path
from urllib.request import urlopen


BASE = (
    "https://raw.githubusercontent.com/AhmedCh24/data_compression_bitteiler_technical_task/"
    "0683fb4e9190f1b89148a3e4fa41c8145f0c4694/data/data_by_sensor_node/csv/"
)
EXPECTED = "33c1be134f2b2f3170ae369a5e34db89ab29ef435951aad334fccc13cccb10a0"


def get_rows(stream, mote):
    for row in csv.DictReader(stream):
        if row["date"] != "2004-02-28":
            continue
        clock = time.fromisoformat(row["time"])
        if clock.hour >= 4:
            continue
        if int(row["moteid"]) != mote:
            raise ValueError(f"unexpected mote ID in source file for mote {mote}")
        seconds = (clock.hour * 60 + clock.minute) * 60 + clock.second
        yield seconds * 1_000_000 + clock.microsecond, mote - 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", type=Path, help="local directory of pinned mote_N.csv files")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("intel-lab-4h.tsv"))
    args = parser.parse_args()
    events = []
    for mote in range(1, 55):
        filename = f"mote_{mote}.csv"
        if args.csv_dir:
            stream = (args.csv_dir / filename).open(encoding="ascii", newline="")
        else:
            stream = io.TextIOWrapper(urlopen(BASE + filename, timeout=60), encoding="ascii", newline="")
        with stream:
            events.extend(get_rows(stream, mote))
    payload = "".join(f"{t}\t{feed}\n" for t, feed in sorted(events)).encode("ascii")
    if hashlib.sha256(payload).hexdigest() != EXPECTED:
        raise ValueError("trace checksum differs from the release fixture")
    args.output.write_bytes(payload)
    print(f"{len(events)} observations across {len({f for _, f in events})} feeds", file=sys.stderr)


if __name__ == "__main__":
    main()
