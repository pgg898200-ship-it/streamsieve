# Native replay benchmark

The CSV records single-threaded, in-process C++17 replay. Each input operation is one put, erase, or poll. One poll can emit many records. Polls synchronously consume every returned key, revision, tombstone flag, and value in increasing revision order. A separately sorted dense-array implementation supplies the expected digest and callback count. Every warmup and measured replay checks both. The write-only scenario also checks the final state after timing.

## Reproduce

From the Afterkey directory on Linux, choose a CPU available to the process. CPU 2 was available for the recorded run.

```sh
g++ -std=c++17 -O3 -DNDEBUG -Wall -Wextra -pedantic bench/compare.cpp -o /tmp/afterkey-compare
taskset -c 2 /tmp/afterkey-compare 16384 65536 9
taskset -c 2 /tmp/afterkey-compare 1048576 65536 5 hot-lag
```

The recorded machine reported Linux x86-64, AMD EPYC 9V74, GCC 14.2.0 (Debian 14.2.0-19) and glibc 2.41. It was a shared virtual environment. All methods use the same compiler, flags, deterministic trace, key space and callback.

## Timing and workloads

The benchmark prefills every key outside the timed region and constructs its input stream in advance using SplitMix64 with seed `0xc004a17`. Approximately one in sixteen writes erases a present key. Erased keys become live again on the next put. Each implementation receives one warmup. Timed trials rotate method order. Each trial repeats an identical replay with a fresh instance until at least 8 ms of timed wall time have accumulated. Initialization, final-state verification and teardown are outside that interval. The application loop, callback and checksum are inside it.

| Scenario | Key distribution | Reader schedule |
| --- | --- | --- |
| hot-lag | Uniform over 16 hot keys | 16 independent cursors, each polled every 2,048 writes. Starts stagger by 128 writes. |
| uniform-lag | Uniform over every registered key | Same staggered schedule as hot-lag |
| frequent | Uniform over every registered key | One cursor every four writes |
| write-only | Uniform over every registered key | Zero timed polls |

The append-log baseline reserves storage for the entire run and seeks directly to each cursor, then discards superseded entries by checking a dense array of latest revisions. The std::list implementation splices stable nodes on overwrite, stores the data within each node and uses dense per-key iterators. It has the same output-sensitive read complexity as Afterkey Index. The dense scan reuses its sort buffer.

`min_ns_op`, `median_ns_op`, and `max_ns_op` are trial-level elapsed time divided by total mixed operations. `median_mixed_mops_s` counts millions of writes plus polls per elapsed second. `median_cpu_ns_op` uses process CPU time over the same replay loop. These statistics describe amortized throughput. The range reflects variation across trials. This benchmark does not capture single-operation tail latency, network time, synchronization or production adoption. For measured record counts from public traces under simulated poll schedules, see [public-trace replay](replay.md).
