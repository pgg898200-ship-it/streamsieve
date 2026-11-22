#include "../bench/wide_radix.hpp"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <limits>
#include <random>
#include <tuple>
#include <vector>

// The scan uses sorted batches. The other engines use three different queues.
// Compare the complete ordered callback sequence after every operation across
// power-of-two epoch boundaries, with duplicate TTLs and unrepresentable sums.
int main() {
    using Time = std::uint64_t;
    using Id = std::uint32_t;
    using Event = std::tuple<Time, Id, Id, bool>;
    using namespace pulsefold::bench;
    constexpr Time maximum = std::numeric_limits<Time>::max();
    std::mt19937_64 rng(13644853);

    for (unsigned bit = 0; bit < 64; ++bit) {
        const Id feed_count = 2 + static_cast<Id>(rng() % 32);
        std::vector<std::vector<Time>> config(feed_count);
        for (auto& subscribers : config) {
            const auto size = rng() % 10;
            for (unsigned j = 0; j < size; ++j) {
                const auto kind = rng() % 4;
                if (kind == 0) subscribers.push_back(maximum);
                else if (kind == 1) subscribers.push_back(Time{1} << (rng() % 64));
                else subscribers.push_back(1 + rng() % 50);
            }
        }
        FeedScan scan(config);
        EagerFeedHeap eager(config);
        LazyRadix narrow(config);
        WideRadix wide(config);
        Time now = (Time{1} << bit) - 1;
        std::vector<Event> expected, observed;
        auto verify = [&](bool poll, Id id) {
            auto drive = [&](auto& engine, auto& events) {
                auto emit = [&](Time time, Id feed, Id subscriber, bool fresh) {
                    events.emplace_back(time, feed, subscriber, fresh);
                };
                if (poll) engine.advance(now, emit);
                else engine.observe(id, now, emit);
            };
            expected.clear();
            drive(scan, expected);
            auto compare = [&](auto& engine) {
                observed.clear();
                drive(engine, observed);
                assert(observed == expected);
            };
            compare(eager);
            compare(narrow);
            compare(wide);
        };
        for (unsigned i = 0; i < 3000; ++i) {
            now += rng() % 7;
            const bool poll = rng() % 4 == 0;
            const Id id = static_cast<Id>(rng() % feed_count);
            verify(poll, id);
        }
        now = maximum;
        verify(true, 0);
        verify(false, 0);
        verify(true, 0);
    }
    std::cout << "Four baselines matched on 192192 ordered operations.\n";
}
