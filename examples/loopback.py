"""Replay paged retrievers over TCP loopback and compare exact top-k coordinators.

A request returns a page of IDs after optional user-specified service delay.
Reported request and byte counts cover client writes and complete client reads.
Latency includes connection setup, reads, parsing, fusion and client cleanup.
One-shot retrieval is included for sources that can return their full window.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import aclosing
import hashlib
import json
import math
from pathlib import Path
import platform
import random
from statistics import median
from time import perf_counter_ns
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import integer_batch, load_trace
from rankfence import Page, fuse


async def benchmark(args):
    available = list(load_trace(args.trace, None))
    n = min(args.queries, len(available))
    rows = [available[i * len(available) // n] for i in range(n)]
    handlers = set()
    failures = []

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            while request := await reader.readline():
                query, source, start, size = map(int, request.split())
                ranking = rows[query][2][source]
                items = ranking[start:start + size]
                delay = args.page_ms / 1000 + args.item_us * len(items) / 1e6
                if delay:
                    await asyncio.sleep(delay)
                response = [start + size >= len(ranking), items]
                writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass  # a certified winner may cancel a source with a page in flight
        except Exception as exc:
            failures.append(exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            handlers.discard(asyncio.current_task())

    async def source(port, q, s, page, counters):
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=32 * 1024 * 1024)
        offset = 0
        try:
            while True:
                request = f"{q} {s} {offset} {page}\n".encode()
                writer.write(request)
                await writer.drain()
                counters["requests_sent"] += 1
                counters["request_bytes"] += len(request)
                line = await reader.readline()
                if not line:
                    raise RuntimeError("server closed before final page")
                counters["response_bytes_read"] += len(line)
                done, ids = json.loads(line)
                offset += len(ids)
                counters["ids_received"] += len(ids)
                yield Page(ids, done)
                if done:
                    return
        finally:
            writer.close()
            await writer.wait_closed()

    async def exhaust(reader):
        async with aclosing(reader):
            return [doc async for page in reader for doc in page.ids]

    rng = random.Random(args.seed)
    results = []
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        for q, (_, topic, lists) in enumerate(rows):
            expected, _ = integer_batch(lists, args.k, args.c, args.page)
            for repeat in range(args.repeats + 1):
                methods = ["rankfence", "paged_batch", "oneshot_batch"]
                rng.shuffle(methods)
                for name in methods:
                    size = max(1, max(map(len, lists))) if name == "oneshot_batch" else args.page
                    counters = Counter()
                    start = perf_counter_ns()
                    readers = [source(port, q, i, size, counters) for i in range(len(lists))]
                    if name == "rankfence":
                        actual = (await fuse(readers, args.k, c=args.c)).ids
                    else:
                        all_items = await asyncio.gather(*(exhaust(r) for r in readers))
                        actual, _ = integer_batch(all_items, args.k, args.c, args.page)
                    elapsed = (perf_counter_ns() - start) / 1e6
                    if actual != expected:
                        raise AssertionError((topic, name, actual, expected))
                    if repeat:
                        results.append({"topic": topic, "repeat": repeat, "method": name,
                                        "ms": round(elapsed, 4), **counters})
                    if handlers:
                        await asyncio.gather(*tuple(handlers))
                    if failures:
                        raise RuntimeError("producer failed") from failures[0]
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--json", required=True)
    for key, value in [("queries", 12), ("repeats", 3), ("page", 32), ("k", 5),
                       ("c", 60), ("seed", 124)]:
        parser.add_argument("--" + key, type=int, default=value)
    parser.add_argument("--page-ms", type=float, default=0.0,
                        help="simulated per-request producer delay in milliseconds")
    parser.add_argument("--item-us", type=float, default=0.0,
                        help="simulated per-item producer delay in microseconds")
    args = parser.parse_args()
    if min(args.queries, args.repeats, args.page, args.k) <= 0 or min(args.c, args.page_ms, args.item_us) < 0:
        parser.error("invalid sizes or negative cost")
    rows = asyncio.run(benchmark(args))
    summary = {}
    for name in ("rankfence", "paged_batch", "oneshot_batch"):
        group = [r for r in rows if r["method"] == name]
        topics = sorted({r["topic"] for r in group})
        samples = sorted(median(r["ms"] for r in group if r["topic"] == t) for t in topics)
        summary[name] = {
            "p50_topic_ms": round(median(samples), 4),
            "p95_topic_ms": round(samples[math.ceil(.95 * len(samples)) - 1], 4),
            **{"mean_" + key: round(sum(r[key] for r in group) / len(group), 2)
               for key in ("requests_sent", "response_bytes_read", "ids_received")}}
    script = Path(__file__)
    files = [script, ROOT / "bench.py", ROOT / "rankfence" / "live.py",
             ROOT / "rankfence" / "core.py", Path(args.trace)]
    result = {"schema": 1, "parameters": vars(args), "platform": platform.platform(),
              "python": platform.python_version(),
              "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
              "summary": summary, "observations": rows}
    Path(args.json).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
