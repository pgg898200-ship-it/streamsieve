---
title: Pulsefold
emoji: ⏱️
colorFrom: green
colorTo: blue
sdk: static
app_file: demo/index.html
license: mit
short_description: Exact heartbeat freshness for fleets with shared reader deadlines
tags:
  - cpp
  - event-time
  - multi-agent
  - benchmarking
---

# Pulsefold

Track when a heartbeat becomes too old for each of its readers. One reader can stop routing work after 50 ms of silence, another can wait 500 ms before failing a task, and an operator dashboard can wait five seconds. Every reader shares the same last-seen timestamp.

Pulsefold is a dependency-free C++17 header with a replay CLI and a POSIX live adapter. Configure a deadline policy once and assign it to a million feeds. Each feed stores one deadline. A 16-way tree of lower bounds skips feeds whose deadline is still in the future. When many readers share a deadline, one callback can hand the consumer a read-only list of everyone affected.

[Interactive heartbeat replay](https://pulsefold.tancurl3.chatgpt.site/) · [Measured results](results/REPORT.md) · [Source header](include/pulsefold.hpp)

On one AMD EPYC CPU, a 1,048,576-feed test represented **1,073,741,824 logical watches in 34.27 MiB** of peak process memory. The grouped rotating-outage benchmark took **12.39 ns per input operation**, against **26.31 ns** for the fastest competing implementation in the release. On a 54-sensor recorded trace, Pulsefold and a cached scan were within 1.3%. Read the full results before choosing an engine.

## Run the example

    curl -fsSLO https://pulsefold.tancurl3.chatgpt.site/releases/pulsefold-0.1.0.zip
    unzip -q pulsefold-0.1.0.zip
    cd pulsefold-0.1.0
    make check
    build/pulsefold 1 5,12 < examples/heartbeat.txt

The last heartbeat at time 4 sets deadlines at 9 and 16. The heartbeat at 10 recovers reader 0 and sets new deadlines for both readers. Output:

    9 0 0 stale
    10 0 0 fresh
    15 0 0 stale
    22 0 1 stale

The columns are event time, feed ID, subscriber ID, and new state. The input accepts `TIME FEED` and `TIME tick`, in nondecreasing time order. Ticks let a replay report silence even when every sender has stopped. The `--summary` switch counts transitions. Integer times and positive deadlines must use the same unit.

## Install from the release branch

The [release branch](https://github.com/pgg898200-ship-it/streamsieve/tree/pulsefold-v0.1.0/pulsefold) adds install targets after the 0.1.0 source archive. From a checkout of that branch:

    make -C pulsefold install PREFIX="$HOME/.local"
    "$HOME/.local/bin/pulsefold" 1 5,12 < pulsefold/examples/heartbeat.txt

A C++17 consumer can now use `#include <pulsefold.hpp>` with `-I"$HOME/.local/include"`. On POSIX, add the live adapter with `make -C pulsefold install-live PREFIX="$HOME/.local"`. Packagers can stage either target with `DESTDIR`, for example `make -C pulsefold install PREFIX=/usr DESTDIR="$PWD/stage"`.

## Connect live agents

On a POSIX host, feed numeric sender IDs to a long-lived process, one per line.

    make live-check
    { printf '0\n'; sleep 1; printf '0\n'; sleep 1; } | build/pulsefold-live 1 250,750

The adapter uses a monotonic clock and one poll timeout for the whole fleet. Deadlines continue to fire during silence while the input remains open. Output reports the logical expiry time in milliseconds since startup. OS scheduling, backpressure and consumer work affect wall-clock arrival.

## Embed in an event loop

```cpp
#include "pulsefold.hpp"
#include <cstdint>
#include <vector>

void example() {
    using Id = std::uint32_t;
    using Time = std::uint64_t;
    pulsefold::Monitor::Config policies{{50, 50, 500}};
    std::vector<Id> feed_policy_ids(10000, 0);
    pulsefold::Monitor monitor(policies, feed_policy_ids);

    auto on_group = [](Time time, Id feed, pulsefold::SubscriberView readers, bool fresh) {
        // Enqueue the read-only slice for this feed, or iterate its IDs here.
        (void)time; (void)feed; (void)readers; (void)fresh;
    };
    monitor.observe_groups(7, 100, on_group);
    monitor.advance_groups(150, on_group);  // Feed 7, stale readers {0, 1}.
    monitor.observe_groups(7, 200, on_group); // One recovery for those readers.
}
```

`observe` and `advance` expand the same transitions into scalar callbacks `(time, feed, subscriber, fresh)`. A one-argument constructor accepts different TTL lists for individual feeds. The shared-policy constructor accepts any mixture of feed-to-policy assignments.

### Event contract

- A feed starts unknown. The first observation arms its readers silently. A feed that has never reported cannot produce an expiry.
- Each reader expires exactly once per gap. All expiries due at an observation time occur before that observation recovers readers. Equal-time expiries appear in ascending feed and subscriber ID order.
- An expiry group contains all readers sharing that TTL for the feed. A recovery view contains exactly the stale readers, ordered by TTL then subscriber ID. Views refer to immutable policy storage. Keep the owning monitor alive when handing off a view.
- Call `advance` on an event-loop wake or new watermark. `next_wake()` is a conservative hint that can be early after a healthy renewal. Ask for it again after advancing. Recheck after input to catch an earlier deadline.
- Inputs use nondecreasing unsigned 64-bit event time. A deadline equal to UINT64_MAX can fire. A mathematical deadline beyond UINT64_MAX cannot fire. Invalid IDs and backward clocks are rejected before processing the operation.
- Drive a monitor from one thread, with nonthrowing callbacks that do not reenter it. Shard by feed if independent event-time order is sufficient. Subscribe/unsubscribe requires building a new configuration. Consumers own late-data handling, transport, backpressure and cross-shard ordering.

### Cost and fit

With shared policies, stored state scales with the feed count plus the size of the policy definitions. A healthy refresh before the pending wake has constant scheduling cost. A consumer can act on a whole stale group in constant time or enumerate its members. Delivering a separate message to every reader still takes work proportional to the number of deliveries.

Choose the grouped path when reader deadlines repeat and the next stage can handle a group. Distinct TTLs produce distinct expiry groups. The scalar comparison includes a cached scan, an indexed per-feed heap and two deferred radix queues, with the same output contract and per-operation digest. The benchmark report gives crossovers.

## Reproduce or challenge a result

Run `make build/benchmark build/scale`. Run `build/scale` for the billion-watch challenge, or `build/scale 16384 256 256` for the all-unique control. Run `build/benchmark --trace bench/data/intel-lab-4h.tsv --repeats 8` for sensor replay. The trace contains observed device timestamps. The monitor applies four experimental TTLs to those timestamps.

For an independent reproduction, include the CPU, compiler and flags, source revision, raw benchmark output and input provenance. The companion benchmark and trace files can also be used to test a challenger that emits equivalent transitions. [Results and commands](results/REPORT.md) · [Public discussion](https://huggingface.co/spaces/pgg-engineering/pulsefold/discussions)

The release was measured on one machine. Independent reproductions and production integrations are welcome.

Code is MIT licensed. The sensor data has its own attribution and permission statement in the data directory.
