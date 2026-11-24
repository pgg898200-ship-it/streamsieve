"""Regenerate the offline GEFS intensity replays from the NOAA/NHC ATCF archives."""

import argparse
import csv
import gzip
import hashlib
import io
from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.request import urlopen

ROOT = "https://ftp.nhc.noaa.gov/atcf/archive/"
STORMS = {
    "lee": ("al132023", "2023090612", "2023091612"),
    "milton": ("al142024", "2024100512", "2024101112"),
}
# Pin uncompressed data. NOAA can recompress an unchanged archive.
DECKS = {
    "aal142024": "ff22195e57856976f6ccb7bff7971f78ce0252fd14ee13fa37d177aa9dc7ce41",
    "bal142024": "2e0ae27ed0a7594c5545ec4e94257a3ddf185c28f343117f0f6b055d19e71285",
    "aal132023": "0ed6ffa50154f6055f8a3cba4db475e3b4a42e99e61080c9b811809204556cbe",
    "bal132023": "ca9ae215e737d32cd13300fbeab900d75ab84c6d915f51f87e236c3cc46ee958",
}
MEMBERS = ["AC00"] + [f"AP{i:02d}" for i in range(1, 31)]


def rows(deck):
    with urlopen(f"{ROOT}{deck[-4:]}/{deck}.dat.gz", timeout=90) as response:
        data = gzip.decompress(response.read())
    if hashlib.sha256(data).hexdigest() != DECKS[deck]:
        raise ValueError(f"{deck} differs from the archived input")
    return csv.reader(io.StringIO(data.decode("ascii")), skipinitialspace=True)


def unique_value(dictionary, key, value):
    if dictionary.setdefault(key, value) != value:
        raise ValueError(f"inconsistent VMAX for {key}")


@lru_cache(maxsize=None)
def valid_time(init, hours):
    return (datetime.strptime(init, "%Y%m%d%H") + timedelta(hours=hours)).strftime("%Y%m%d%H")


def extract(a_rows, b_rows, first_target="2023090612", last_target="2023091612"):
    obs = {}
    for row in b_rows:
        if row[4] == "BEST" and not row[3].strip() and int(row[5]) == 0:
            unique_value(obs, row[2], int(row[8]))
    cycles = defaultdict(dict)
    for row in a_rows:
        member = row[4]
        if member not in MEMBERS:
            continue
        hours = int(row[5])
        if hours < 6 or hours > 72 or hours % 6:
            continue
        target = valid_time(row[2], hours)
        if not (first_target <= target <= last_target and target.endswith("12")):
            continue
        wind = int(row[8])
        if wind > 0:
            unique_value(cycles[target, row[2]], member, wind)
    for (target, init), winds in sorted(cycles.items()):
        if len(winds) == len(MEMBERS):
            yield [target, init, obs[target], *(winds[m] for m in MEMBERS)]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storm", choices=STORMS, default="lee")
    storm = parser.parse_args().storm
    deck, first, last = STORMS[storm]
    result = list(extract(rows("a" + deck), rows("b" + deck), first, last))
    destination = Path(__file__).with_name(f"{storm}_gefs.csv")
    with destination.open("w", encoding="ascii", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["target_utc", "init_utc", "observed_kt", *MEMBERS])
        writer.writerows(result)
    print(f"{len(result)} complete cycles, SHA-256 {hashlib.sha256(destination.read_bytes()).hexdigest()}")
