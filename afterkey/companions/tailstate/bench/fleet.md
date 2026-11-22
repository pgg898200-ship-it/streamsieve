# One million keys, 10,000 subscriber cursors

This single-threaded replay measures a publisher applying 786,432 writes and serving 120,000 pages of up to 16 rows. Each simulated subscriber owns its cursor and acknowledges its last row immediately. A page request does no networking, serialization or storage I/O. Subscriber count represents logical readers served serially.

A seeded Fisher–Yates shuffle sets the initial mutation order of 1,048,576 keys. Half the subscribers start in the first half of that history. The rest start within 4,096 writes of the original head. Each of twelve waves applies 65,536 writes before serving one page per subscriber in a shuffled order. Nine of every ten writes go to a 32,768-key region that moves each wave. Every eleventh live-phase write is a deletion. Consumers retain their last (sequence, key) hint across waves.

The comparator is a C++17 `std::map` keyed by sequence. An array gives each key its current tree iterator. Mutations reuse nodes and insert at the monotone end. A live hint advances from the cached iterator, and a stale hint invokes `upper_bound`. Tailstate uses `ts_create_indexed` and `ts_resume`.

A separate verification pass compares sequence, key, value and deletion on every emitted row. Timed passes include the same digest and cursor advancement on both sides. Three fresh instances per method run in alternating AB/BA/AB pairs. Build time covers allocation and initial state load. Mixed time sums timed writes and reads after load. No method is resident alongside its competitor during its timed pass.

## Reproduce

Run from the repository root with a C99 compiler and a C++17 compiler.

```sh
make build/tailstate.o
c++ -Iinclude -O3 -std=c++17 bench/fleet.cpp build/tailstate.o -o build/fleet
build/fleet
```

## Recorded replay

GCC/G++ 14.2.0 with `-O3` on an AMD EPYC 9V74 Linux virtual machine. The table shows medians of the three measured totals for each column. VM scheduling and cache locality affect this test. The three indexed mixed times ranged from 270.5 to 586.6 ms.

| Engine | Build ms | Write ms | Page ms | Mixed ms |
| --- | ---: | ---: | ---: | ---: |
| Tailstate indexed | 53.870 | 96.532 | 474.814 | 571.346 |
| Hinted std::map | 78.405 | 301.350 | 120.084 | 421.434 |

All 1,920,000 rows matched field by field. Of 120,000 requests, 115,614 carried live anchors and 4,386 carried stale anchors. The median cursor lag was 787,184 mutations and p95 was 1,579,925. Tailstate reserved 54,657,088 bytes according to `ts_store_bytes`, excluding client cursors and allocator overhead. Full run timings and digests appear in [fleet.txt](fleet.txt).
