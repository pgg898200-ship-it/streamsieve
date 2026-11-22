"""Count unique timeout and recovery transitions observed by overlapping collectors."""

import argparse
import json
import sys
from contextlib import nullcontext

from streamsieve import EvidenceLattice


def ingest(lattice, lines, source, agent_id, tick_ns):
    """Add one collector's Pulsefold transitions to a StreamSieve evidence lattice."""
    if not source or tick_ns <= 0:
        raise ValueError("use a nonempty source and positive nanoseconds per tick")
    rows = 0
    for line_no, line in enumerate(lines, 1):
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 4 or fields[3] not in ("stale", "fresh"):
            raise ValueError(f"line {line_no}: expected TIME FEED SUBSCRIBER stale|fresh")
        if any(not s.isascii() or not s.isdecimal() for s in fields[:3]):
            raise ValueError(f"line {line_no}: expected unsigned integer fields")
        tick, feed, subscriber = map(int, fields[:3])
        timestamp_ns = tick * tick_ns
        if timestamp_ns >= 2**64 or max(feed, subscriber) >= 2**32:
            raise ValueError(f"line {line_no}: timestamp or identifier out of range")
        state = fields[3]
        # Canonical JSON handles separators in user-provided namespaces. The
        # collector is deliberately absent so independently seen copies merge.
        identity = json.dumps(["pulsefold/v1", source, timestamp_ns, feed, subscriber, state],
                              separators=(",", ":"), ensure_ascii=True)
        lattice.observe(f"pulsefold/{state}", EvidenceLattice.hash_id(identity),
                        llr=0.0, log_e=0.0, agent_id=agent_id, timestamp_ns=timestamp_ns)
        rows += 1
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="shared epoch, feed map and TTL-policy identity")
    parser.add_argument("--tick-ns", type=int, required=True, help="nanoseconds per input tick")
    parser.add_argument("paths", nargs="*", help="one scalar transition log per collector; default stdin")
    args = parser.parse_args()
    inputs = args.paths or ["-"]
    if inputs.count("-") > 1:
        parser.error("stdin can identify only one collector")
    rows = 0
    with EvidenceLattice() as merged:
        for agent, name in enumerate(inputs):
            try:
                stream = nullcontext(sys.stdin) if name == "-" else open(name, encoding="utf-8")
                with stream as lines, EvidenceLattice() as replica:
                    rows += ingest(replica, lines, args.source, agent, args.tick_ns)
                    merged.merge(replica)
            except (OSError, ValueError) as exc:
                parser.exit(1, f"{name}: {exc}\n")
        unique = {state: merged.query(f"pulsefold/{state}").unique_evidence
                  for state in ("stale", "fresh")}
    print(json.dumps({"input_rows": rows, "unique": unique,
                      "duplicate_rows": rows - sum(unique.values())}))


if __name__ == "__main__":
    main()
