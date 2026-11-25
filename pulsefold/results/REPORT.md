# Pulsefold benchmark results

On one pinned CPU, Pulsefold represented 1,073,741,824 feed–subscriber relationships at a median **34.27 MiB** process high-water mark. Expiring every feed produced **2,097,160 callbacks** and took **18.65 ms** including validation. A grouped callback passes an immutable slice of subscriber IDs. Sending to each subscriber would add downstream work.

## Scalar dispatch

Each cell gives median wall-clock nanoseconds per input operation across repeated runs. Operations are observations or watermark advances. The factor divides the fastest competing median by Pulsefold’s median, using the underlying unrounded values. Above 1.00 favors Pulsefold. The last column computes that ratio separately for each seed, first 20261122 and then 73492051. The pooled ratio uses all 20 runs per engine.

The eager heap updates one indexed deadline per feed. The cached scan rechecks all feeds when its minimum expires. The lazy and wide radix queues defer checks across renewals and use one-bit and four-bit bucket widths respectively. Each comparator is an implementation in this repository. [Heap, scan, and lazy radix](../bench/baselines.hpp) · [Wide radix](../bench/wide_radix.hpp)

| Workload | Pulsefold | Eager heap | Cached scan | Lazy radix | Wide radix | Factor | Factors by seed |
|---|---:|---:|---:|---:|---:|---:|---:|
| small | 3.42 | 21.69 | 3.37 | 15.70 | 14.10 | 0.99× | 0.99× / 0.97× |
| single-subscriber | 4.03 | 27.62 | 3.88 | 17.39 | 14.23 | 0.96× | 0.96× / 0.96× |
| long-grace | 3.57 | 33.05 | 4.50 | 9.35 | 7.77 | 1.26× | 1.34× / 1.22× |
| mixed-slo | 25.99 | 46.84 | 910.15 | 36.60 | 31.85 | 1.23× | 1.20× / 1.43× |
| rotating-outages | 26.88 | 43.38 | 360.94 | 43.03 | 35.60 | 1.32× | 1.36× / 1.38× |
| high-churn | 439.64 | 748.64 | 635.69 | 533.51 | 448.98 | 1.02× | 1.02× / 1.08× |
| synchronous-storm | 24.88 | 59.77 | 82.35 | 36.20 | 34.26 | 1.38× | 1.39× / 1.38× |
| poll-heavy | 1.86 | 5.59 | 1.65 | 8.05 | 6.52 | 0.89× | 0.87× / 0.91× |
| Intel Berkeley replay | 35.77 | 50.00 | 36.23 | 79.62 | 79.29 | 1.01× | — |

Pulsefold had the lowest pooled median in **five of eight** synthetic schedules. Its largest advantage was 1.38× during synchronous outages. The cached scan led on the small, single-subscriber and poll-heavy schedules. The small, single-subscriber, high-churn and recorded sensor results were within 5% of the fastest competing median. Treat those margins as close on this shared host.

The recorded sensor workload contains 10,535 timestamped observations from 54 Intel Berkeley motes. The first 1,053 observations warmed each engine. The remaining 9,482 observations produced 4,731 stale and 4,725 fresh scalar callbacks under four experimental freshness budgets of 45, 90, 180 and 600 seconds. Pulsefold measured 35.77 ns/op and feed-scan measured 36.23 ns/op. The 1.01× factor on this trace is a practical tie. [Fixture and provenance](../bench/data/README.md) · [Synthetic runs](synthetic.csv) · [Sensor runs](sensor.csv)

## Grouped dispatch

All four engines return subscriber slices. The scan caches a conservative next wake. The two radix implementations schedule each distinct TTL and map the result to a shared subscriber slice. They use the same one-bit and four-bit radix queues as the scalar benchmark. Each engine ran eight times per workload, twice in each run-order position. Callback and transition counts cover one measured replay.

| Workload | Pulsefold ns/op | Group scan ns/op | Group radix ns/op | Group wide radix ns/op | Factor | Group callbacks | Represented transitions |
|---|---:|---:|---:|---:|---:|---:|---:|
| small-healthy | 4.07 | 4.61 | 16.10 | 14.52 | 1.13× | 0 | 0 |
| large-mixed | 13.48 | 647.54 | 26.55 | 22.89 | 1.70× | 26,695 | 9,998,336 |
| rotating-outages | 12.39 | 249.25 | 32.19 | 26.31 | 2.12× | 9,216 | 2,097,152 |
| synchronous-storm | 5.62 | 6.49 | 17.71 | 17.03 | 1.16× | 8,192 | 8,388,608 |
| all-unique | 36.82 | 193.21 | 81.42 | 64.54 | 1.75× | 66,048 | 131,072 |

Pulsefold had the lowest median in all five grouped workloads in the four-engine run. An earlier six-repeat, three-engine run ranked the cached scan first on small-healthy, so the small healthy workload has shown both orderings on this host. Its factor against the fastest competing median was 2.12× on rotating outages and 1.70× on the large mixed-policy fleet. Distinct TTLs still allow a single recovery callback to cover several readers that were already stale. [Grouped runs](grouped.csv)

## Full-fleet outage

Each run starts a new process, compiles two shared policies, arms every feed, and renews every feed in a seeded shuffle. A four-feed cohort expires and recovers selected readers. The final watermark expires every reader through the grouped API. The verifier checks timestamp and feed order, callback counts, represented totals, and each immutable group’s full membership once before checking its identity on reuse.

| Metric, median of five fresh processes | Shared TTLs | All unique within each feed |
|---|---:|---:|
| Feeds | 1,048,576 | 16,384 |
| Subscribers per feed | 1,024 | 256 |
| Regular policy TTL groups | 2 | 256 |
| Logical relationships | 1,073,741,824 | 4,194,304 |
| Policy and monitor setup (ms) | 11.50 | 0.17 |
| Build shuffled renewal order (ms) | 8.46 | 0.06 |
| Arm all feeds (ms) | 6.50 | 0.08 |
| Renew all feeds (ms) | 58.07 | 0.07 |
| Verified scalar transitions before outage | 16 | 16 |
| Outage group callbacks | 2,097,160 | 4,194,304 |
| Represented transitions | 1,073,741,824 | 4,194,304 |
| Outage dispatch and verification (ms) | 18.65 | 34.04 |
| Process peak RSS (MiB) | 34.27 | 2.23 |

The five full-fleet dispatch times ranged from 17.09–26.41 ms. The shuffled renewal times ranged from 45.22–309.74 ms. The shared-TTL case compresses identical expiry deadlines. The all-unique case needs one callback per reader during a full outage. The workloads use different feed counts. Compare the callback counts to see how TTL diversity affects the amount of work. Linux reports process peak RSS through the VmHWM field of `/proc/self/status`, including inputs, the renewal permutation, the monitor and the verifier. [Shared-TTL processes](scale.jsonl) · [All-unique processes](unique.jsonl)

## Measurement and reproduction

Runs were pinned to logical CPU 4 on a shared Linux x86-64 host with an AMD EPYC 9V74. The build used GCC 14.2.0 and the Makefile flags below. Each scalar engine ran 10 times on each of two synthetic seeds and 10 times on the fixed sensor trace. Scalar and grouped harnesses rotate engine order. Pooled synthetic results weight each seed equally.

The scalar timer starts after trace generation, engine construction, and warm-up. Each returned scalar event contributes its timestamp, feed, subscriber, transition type and delivering operation to a digest inside the timed loop. Timed grouped callbacks read the first member and group length and update a checksum. An untimed replay verifies the ordered grouped event streams and every distinct view’s full membership against the independent competitors. Raw CSV p99 values summarize **average ns/op within complete 128-operation batches**. Individual event latency, event-loop scheduling, transport, model inference and consumer delivery require separate measurements.

Run from the repository root. Choose an available CPU if logical CPU 4 is unavailable.

```sh
make CXX=g++ CXXFLAGS='-O3 -std=c++17 -DNDEBUG -Wall -Wextra -Wpedantic' build/benchmark build/grouped build/scale
taskset -c 4 build/benchmark --seed 20261122 --repeats 10
taskset -c 4 build/benchmark --seed 73492051 --repeats 10
taskset -c 4 build/benchmark --trace bench/data/intel-lab-4h.tsv --repeats 10
taskset -c 4 build/grouped --seed 73492051 --repeats 8
for run in 1 2 3 4 5; do taskset -c 4 build/scale; done
for run in 1 2 3 4 5; do taskset -c 4 build/scale 16384 256 256; done
```

## November 24, 2026 • grouped challenger and recorded replay

A block scan caches the earliest deadline for each consecutive 32 feeds. When the global minimum is due, it checks block minima and scans only the blocks that could have expired. Both scan variants use the same expiration ordering and callback code. On rotating outages this was the fastest of four challengers. Pulsefold processed the workload in 9.68–9.79 ns/op versus 16.02–16.14 ns/op for the block scan. On the small healthy workload there were no callbacks.

Each cell reports the median of ten run means, in ns per input operation. The factor is the fastest challenger's median divided by Pulsefold's median. Every engine occupied each starting position twice per scenario and seed.

| Grouped workload | Pulsefold (73492051) | Fastest challenger | Factor | Pulsefold (20261124) | Fastest challenger | Factor |
|---|---:|---|---:|---:|---|---:|
| small-healthy | 1.77 | 2.21 (group-scan) | 1.25× | 1.77 | 2.21 (group-scan) | 1.25× |
| large-mixed | 7.83 | 13.27 (group-wide-radix) | 1.70× | 7.99 | 13.50 (group-wide-radix) | 1.69× |
| rotating-outages | 9.68 | 16.14 (group-tiled-scan) | 1.67× | 9.79 | 16.02 (group-tiled-scan) | 1.64× |
| synchronous-storm | 2.43 | 3.14 (group-scan) | 1.29× | 2.43 | 3.34 (group-scan) | 1.37× |
| all-unique | 29.35 | 41.85 (group-wide-radix) | 1.43× | 29.60 | 42.28 (group-wide-radix) | 1.43× |

On the recorded Intel Berkeley sensor replay, the scalar cached scan took 27.44 ns/op versus Pulsefold’s 29.19 ns/op. Both emitted 4,731 stale and 4,725 fresh transitions over the 9,482 measured observations. All five engines matched on both output digests. The simpler scan merits consideration for a small fleet with scalar callbacks.

PGG ran both benchmarks on a shared AMD EPYC 9V45 host, pinned to logical CPU 4, using GCC 14.2.0 and the release Makefile defaults. The November 22 comparison used an EPYC 9V74 and four grouped engines. Host and challenger changes prevent attributing the entire change in margin to one cause. Timings exclude setup, warm-up and external delivery. The grouped harness checked ordered streams and subscriber membership before timing. These are first-party measurements.

[Grouped measurements, 500 runs](grouped-20261124.csv) · [Sensor replay, 50 runs](sensor-20261124.csv) · [Challenger implementation](../bench/grouped.cpp)

From the release branch’s `pulsefold` directory:

```sh
make build/grouped build/benchmark
for seed in 73492051 20261124; do
    taskset -c 4 build/grouped --seed "$seed" --repeats 10
done
taskset -c 4 build/benchmark --trace bench/data/intel-lab-4h.tsv --repeats 10
```

## November 25, 2026 • replaying silence in an external trace

The scalar benchmark on the release branch accepts both `TIME FEED` and `TIME tick` from an external trace. A tick advances event time without reporting a heartbeat. This lets every engine measure expiry during silence or at the end of a capture. Timestamps must be nondecreasing and use microseconds for the built-in 54-feed sensor policy.

PGG replayed the published Intel Berkeley fixture with one experimental tick appended 600 seconds after its final observation. This models ten minutes of silence after capture; the fixture itself records no observations for that additional period. All five engines matched on both output digests. Their measured transition counts matched the replay CLI after subtracting the 1,053-operation warm-up.

| Measured segment | Observations | Ticks | Stale | Fresh |
|---|---:|---:|---:|---:|
| Original fixture | 9,482 | 0 | 4,731 | 4,725 |
| Same fixture with final tick | 9,482 | 1 | 4,912 | 4,725 |

Run from the branch’s `pulsefold` directory. The derived trace SHA-256 is `c56088adc6667f2348c52b26a6fff61a0316eb78cd486a4d969ef453f84adb04`.

```sh
make build/benchmark
trace=$(mktemp)
cat bench/data/intel-lab-4h.tsv > "$trace"
awk 'END {printf "%.0f tick\n", $1 + 600000000}' bench/data/intel-lab-4h.tsv >> "$trace"
build/benchmark --trace "$trace" --repeats 1
rm "$trace"
```
