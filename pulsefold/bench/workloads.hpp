#pragma once

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <limits>
#include <numeric>
#include <random>
#include <vector>

namespace pulsefold::benchmarks {
using Time = std::uint64_t;
using Id = std::uint32_t;
using Clock = std::chrono::steady_clock;
constexpr Id poll = std::numeric_limits<Id>::max();
constexpr std::size_t batch_size = 128;

enum class Pattern { healthy, mixed, loss, churn, storm, polling };
struct Scenario {
    const char* name;
    Id feeds;
    Id subscribers;
    unsigned min_periods;
    bool uniform_ttl;
    Pattern pattern;
};

inline constexpr Scenario scenarios[] = {
    {"small",              8,  4,  4, false, Pattern::healthy},
    {"single-subscriber", 1024, 1,  4, false, Pattern::healthy},
    {"long-grace",       4096, 16, 32, false, Pattern::healthy},
    {"mixed-slo",        2048, 16,  6, false, Pattern::mixed},
    {"rotating-outages", 1024, 16,  3, false, Pattern::loss},
    {"high-churn",        128, 16,  1, false, Pattern::churn},
    {"synchronous-storm", 512, 32,  4,  true, Pattern::storm},
    {"poll-heavy",        256,  8,  3, false, Pattern::polling},
};

struct Operation { Time time; Id feed; };
struct Trace {
    std::vector<std::vector<Time>> config;
    std::vector<Operation> operations;
    std::size_t warmup = 0;
};

// One round visits every feed in a seeded permutation. A unit of logical time
// is one visit slot. The storm case puts a whole round at the same instant.
inline Trace generate(const Scenario& sc, Time seed) {
    std::mt19937_64 random(seed);
    const Time period = sc.feeds;
    Trace trace;
    trace.config.resize(sc.feeds);
    for (Id f = 0; f < sc.feeds; ++f) {
        for (Id s = 0; s < sc.subscribers; ++s) {
            Time ttl = sc.min_periods * period;
            if (!sc.uniform_ttl) ttl += random() % (ttl + 1);
            // Mixed SLOs: one in 32 feeds has tighter freshness requirements.
            if (sc.pattern == Pattern::mixed && f % 32 == 0)
                ttl = period / 2 + random() % period;
            if (sc.pattern == Pattern::churn) ttl = 1 + random() % period;
            trace.config[f].push_back(ttl);
        }
    }
    std::vector<Id> permutation(sc.feeds);
    std::iota(permutation.begin(), permutation.end(), 0U);
    const std::size_t warm_rounds = 2 * sc.min_periods + 4;
    const std::size_t measured_rounds = std::max<std::size_t>(64, 32768 / sc.feeds);
    Time now = 0;
    const bool storm = sc.pattern == Pattern::storm;
    for (std::size_t r = 0; r < warm_rounds + measured_rounds; ++r) {
        if (r == warm_rounds) trace.warmup = trace.operations.size();
        for (std::size_t i = permutation.size(); i > 1; --i)
            std::swap(permutation[i - 1], permutation[random() % i]);
        if (storm) {
            now += period;
            // The whole fleet goes quiet long enough to expire every feed.
            if (r % 8 == 7) {
                now += 8 * period;
                trace.operations.push_back({now, poll});
            }
        }
        for (Id f : permutation) {
            if (!storm) ++now;
            // Each cohort vanishes for eight rounds in every sequence of 64.
            const bool missing = sc.pattern == Pattern::loss && f % 8 == (r / 8) % 8;
            trace.operations.push_back({now, missing ? poll : f});
            // Reader polls interleaved with each renewal at the same logical time.
            if (sc.pattern == Pattern::polling) {
                for (int p = 0; p < 7; ++p) trace.operations.push_back({now, poll});
            }
        }
    }
    return trace;
}

inline Time mix(Time n) {
    n = (n ^ (n >> 30)) * 0xbf58476d1ce4e5b9ULL;
    n = (n ^ (n >> 27)) * 0x94d049bb133111ebULL;
    return n ^ (n >> 31);
}

struct Digest {
    Time fresh = 0, stale = 0, sum = 0, parity = 0, operation = 0;
    void operator()(Time time, Id feed, Id subscriber, bool is_fresh) {
        // Include the operation that delivered the transition. Sum and XOR
        // allow ties to arrive in different orders without masking duplicates.
        const Time h = mix(time ^ mix(operation) ^ mix((Time(feed) << 32) | subscriber)
                                ^ (is_fresh ? 0xE9017B82CD685C5FULL : 0));
        sum += h;
        parity ^= h;
        if (is_fresh) ++fresh; else ++stale;
    }
    bool matches(const Digest& other) const {
        return fresh == other.fresh && stale == other.stale &&
               sum == other.sum && parity == other.parity;
    }
};

} // namespace pulsefold::benchmarks
