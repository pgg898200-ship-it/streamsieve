#include "../include/pulsefold.hpp"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>

namespace {

using Tick = std::uint64_t;
using Index = std::uint32_t;
using Config = std::vector<std::vector<Tick>>;
using Event = std::tuple<Tick, Index, Index, bool>;
using Events = std::vector<Event>;
constexpr Tick last_tick = std::numeric_limits<Tick>::max();

void require(bool condition, const std::string& message) {
    if (!condition) throw std::runtime_error(message);
}

// The oracle scans every independent subscription at every call. It shares
// neither an ordering index nor an expiration cursor with the product.
struct Watch {
    Tick ttl;
    std::optional<Tick> observed_at;
    bool stale = false;
};

class ScanMonitor {
public:
    explicit ScanMonitor(const Config& config) : feeds_(config.size()) {
        for (std::size_t f = 0; f < config.size(); ++f) {
            for (Tick ttl : config[f]) feeds_[f].push_back({ttl, std::nullopt, false});
        }
    }

    Events apply(Tick now, std::optional<Index> refresh) {
        Events events;
        for (Index f = 0; f < feeds_.size(); ++f) {
            for (Index s = 0; s < feeds_[f].size(); ++s) {
                auto& watch = feeds_[f][s];
                if (!watch.observed_at || watch.stale) continue;
                // Subtraction handles an unrepresentable mathematical deadline.
                if (now - *watch.observed_at >= watch.ttl) {
                    events.emplace_back(*watch.observed_at + watch.ttl, f, s, false);
                    watch.stale = true;
                }
            }
        }
        std::sort(events.begin(), events.end());
        if (refresh) {
            auto& feed = feeds_[*refresh];
            for (Index s = 0; s < feed.size(); ++s) {
                if (feed[s].stale) events.emplace_back(now, *refresh, s, true);
                feed[s].observed_at = now;
                feed[s].stale = false;
            }
        }
        return events;
    }

    std::optional<Tick> first_deadline() const {
        std::optional<Tick> earliest;
        for (const auto& feed : feeds_) {
            for (const auto& watch : feed) {
                if (watch.stale || !watch.observed_at || last_tick - *watch.observed_at < watch.ttl)
                    continue;
                const Tick due = *watch.observed_at + watch.ttl;
                if (!earliest || due < *earliest) earliest = due;
            }
        }
        return earliest;
    }

private:
    std::vector<std::vector<Watch>> feeds_;
};

class Check {
public:
    explicit Check(const Config& config) : actual_(config), grouped_(config), oracle_(config) {}

    Check(const Config& policies, const std::vector<Index>& policy_for)
        : actual_(policies, policy_for), grouped_(policies, policy_for),
          oracle_(expand(policies, policy_for)) {}

    Events call(Tick now, std::optional<Index> refresh = std::nullopt) {
        Events recorded;
        auto emit = [&](Tick at, Index feed, Index subscriber, bool fresh) {
            recorded.emplace_back(at, feed, subscriber, fresh);
        };
        if (refresh) actual_.observe(*refresh, now, emit);
        else actual_.advance(now, emit);

        Events expanded;
        std::optional<std::tuple<Tick, Index, bool>> previous;
        auto emit_group = [&](Tick at, Index feed, pulsefold::SubscriberView group, bool fresh) {
            require(group.size() > 0 && static_cast<std::size_t>(group.end() - group.begin()) == group.size(),
                "invalid subscriber view");
            const auto key = std::make_tuple(at, feed, fresh);
            require(!previous || key != *previous, "emitted multiple groups for one deadline or recovery");
            previous = key;
            for (Index subscriber : group) expanded.emplace_back(at, feed, subscriber, fresh);
        };
        if (refresh) grouped_.observe_groups(*refresh, now, emit_group);
        else grouped_.advance_groups(now, emit_group);
        require(expanded == recorded, "group expansion changed the scalar event order");

        // Ordering of recovery callbacks within one feed is immaterial. All
        // expirations, including those at the observation tick, must precede them.
        const auto recoveries = std::find_if(recorded.begin(), recorded.end(),
            [](const Event& e) { return std::get<3>(e); });
        const auto context = " at tick " + std::to_string(now) + " on call " +
            std::to_string(++calls_);
        require(std::all_of(recoveries, recorded.end(),
            [](const Event& e) { return std::get<3>(e); }), "expiration after recovery" + context);
        require(std::is_sorted(recorded.begin(), recoveries), "expirations out of order" + context);
        std::sort(recoveries, recorded.end());
        require(recorded == oracle_.apply(now, refresh), "oracle mismatch" + context);
        const auto hint = actual_.next_wake();
        const auto exact = oracle_.first_deadline();
        require(!hint || *hint > now, "wake hint failed to advance" + context);
        require(!exact || (hint && *hint <= *exact), "missing or late wake hint" + context);
        return recorded;
    }

    void reject(Tick now, std::optional<Index> refresh) {
        const auto hint = actual_.next_wake();
        bool called = false;
        bool rejected = false;
        auto emit = [&](Tick, Index, Index, bool) { called = true; };
        try {
            if (refresh) actual_.observe(*refresh, now, emit);
            else actual_.advance(now, emit);
        } catch (const std::exception&) {
            rejected = true;
        }
        require(rejected && !called, "invalid call must reject before dispatch");
        require(hint == actual_.next_wake(), "invalid call changed next wake");
    }

private:
    static Config expand(const Config& policies, const std::vector<Index>& policy_for) {
        Config by_feed;
        for (Index policy : policy_for) by_feed.push_back(policies.at(policy));
        return by_feed;
    }

    pulsefold::Monitor actual_;
    pulsefold::Monitor grouped_;
    ScanMonitor oracle_;
    std::size_t calls_ = 0;
};

void boundaries() {
    Check c(Config{{7, 3, 3, 1}, {4, 3}, {}, {1}});
    require(c.call(101).empty(), "unseen feeds must stay silent");
    for (Index f : {1u, 3u, 0u, 2u}) require(c.call(101, f).empty(), "first observation");
    require(c.call(102, 0) == Events{{102,0,3,false}, {102,3,0,false}, {102,0,3,true}},
        "same-tick expiration must precede recovery");
    require(c.call(102, 0).empty(), "duplicate observation");
    require(c.call(105) == Events{{103,0,3,false}, {104,1,1,false}, {105,0,1,false},
        {105,0,2,false}, {105,1,0,false}}, "tied TTLs must retain subscriber IDs");
    require(c.call(109, 0) == Events{{109,0,0,false}, {109,0,0,true}, {109,0,1,true},
        {109,0,2,true}, {109,0,3,true}}, "stalled feed recovery");
    c.call(last_tick);
    require(c.call(last_tick).empty(), "a stale transition may fire only once");

    Check empty(Config{});
    empty.call(0);
    empty.reject(55, 0);
    empty.call(1);
    empty.call(last_tick);
}

void clock_limits() {
    Check c(Config{{last_tick, last_tick - 1}, {1, 2, 3}, {last_tick}});
    c.call(0, 0);
    c.call(1, 2);
    c.call(last_tick - 2, 1);
    require(c.call(last_tick - 1) == Events{{last_tick-1,0,1,false}, {last_tick-1,1,0,false}},
        "near-maximum deadline");
    require(c.call(last_tick, 1) == Events{{last_tick,0,0,false}, {last_tick,1,1,false},
        {last_tick,1,0,true}, {last_tick,1,1,true}}, "exact max and unrepresentable deadlines");
    c.call(last_tick, 0);
    require(c.call(last_tick).empty(), "overflow must not create a deadline at max");

    Check rearm(Config{{10}, {2}});
    rearm.call(last_tick - 12, 0);
    rearm.call(last_tick - 9, 0);  // Its mathematical deadline now overflows.
    rearm.call(last_tick - 2);    // Retire the obsolete first wake.
    rearm.call(last_tick - 2, 1);
    require(rearm.call(last_tick) == Events{{last_tick,1,0,false}},
            "a new feed must still fire after the previous wake overflows");
}

void rejected_calls_are_atomic() {
    Check c(Config{{2, 5}, {1}, {}});
    c.call(10, 0);
    c.reject(100, 3);
    c.reject(9, std::nullopt);
    c.reject(9, 1);
    c.reject(8, std::numeric_limits<Index>::max());
    require(c.call(12) == Events{{12,0,0,false}}, "invalid calls may not consume deadlines");
    c.reject(last_tick, std::numeric_limits<Index>::max());
    c.call(13, 0);
    c.call(14, 1);
    c.call(25);
}

void shared_policies() {
    const Config policies{{7, 1, 3, 1}, {}, {11, 4}};
    const std::vector<Index> policy_for{2, 0, 0, 1, 0, 2};
    Check c(policies, policy_for);
    for (Index f : {0u, 1u, 4u, 3u}) c.call(1, f);
    c.call(2, 2);
    c.call(2, 4);
    c.call(4, 1);
    c.call(6, 5);
    c.call(1000);
    for (Index f = 0; f < policy_for.size(); ++f) c.call(1000, f);
    c.call(last_tick);

    Check unused(policies, {});
    unused.call(last_tick);
    bool rejected = false;
    try {
        pulsefold::Monitor bad(policies, std::vector<Index>{0, 3});
    } catch (const std::exception&) {
        rejected = true;
    }
    require(rejected, "invalid shared policy ID");
    rejected = false;
    try {
        pulsefold::Monitor bad(Config{{3, 0, 4}});
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    require(rejected, "zero TTL");
}

void differential() {
    for (unsigned seed = 0; seed < 60; ++seed) {
        std::mt19937_64 rng(seed);
        const Index n = 1 + static_cast<Index>(rng() % 24);
        Config config(n);
        for (auto& feed : config) {
            const auto size = rng() % 24;
            for (std::size_t s = 0; s < size; ++s) feed.push_back(1 + rng() % 20);
        }
        Check c(config);
        Tick now = 0;
        for (int step = 0; step < 450; ++step) {
            if (step % 53 == 0) now += 1000;
            else now += rng() % 4;
            if (step % 37 == 0) c.reject(now + 100, n);
            if (rng() % 5 == 0) c.call(now);
            else c.call(now, static_cast<Index>(rng() % n));
        }
        c.call(last_tick);
        for (Index f = 0; f < n; ++f) c.call(last_tick, f);
        c.call(last_tick);
    }
}

} // namespace

int main() {
    try {
        boundaries();
        clock_limits();
        rejected_calls_are_atomic();
        shared_policies();
        differential();
        std::cout << "Pulsefold monitor checks passed (60 randomized traces).\n";
        return EXIT_SUCCESS;
    } catch (const std::exception& e) {
        std::cerr << "Pulsefold monitor check failed: " << e.what() << '\n';
        return EXIT_FAILURE;
    }
}
