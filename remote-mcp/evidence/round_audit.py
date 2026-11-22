"""Witness the minimum synchronous rounds on the hashed Robust03 replay.

python round_audit.py --repo path/to/rankfence --trace /tmp/robust03.json --json round_audit.json
Generate the input with the release's benchmarks/fetch_replay.py robust03 /tmp/robust03.json.
"""
import argparse
from collections import Counter, defaultdict
from fractions import Fraction
from functools import lru_cache
import hashlib
import heapq
import json
from pathlib import Path
import sys
import zlib

PAGE, SHARDS, K, C = 16, 8, 5, 60
INPUT_SHA = "0c4b02522c49436f2ac3eca6c44c6853b0f6acad217ee62e631564297e17c3ea"


def encoded_hash(value):
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def shard(doc):
    return zlib.crc32(doc.encode()) % SHARDS


def partition(lists):
    by_shard = [[[] for _ in lists] for _ in range(SHARDS)]
    for ranker, docs in enumerate(lists):
        for doc in docs:
            group = shard(doc)
            by_shard[group][ranker].append(f"{group}/{doc}")
    return [docs for group in by_shard for docs in group]


@lru_cache(maxsize=None)
def discount(rank):
    return Fraction(1, C + rank)


def score(lists):
    scores = defaultdict(Fraction)
    for docs in lists:
        for rank, doc in enumerate(docs, 1):
            scores[doc] += discount(rank)
    return scores


def top(scores):
    return tuple(heapq.nsmallest(K, scores, key=lambda doc: (-scores[doc], doc)))


def full_prefix(lists, nrounds):
    depth = nrounds * PAGE
    return [s[:depth] for s in lists], [nrounds > 0 and len(s) <= depth for s in lists]


def observed_stop(lists, groups, PartitionFence, retirable_sources, verify):
    state = PartitionFence(groups, K, C)
    retired = set()
    accepted = [[] for _ in lists]
    for round_id in range(1, max(1, (max(map(len, lists)) + PAGE - 1) // PAGE) + 1):
        for i, seq in enumerate(lists):
            if state.done[i] or i in retired:
                continue
            start = (round_id - 1) * PAGE
            page = seq[start:start + PAGE]
            state.push(i, page, done=start + PAGE >= len(seq))
            accepted[i].extend(page)
        output = state.ordered()
        if output is not None:
            receipt = {"format": "rankfence-partition/1", "k": K, "c": C, "mode": "ordered",
                       "prefixes": accepted, "source_groups": groups, "done": list(state.done),
                       "ids": list(output)}
            if not verify(receipt, source_groups=groups):
                raise AssertionError("stopping receipt failed")
            if top(score(lists)) != tuple(output):
                raise AssertionError("full rational ranking disagrees with scheduler")
            return round_id, encoded_hash(receipt), sum(map(len, accepted))
        retired.update(retirable_sources(state))
    raise AssertionError("no stop")


def fresh(group, label, observed):
    # All invented IDs are legal under the actual CRC32 mapping. The labels
    # differ by role/source, so every neutral ending is unique.
    n = 0
    while True:
        raw = f"!round-audit-{label}-{n}"
        candidate = f"{group}/{raw}"
        if shard(raw) == int(group) and candidate not in observed:
            return candidate
        n += 1


def validated_order(prefixes, closed, groups, endings):
    # Independent check of *both actual completed worlds*, including legality.
    completed = []
    for prefix, suffix, eof, group in zip(prefixes, endings, closed, groups, strict=True):
        if (eof and suffix) or (not eof and len(suffix) != 1):
            raise AssertionError("changed EOF status from the common history")
        docs = prefix + list(suffix)
        if len(set(docs)) != len(docs):
            raise AssertionError("duplicate in completion")
        if any(not d.startswith(group + "/") or str(shard(d.split("/", 1)[1])) != group for d in docs):
            raise AssertionError("wrong namespace")
        completed.append(docs)
    return top(score(completed))


def construct(prefixes, closed, groups):
    lower = score(prefixes)
    sets = [set(s) for s in prefixes]
    neutral = [() if eof else (fresh(g, f"filler-{i}", lower),)
               for i, (g, eof) in enumerate(zip(groups, closed))]
    base = validated_order(prefixes, closed, groups, neutral)
    # For each known ID, or a fresh matching ID per group, compute its attainable
    # upper score by giving it next rank in every group-local source that can
    # still contain it. No other observed score changes in the second world.
    group_open = defaultdict(list)
    for i, g in enumerate(groups):
        if not closed[i]:
            group_open[g].append(i)
    fresh_ids = [fresh(group, "challenger", lower) for group in group_open]
    upper = {}
    for doc in list(lower) + fresh_ids:
        g = doc.split("/", 1)[0]
        upper[doc] = lower.get(doc, 0) + sum(discount(len(prefixes[i]) + 1)
                                              for i in group_open[g] if doc not in sets[i])
    # Try the strongest outsider, then every current leader. If no witness
    # appears, report it explicitly; absence of this search result proves nothing.
    strongest = min((d for d in upper if d not in base),
                    key=lambda d: (-upper[d], d), default=None)
    candidates = [d for d in [strongest, *reversed(base)] if d is not None and d in upper]
    for doc in candidates:
        group = doc.split("/", 1)[0]
        positions = [i for i in group_open[group] if doc not in sets[i]]
        if not positions:
            continue
        other = list(neutral)
        for i in positions:
            other[i] = (doc,)
        outcome = validated_order(prefixes, closed, groups, other)
        if outcome != base:
            return {"kind": "observed" if doc in lower else "new",
                    "group": group, "changed_sources": positions,
                    "challenger_sha256": encoded_hash(doc),
                    "completion_a_answer_sha256": encoded_hash(base),
                    "completion_b_answer_sha256": encoded_hash(outcome),
                    "completion_a_suffix_sha256": encoded_hash(neutral),
                    "completion_b_suffix_sha256": encoded_hash(other)}
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo.resolve()))
    from rankfence import PartitionFence, verify
    from rankfence.retire import retirable_sources
    raw = args.trace.read_bytes()
    if hashlib.sha256(raw).hexdigest() != INPUT_SHA:
        raise ValueError("expected the release's pinned Robust03 replay")
    trace = json.loads(raw)
    rows = []
    for query in trace["queries"]:
        lists = partition(query["lists"])
        groups = [str(g) for g in range(SHARDS) for _ in query["lists"]]
        stop, receipt_hash, read = observed_stop(lists, groups, PartitionFence, retirable_sources, verify)
        prefixes, eof = full_prefix(lists, stop - 1)
        witness = construct(prefixes, eof, groups)
        rows.append({"topic": query["id"], "observed_stop_round": stop, "entries_read": read,
                     "verified_minimum_round": stop if witness else None,
                     "verified_receipt_sha256": receipt_hash,
                     "full_previous_history_sha256": encoded_hash([prefixes, eof]),
                     "witness": witness})
    if len(rows) != 100:
        raise AssertionError("incomplete trace")
    successes = [row for row in rows if row["witness"] is not None]
    summary = {"queries": len(rows), "verified_minimum_round": len(successes),
               "no_witness": [r["topic"] for r in rows if not r["witness"]],
               "entries_read": sum(row["entries_read"] for row in rows),
               "witness_kinds": dict(Counter(r["witness"]["kind"] for r in successes)),
               "stop_round_histogram": dict(sorted(Counter(r["observed_stop_round"] for r in rows).items()))}
    files = [Path(__file__), *(args.repo / "rankfence" / s for s in ("core.py", "partition.py", "retire.py", "receipt.py"))]
    report = {"format": "rankfence-round-audit/1", "replay_sha256": INPUT_SHA,
              "source_run_revision": "https://github.com/joaopalotti/trectools/tree/8a896def007e3d657eb29f820ee3de98e2f32691/examples/robust03",
              "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
              "config": {"page": PAGE, "shards": SHARDS, "rankers_per_shard": 4, "k": K, "c": C,
                         "scope": "one stable page per source per round; shard-local ranks, EOF on last page; no lookups or side information"},
              "summary": summary, "queries": rows}
    args.json.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
