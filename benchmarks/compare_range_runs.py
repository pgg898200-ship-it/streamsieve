"""Summarize a replication after checking its query coverage and winners."""
import argparse
import csv
from collections import defaultdict
from math import ceil, isfinite
from pathlib import Path
from statistics import median

REFERENCE = Path(__file__).resolve().parents[1] / "results/scifact-range-v1.tsv"


def read(path):
    rows = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            key = row["layout"], row["claim"], row["method"]
            ms = float(row["ms"])
            if key in rows or not isfinite(ms) or ms < 0:
                raise ValueError(f"duplicate observation or invalid timing at {key}")
            rows[key] = row
    return rows


def compare(path):
    reference, local = read(REFERENCE), read(path)
    if reference.keys() != local.keys():
        raise ValueError("query, layout or method coverage differs from the recorded run")
    times = defaultdict(list)
    skips = defaultdict(int)
    for key, row in local.items():
        for field in ("winner", "probed", "skipped", "cited"):
            if row[field] != reference[key][field]:
                raise ValueError(f"{field} disagrees at {key}")
        group = key[0], key[2]
        times[group].append(float(row["ms"]))
        skips[group] += int(row["skipped"])
    print("layout\tmethod\tqueries\tarchive_skips\tp50_ms\tp95_ms")
    for (layout, method), values in sorted(times.items()):
        values.sort()
        p95 = values[ceil(.95 * len(values))-1]
        print(f"{layout}\t{method}\t{len(values)}\t{skips[layout, method]}\t{median(values):.3f}\t{p95:.3f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local", type=Path)
    compare(parser.parse_args().local)
