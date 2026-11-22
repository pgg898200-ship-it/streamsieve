---
title: Afterkey
sdk: static
app_file: demo/index.html
license: apache-2.0
short_description: C++17 latest-state updates for lagging readers
tags:
- cpp
- multi-agent
- streaming
- benchmarks
---

# Afterkey

**Let a slow reader catch up with one value per changed key.** Afterkey is a C++17 header for agent dashboards, worker state and telemetry fan-out. A producer overwrites a fixed set of keys. Each reader keeps one 64-bit cursor. A reader that sleeps through a million updates to the same key receives one row when it returns. Deletions travel through the same feed.

[Try two independent readers in your browser](https://afterkey.tancurl3.chatgpt.site) · [Download the source kit](https://raw.githubusercontent.com/pgg898200-ship-it/streamsieve/main/afterkey/releases/afterkey-0.1.0.zip) · [Reproduce the benchmarks](bench/environment.md)

## Run in 30 seconds

Download the standalone kit and run the example with a C++17 compiler. If you cloned StreamSieve, start in its `afterkey/` directory and run the last two commands.

```sh
curl -fsSLO https://raw.githubusercontent.com/pgg898200-ship-it/streamsieve/main/afterkey/releases/afterkey-0.1.0.zip
unzip -q afterkey-0.1.0.zip
cd afterkey-0.1.0
c++ -std=c++17 -O2 -Iinclude examples/quickstart.cpp -o /tmp/afterkey-demo
/tmp/afterkey-demo
```

The reader starts after revision 1. It receives the latest value for key 7 and the deletion of key 3, in that order.

```text
3 7 11
4 3 deleted
```

For an existing service, copy [include/afterkey.hpp](include/afterkey.hpp) into its include path. The header has no third-party dependencies. Each callback receives a revision, a dense key and a borrowed pointer to the absolute value. A null pointer means deletion. Advance the cursor only after your consumer has successfully applied the entire batch. Retrying after a partial failure can redeliver rows, so apply values and tombstones idempotently.

## Read a bounded page

Use the headers in this checkout for the page API and the optional sparse journal. The 0.1.0 download above contains the original full-feed API.

Both `Index` and `Journal` expose `since_page(revision, last_key, limit, callback)`. It returns the number of rows delivered and stops at the requested limit. A deletion consumes one row. Use `std::nullopt` when only the revision is available.

```cpp
std::uint64_t cursor = 0;
std::optional<std::uint32_t> last_key;
auto delivered = index.since_page(cursor, last_key, 16,
    [&](std::uint64_t revision, std::uint32_t key, const int* value) {
        apply(key, value);  // copy the value, or apply the tombstone
        cursor = revision;
        last_key = key;
    });
```

Use the last delivered revision and key on the next call. Persist that pair with the successfully applied page when the consumer uses durable checkpoints. Each call reads current state, so writes between pages can supersede unseen rows. Empty pages leave the caller's cursor unchanged. A zero limit delivers no rows and a future revision still throws.

A matching key hint gives `Index` a direct link to the next row. A stale or invalid hint uses the existing numeric seek, which can walk the full changed suffix. `Journal` computes the start position directly for recent cursors and uses the hint or binary search for packed history. It skips superseded entries, so the page limit bounds delivered rows and callbacks. The number of journal positions scanned can be higher.

### Skip long runs of superseded writes

Use `afterkey::Journal<T, true>` when readers frequently cross a large contiguous run of retired journal positions. One bit records whether each position holds a current key, and a second level records which 64-position words have any live entries. Both `since` and `since_page` can skip holes. The default `Journal<T>` keeps the original sequential scan and write costs.

At 65,536 keys the two bitmaps occupy 16,640 bytes (about 0.254 bytes/key), plus implementation-specific bookkeeping. Writes update the maps and compaction rebuilds them. Dense sequential reads can be faster with the default journal. Both configurations emit identical rows and preserve the same cursor and callback contracts. Recompile consumers when updating these header-only templates.

A seeded 65,536-key replay on one shared AMD EPYC 9V74 VM retired a contiguous quarter of the original registration order. Median time to deliver a 16-row page starting inside that gap was **90 ns** with sparse reads and **6,690 ns** with the default journal. On the 64-row sparse catch-up, whole-feed times were 261 ns and 4,727 ns. For 4,096 densely surviving rows they were 6,780 ns and 6,710 ns. After compaction the writes measured 6.95 ns and 5.74 ns respectively, and the sparse configuration added 16,688 allocated bytes. These synthetic in-process measurements include a consuming callback. The runnable comparison and raw results are under `bench/afterkey/` in the [Tailstate source kit](https://pgg-tailstate.tancurl3.chatgpt.site).

## Which workload benefits?

With 1,048,576 populated keys, 16 hot keys and 16 staggered readers, Index ran **2.04× faster than a cursor-indexed append log** and **1.15× faster than an equivalent std::list recency index**. The latter isolates the benefit of the dense slot representation. The machine was a shared AMD EPYC 9V74 VM. Each mixed operation is one write or one poll, and a poll can deliver many rows.

| Synthetic workload | Afterkey Index | Bounded Journal | Cursor log | std::list |
| --- | ---: | ---: | ---: | ---: |
| 1M populated keys, 16 hot | 5.60 | 20.47 | 11.45 | 6.42 |
| 16K populated keys, 16 hot | 4.41 | 13.09 | 10.41 | 6.06 |
| 16K populated keys, uniform writes | 421.46 | 134.77 | 121.63 | 546.34 |

Values are median elapsed nanoseconds per mixed operation across 5 or 9 trials. Timings include the callback and exclude initialization. The cursor log won under uniform churn. With broad changes, contiguous traversal makes the Journal a faster bounded-memory option in this workload. CPU-time medians, sample ranges, the full-map scan and additional cases are in the [results CSV](bench/results.csv).

With **4,096 independently lagging readers**, a second benchmark measured each synchronous catch-up call on one thread. For a moving hot set, p99 was **1.67 µs** for Index and **4.99 µs** for the cursor log. With uniform writes, p99 was **417.86 µs** and **75.92 µs** respectively. The timer includes the consuming callback. Queueing, network and producer time are excluded. [Latency harness](bench/fanout.cpp) · [Raw percentiles](bench/fanout.json)

The public replays check exact ordered outputs. On the FreeInference session trace an hourly reader received 64 rows for 1,375 recorded completions. On the Paris Vélib’ trace an hourly reader received 895,673 rows for 5,143,865 recorded writes. At one-minute intervals only 94 Paris writes were superseded. Poll schedules were simulated. These counts describe the work a reader can skip. [Sources, checksums and replay commands](bench/replay.md)

## Contracts and costs

| | Index | Journal |
| --- | --- | --- |
| Header | `afterkey.hpp` | `afterkey_journal.hpp` |
| Write | O(1) worst-case bookkeeping | O(1) amortized, O(capacity) on compaction |
| Read | O(1 + distinct keys touched since cursor) | Scans up to min(2 × capacity, writes since cursor) entries, plus a seek for older cursors |
| Storage | Dense slots, two 32-bit neighbor indices per slot | Dense slots plus a bounded chronological key journal |

Both implementations retain one current payload per live key and tombstones for erased keys. Their own storage is allocated at construction. Payloads and callbacks can allocate. Use an application-level dense ID mapping for strings or sparse IDs. Every put and erase advances the revision, including unchanged values and repeated erases. The index rejects future cursors, out-of-range keys and callback writes. Payloads must be nothrow-move-constructible and nothrow-destructible.

Run reads and writes on one event loop or serialize access externally. Cursors belong to one live instance. On process restart generate a new instance identifier, replace client state, and replay from zero. A client expanding its subscription also needs a fresh snapshot of added keys. For audit trails, increments, commands and intermediate transitions, retain a separate event log.

## Integrations

- [StreamSieve channel scores](examples/streamsieve.cpp) publishes absolute scores and checks that a dashboard and a slower observer both reconstruct the underlying native state.
- [ZeroMQ catch-up](examples/zmq_catchup.cpp) serves independent filtered readers over REQ/REP, including tombstones and reconnects to the same server instance. Its topic masks filter after index traversal. Build instructions are in the example. The multi-topic slow-consumer case was described in [libzmq issue 1688](https://github.com/zeromq/libzmq/issues/1688).
- [Randomized reference tests](tests/test_afterkey.cpp) exercise tombstones, callbacks, independent readers and historical cursors across compactions. Run `c++ -std=c++17 -O2 tests/test_afterkey.cpp -o /tmp/test-afterkey && /tmp/test-afterkey`.

## Prior art

Kubernetes already moves changed nodes to the head of a generation-ordered list and stops its snapshot walk when it reaches an old generation. CouchDB provides a durable document changes feed with latest-change semantics. Chronicle Ring offers conflation for low-latency messaging. Afterkey packages this pattern as a small, in-process C++ component with dense storage, ordered tombstones and reproducible crossover measurements. The measured competitors above are in-process C++ implementations. [Kubernetes scheduler source](https://cos.googlesource.com/third_party/kubernetes/+/4f5c634fcfa498e3634c9b6b31cc4ca4c6a1203c/pkg/scheduler/backend/cache/cache.go) · [CouchDB changes feed](https://docs.couchdb.org/en/stable/api/database/changes.html) · [Chronicle Ring](https://chronicle.software/products/chronicle-ring)

Afterkey source is licensed under [Apache-2.0](LICENSE). Trace datasets retain their respective licenses.
