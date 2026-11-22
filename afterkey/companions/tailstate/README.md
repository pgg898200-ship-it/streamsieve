# Tailstate

Tailstate is a small C99 index for reconnecting workers that need the current state of a fleet. Each worker supplies its last acknowledged cursor and gets the newest update or deletion for every key changed since then, in sequence order. Writers allocate once for a bounded universe of dense key IDs. The publisher stores zero per-subscriber queues.

A worker-capacity change from 8 → 7 → 6 while a scheduler is offline costs one delivered row. That substitution is valid for replacement state such as available slots, latest measurements and progress. A command, audit record, token stream or increment needs an event log.

[Interactive workbench and source download](https://pgg-tailstate.tancurl3.chatgpt.site)

## How do I resume a worker after it disconnects?

Install the source release with a C compiler and CPython development headers on a machine with Python 3.8+. The Python wrapper has no third-party runtime dependencies.

```sh
CC=cc python3 -m pip install https://pgg-tailstate.tancurl3.chatgpt.site/tailstate.tar.gz
```

```python
from tailstate import Store

with Store(1024, indexed=True) as fleet:
    fleet.put(17, 8)
    acknowledged = fleet.page(limit=16).cursor
    fleet.put(17, 7)
    fleet.put(17, 6)
    fleet.delete(42)
    page = fleet.page(acknowledged, limit=16)
    assert [(r.key, r.value, r.deleted) for r in page.changes] == [
        (17, 6, False), (42, 0, True)
    ]
```

Each Python cursor holds a publisher instance ID, sequence and optional key hint. Apply every row and persist the returned cursor in the same transaction. Each page observes the current state at read time. Another writer may supersede a row between pages. A new publisher instance requires a fresh replica and reloading the publisher from authoritative state. The SQLite example exercises worker restarts, failed commits in the tests, and publisher replacement.

```sh
make
make test
python3 examples/reconnect.py
make paged
```

C callers can vendor `include/tailstate.h` and `src/tailstate.c`, or link the static or shared library from `build/`. Serialize access to each store. Keep values inline as unsigned 64-bit words or manage stable, versioned payloads externally. The native API leaves publisher instance IDs to the caller.

## Which index should I choose?

| Workload | Starting point | Tradeoff |
| --- | --- | --- |
| Hot publishers, slow readers consuming all changed keys | `ts_create` or Python `Store(n)` | About 24.125 bytes per possible key on the tested ABI. A move-to-tail list makes complete reads proportional to distinct changed keys. An old arbitrary cursor can require a long seek. |
| Bounded pages with independently lagging readers | `ts_create_indexed` or `Store(n, indexed=True)` | Adds a recent-sequence ring and a compressed binary prefix tree over 64-write blocks. Ancient cursors still resolve after the ring expires. Up to about 52.125 bytes per key on the tested ABI. |
| Sparse, unbounded IDs or histories where all transitions matter | Ordered map, persistent change feed or append log | Tailstate reserves for the full key capacity, including deleted or untouched slots. It stores one current revision per slot. |

A surviving `(sequence, key)` hint can resume at its successor in constant time. Stale, missing and incorrect key hints return the exact same rows as a numeric seek. A write invalidates a C iterator. Fetch and copy a bounded page under the writer's lock, then release it before network I/O. Tombstones live until the same slot is updated or the store is destroyed. Shards need independent cursors and supply shard-local order.

## How fast is it?

We replayed all 37,780 machine events for 12,583 machines in Google's 2011 Borg trace. With hypothetical hourly polling, each exact implementation reduced 25,303 post-snapshot mutations to 17,270 delivered rows. The compact store processed the full replay in 0.606 ms median versus 6.023 ms for a node-reusing C++ ordered map on the same CPU. With daily polling it took 0.468 ms versus 2.150 ms. Parsing, allocation, waiting and transport are excluded as described in the replay protocol. Coalescing itself accounts for the row reduction in all three implementations. [Trace, oracle, commands and results](bench/real/README.md)

The synthetic page test returns 16 rows from 65,536 keys. With a recent overwritten anchor and a surviving successor, indexed Tailstate measured **170 ns** median versus **250 ns** for a hinted map. With the next four revisions also superseded, they measured **721 ns** and **231 ns**. Indexed Tailstate applied 16,384 rewrites in **0.443 ms** versus **1.593 ms**, using 3,416,128 versus 4,194,376 requested bytes. These warmed, single-host measurements include winning and losing cases. [Paged benchmark output](bench/pages-results.txt)

On the mixed-rate trace the compact store returned 64 ordered rows in **351 ns** p50 versus **36,840 ns** for an 8,192-event ring with dense fallback. A compacting bitmap read in **230 ns** and reserved 2,238,776 bytes versus Tailstate's 1,581,112. The bitmap reorganizes its index periodically on the write path. [All eight kernels, timing definitions and reproducible commands](bench/README.md)

```sh
make bench
make pages
make fleet
```

The fleet test runs one million dense keys and 10,000 logical workers in one process. Querying many small pages can favor an ordered map. The included [fleet results](bench/fleet.md) report reads, writes, and combined time. None of these timings include live network delivery.

## Related systems

Apache CouchDB’s changes feed and Replicache's global version strategy already implement variants of latest-state synchronization. Redis sorted sets support range seeks by score. Tailstate specializes in a bounded, in-process, single-writer index for an application that already owns its authoritative state. The release benchmarks in-process kernels. It makes no measured Redis or database-server latency claim. [CouchDB changes feed](https://docs.couchdb.org/en/stable/api/database/changes.html) · [Replicache global version strategy](https://doc.replicache.dev/strategies/global-version) · [Redis sorted sets](https://redis.io/docs/latest/develop/data-types/sorted-sets/)

MIT-licensed source. To report a reproduction, include the source revision, machine, compiler, key count, write distribution, page length, cursor ages and the comparator. Those details let another team test the same claim.
