#include "../include/afterkey.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <vector>

// g++ -std=c++17 -O3 -DNDEBUG bench/fanout.cpp -o /tmp/afterkey-fanout
// taskset -c 2 /tmp/afterkey-fanout
// The result is JSON. Timings cover the call and its consuming callbacks. Reader
// scheduling, prefill and result verification are outside each sample.
using Key = std::uint32_t;
using U64 = std::uint64_t;
using Clock = std::chrono::steady_clock;
constexpr Key capacity = 1U << 20;
constexpr Key readers = 4096;
constexpr Key warmup = 8192;
constexpr Key measured_writes = 32768;
constexpr unsigned trials = 3;

class CursorLog {
    struct Row { U64 revision{}; U64 value{}; bool present{}; };
    std::vector<Row> rows;
    std::vector<Key> history;
    void write(Key key, U64 value, bool present) {
        history.push_back(key);
        rows[key] = {history.size(), value, present};
    }
public:
    explicit CursorLog(Key n) : rows(n) { history.reserve(n + warmup + measured_writes); }
    void put(Key k, U64 v) { write(k, v, true); }
    void erase(Key k) { write(k, 0, false); }
    template<class Sink> void since(U64 cursor, Sink&& consume) const {
        for (std::size_t i = cursor; i < history.size(); ++i) {
            Key k = history[i];
            const auto& r = rows[k];
            if (r.revision == i + 1) consume(r.revision, k, r.present ? &r.value : nullptr);
        }
    }
};

static U64 random_word(U64& state) {
    state += 0x9e3779b97f4a7c15ULL;
    U64 z = state;
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

struct Write { Key key; U64 value; };
struct Trace {
    std::vector<Write> writes;
    std::vector<std::vector<Key>> polls;
    std::size_t measured_polls = 0;
    explicit Trace(bool uniform) : polls(warmup + measured_writes) {
        U64 writer_rng = 0x20190d;
        U64 reader_rng = 0x10074d;
        for (Key i = 0; i < polls.size(); ++i) {
            Key word = static_cast<Key>(random_word(writer_rng));
            // Move 16 of the 64 hot keys every 512 writes. All readers see the
            // same state history, with different cuts based on their own pace.
            Key base = (i / 512) * 16;
            Key k = uniform ? word % capacity : (base + word % 64) % capacity;
            writes.push_back({k, random_word(writer_rng)});
        }
        for (Key reader = 0; reader < readers; ++reader) {
            Key period = 256U << (reader % 6);
            Key first = random_word(reader_rng) % period;
            for (Key i = first; i < polls.size(); i += period) {
                polls[i].push_back(reader);
                measured_polls += i >= warmup;
            }
        }
    }
};

struct Output { U64 hash, count; };
struct Samples { std::vector<U64> latency; U64 emitted = 0; };

static U64 ns(Clock::time_point start, Clock::time_point end) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
}

template<class Store>
static void run(const Trace& trace, Samples& samples, std::vector<Output>& oracle, bool first) {
    Store store(capacity);
    for (Key k = 0; k < capacity; ++k) store.put(k, k);
    std::vector<U64> cursors(readers, capacity);
    std::size_t poll_num = 0;
    U64 emitted = 0;
    for (std::size_t i = 0; i < trace.writes.size(); ++i) {
        const auto& w = trace.writes[i];
        if ((w.value & 31) == 0) store.erase(w.key);
        else store.put(w.key, w.value);
        for (Key reader : trace.polls[i]) {
            U64 cursor = cursors[reader];
            Output out{0xcbf29ce484222325ULL ^ cursor, 0};
            auto consume = [&](U64 revision, Key key, const U64* value) {
                out.hash = (out.hash ^ revision) * 1099511628211ULL;
                out.hash = (out.hash ^ key) * 1099511628211ULL;
                out.hash = (out.hash ^ (value ? *value : 0)) * 1099511628211ULL;
                out.hash ^= value != nullptr;
                ++out.count;
            };
            auto start = Clock::now();
            std::atomic_signal_fence(std::memory_order_seq_cst);
            store.since(cursor, consume);
            std::atomic_signal_fence(std::memory_order_seq_cst);
            auto end = Clock::now();
            cursors[reader] = capacity + i + 1;
            if (i >= warmup) {
                samples.latency.push_back(ns(start, end));
                emitted += out.count;
                if (first) oracle.push_back(out);
                else if (out.count != oracle.at(poll_num).count || out.hash != oracle[poll_num].hash)
                    throw std::runtime_error("per-poll output mismatch");
                ++poll_num;
            }
        }
    }
    if (poll_num != trace.measured_polls) throw std::runtime_error("missing poll");
    samples.emitted += emitted;
}

static U64 percentile(const std::vector<U64>& sorted, double fraction) {
    auto rank = static_cast<std::size_t>(std::ceil(fraction * sorted.size()));
    return sorted[rank - 1];
}

static void summarize(const char* name, Samples& s) {
    std::sort(s.latency.begin(), s.latency.end());
    std::cout << "      \"" << name << "\": {\"samples\":" << s.latency.size()
              << ",\"emitted_per_trial\":" << s.emitted / trials
              << ",\"p50_ns\":" << percentile(s.latency, 0.5)
              << ",\"p99_ns\":" << percentile(s.latency, 0.99)
              << ",\"p999_ns\":" << percentile(s.latency, 0.999) << "}";
}

int main() {
    std::vector<U64> floor;
    for (unsigned i = 0; i < 100000; ++i) {
        auto a = Clock::now();
        std::atomic_signal_fence(std::memory_order_seq_cst);
        auto b = Clock::now();
        floor.push_back(ns(a, b));
    }
    std::sort(floor.begin(), floor.end());
    std::cout << "{\n  \"capacity\":" << capacity << ", \"readers\":" << readers
              << ", \"warmup_writes\":" << warmup << ", \"measured_writes\":" << measured_writes
              << ", \"trials\":" << trials << ", \"empty_clock_pair_median_ns\":" << percentile(floor, 0.5)
              << ",\n  \"method\":\"Single-threaded synthetic closed-loop wall time per poll, including consuming callback. "
                 "Nearest-rank percentiles pooled across trials. Timers are unsubtracted. Requests execute serially. "
                 "Excludes queue wait, networking and producer time.\",\n  \"scenarios\":{\n";
    for (bool uniform : {false, true}) {
        Trace trace(uniform);
        std::vector<Output> oracle;
        oracle.reserve(trace.measured_polls);
        Samples index, log;
        index.latency.reserve(trials * trace.measured_polls);
        log.latency.reserve(trials * trace.measured_polls);
        for (unsigned trial = 0; trial < trials; ++trial) {
            if (trial % 2 == 0) {
                run<CursorLog>(trace, log, oracle, trial == 0);
                run<afterkey::Index<U64>>(trace, index, oracle, false);
            } else {
                run<afterkey::Index<U64>>(trace, index, oracle, false);
                run<CursorLog>(trace, log, oracle, false);
            }
        }
        std::cout << "    \"" << (uniform ? "uniform" : "moving-hot-set") << "\":{\n";
        summarize("afterkey", index);
        std::cout << ",\n";
        summarize("cursor-log", log);
        std::cout << "\n    }" << (uniform ? "\n" : ",\n");
    }
    std::cout << "  }\n}\n";
}
