"""Fetch/check the pinned Borg machine trace and emit normalized input for replay.cpp."""
import argparse
import csv
import gzip
import hashlib
import io
import sys
import urllib.request
from decimal import Decimal
from pathlib import Path

ARCHIVE = "part-00000-of-00001.csv.gz"
SHA256 = "fc44b8d2b33a96a261382488789855be7a0ce338888c99930ff45cb592249664"
PRIMARY = "https://storage.googleapis.com/clusterdata-2011-2/machine_events/" + ARCHIVE
MIRROR = ("https://raw.githubusercontent.com/mCiccone23/spark-project/"
          "0e6c5f36f33921240e8d3870cd98b4bce21ad212/machine_events/" + ARCHIVE)


def fetch():
    failures = []
    for url in (PRIMARY, MIRROR):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read(400_000)
            if hashlib.sha256(data).hexdigest() == SHA256:
                return data
            failures.append(f"hash mismatch for {url}")
        except OSError as exc:
            failures.append(str(exc))
    raise SystemExit("; ".join(failures))


def capacity(field):
    if not field:
        return 0xFFFFFFFF
    n = Decimal(field) * 100_000
    if n != int(n) or not 0 <= n < 0xFFFFFFFF:
        raise ValueError("capacity cannot be packed exactly")
    return int(n)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, nargs="?", help="existing .csv.gz (optional)")
    args = parser.parse_args()
    data = args.archive.read_bytes() if args.archive else fetch()
    if hashlib.sha256(data).hexdigest() != SHA256:
        sys.exit("unexpected input hash")
    keys = {}
    records = []
    previous = 0
    with gzip.open(io.BytesIO(data), "rt", newline="") as source:
        for timestamp, machine, event, _platform, cpu, memory in csv.reader(source):
            time = int(timestamp)
            if time < previous or event not in ("0", "1", "2"):
                raise ValueError("unexpected event order or type")
            previous = time
            key = keys.setdefault(machine, len(keys))
            deleted = int(event == "1")
            value = 0 if deleted else (capacity(cpu) << 32) | capacity(memory)
            records.append((time, key, value, deleted))
    print(len(keys), len(records))
    for record in records:
        print(*record)


if __name__ == "__main__":
    main()
