# Tailstate benchmarks

Run the complete catch-up benchmark from the repository root on a POSIX host with a C99 compiler.

```sh
cc -O3 -DNDEBUG -std=c99 -Wall -Wextra -Werror -pedantic -Iinclude \
  src/tailstate.c bench/bench.c -o /tmp/tailstate-bench
/tmp/tailstate-bench
```

[results.csv](results.csv) contains the reference run on an AMD EPYC 9V74 VM, Linux/x86-64, GCC 14.2.0. The run used the command above with `taskset -c 2` to pin the process. The harness generates its input from a fixed seed. Its rows and ordered digests are deterministic. Wall times vary with the machine and other processes. [pages-results.txt](pages-results.txt) covers 16-row pages against a C++ ordered map with and without a last-key hint and includes its own build commands. A [Google Borg machine-events replay](real/README.md) checks complete catch-up results on a public fleet-capacity trace.

## What is timed

Every implementation receives the same mutations and returns the latest version of every key changed after the caller's cursor, ordered by increasing sequence. Deletes carry a tombstone with a zero payload. The caller consumes all four fields in an order-sensitive digest. A row count and digest mismatch on any warm read or mixed-round subscriber query aborts the run. The harness compares all eight kernels, including independently implemented forward and reverse replay. The library's test suite separately checks individual rows.

The catch-up suite seeds 65,536 dense keys, then applies 32 batches of 1,024 mutations. About one in sixteen writes is a delete. Uniform traces draw from the full keyspace. Hot traces scatter 32 frequently written IDs across that keyspace. Eight trials rotate the eight implementations through every timing position. State, input preallocation, and the initial fill are outside the timed sections. The raw journal arrays are pre-sized for the entire trace. Their write numbers exclude the cost of dynamically growing a log.

| CSV column | Measurement |
| --- | --- |
| `write_ns_each` | Median elapsed time for a write-only pass divided by 32,768 writes. |
| `read_p50_ns`, `read_p95_ns` | Median and nearest-rank 95th percentile of 88 full warm reads after the writes. Each trial starts with an untimed read. |
| `mixed_ns_per_round` | From a fresh seeded store, median trial total divided by 32 rounds. One round includes 1,024 writes and all indicated subscriber queries. |
| `mixed_round_p99_ns` | Nearest-rank 99th percentile across 256 individual mixed rounds, including compactions. Host scheduling can affect this statistic. |
| `index_bytes` | Requested algorithm state and buffers, including preallocated sorting scratch or history where applicable. Excludes allocator overhead, generated trace, and validation buffers. |
| `rows_nominal`, `digest_nominal` | Row count and hexadecimal digest for the full nominal lag at the final head. Other subscribers read from nearby distinct cursors. |

All times include the digest work and timer overhead where applicable. Baseline row consumption can be inlined. Tailstate uses its public synchronous visitor API. These runs measure a single process and thread with 64-bit values and no transport, persistence, or subscriber queue.

## Workloads

| Name | Interpretation |
| --- | --- |
| hot-duplicates | 8 subscribers about 8,192 writes behind a steady 32-key hot set. |
| hot-onset | Same hot traffic, beginning immediately after the uniform seed. Early mixed requests still include thousands of cold keys. |
| hot-write-heavy | One subscriber for each 1,024 writes against the steady hot set. |
| multirate | 8 subscribers about 32,768 writes behind. Every 1,024th write touches a new slow-moving key while other writes hit the hot set. |
| uniform-recent | 8 subscribers about 64 writes behind uniformly distributed updates. |
| uniform-backlog | 8 subscribers about 16,384 writes behind uniformly distributed updates. |
| initial-sync | One request from cursor zero after each batch. The CSV encodes this case with an unsigned sentinel of 4294967295. |

The multirate case emits 64 rows at nominal lag because the cursor covers 32 slow-key writes and recent updates to all 32 hot keys. The fixed ring holds 8,192 recent positions and must consult the dense table for that case. Its safe-cursor optimization can still use the ring for old cursors if eviction has only removed superseded entries since the cursor. A different ring size shifts the crossover.

## Comparison kernels

| Method | State and cost driver |
| --- | --- |
| tailstate | A fixed slot table with an intrusive recency list. Writes move the affected key to the tail. Numeric seeks and complete reads scale with the number of changed keys. |
| tailstate-indexed | Same ordered rows, plus the library's preallocated sequence prefix index for seeking. |
| dense-radix | Scan all key versions, gather matches in reusable arrays, and use stable radix passes only on bytes that can differ. Fixed space. |
| journal-latest | Jump to the cursor's offset in a four-byte-per-write key log. Scan forward and emit only events whose sequence still matches the key's current version. |
| journal-reverse | Scan the same key log backward using reusable per-key generation stamps; replay surviving keys in reverse discovery order. |
| journal-bitmap | Mark only current events in a two-level positional bitmap. Scan nonempty groups, words and set bits starting at the cursor. Fast reads with history-sized storage. |
| ring-bitmap | Keep just the latest 8,192 event positions with the same bitmap. Record a conservative bound on live evictions. Use the dense radix fallback when the cursor requires older rows. Fixed space. |
| compact-bitmap | Keep at most 81,920 event positions. Compact live keys in sequence order when full and rebuild the bitmap. Newer cursors have a direct offset; older cursors binary-search the compacted sequence prefix. Fixed space and periodic whole-buffer work on the writer. |

The compacting and full-history bitmap kernels can deliver lower read latency than Tailstate. The compacting kernel additionally measures the cost of reorganizing the live set on writes. Uniform backlog and initial synchronization exercise cache locality across many keys. The cases with frequent duplicates exercise how much of the write history each approach must inspect per subscriber.

## Reference run

The multirate run returned the same 64 ordered rows and digest `754e12ae20c6ffd6` for all eight implementations. Selected results include the fastest reader and the fastest mixed implementation in this case.

| Method | Warm read p50 | Mixed round* | Requested bytes |
| --- | ---: | ---: | ---: |
| Tailstate | 351 ns | 7.33 µs | 1,581,112 |
| Tailstate indexed | 291 ns | 11.99 µs | 3,416,128 |
| Ring bitmap | 36,840 ns | 1,031.86 µs | 1,672,360 |
| Full-history bitmap | 260 ns | 6.38 µs | 1,655,192 |
| Compacting bitmap | 230 ns | 15.37 µs | 2,238,776 |

\*Eight trials. The reported mixed value is the median of each trial's mean round time. The shared VM had visible scheduling stalls. In particular, long mixed-round averages and upper-tail times should be replicated on an isolated deployment CPU. The full-history bitmap's allocation reflects this trace length; it increases as history grows. The compacting bitmap and both Tailstate indexes have capacities fixed at initialization.

Run provenance (SHA-256)

```text
6b708620f10e6c2ca97afd2a356d219d3c25a7fc6d19b574bfacd321b87f872f  src/tailstate.c
f80741ab543a03a4cd26d0a53aa13d00fd56f34a779a083f66ca149fa169651e  include/tailstate.h
26bccd5785d4133ad249a883b7b0bfe11a24e6f57285a351fd50f13e1b53d56b  bench/bench.c
d65643126198301f626630acf2d547815cad137e4a27f6aaf2f91d0c87c66127  bench/results.csv
```
