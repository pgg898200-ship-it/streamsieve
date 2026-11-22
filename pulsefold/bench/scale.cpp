// Build with c++ -O3 -DNDEBUG -std=c++17 -Iinclude bench/scale.cpp -o scale
// Usage: ./scale [feeds [subscribers-per-feed [regular-ttl-groups]]]
#include "pulsefold.hpp"
#include <algorithm>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <numeric>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>

namespace {
using Clock = std::chrono::steady_clock;
using Id = std::uint32_t;
using Time = std::uint64_t;
using Event = std::tuple<Time, Id, Id, bool>;
constexpr Time base_ttl = 4096;

Id positive(const char* arg) {
    const std::string value(arg);
    Id n = 0;
    const auto end = value.data() + value.size();
    const auto parsed = std::from_chars(value.data(), end, n);
    if (parsed.ec != std::errc{} || parsed.ptr != end || n == 0)
        throw std::invalid_argument("dimensions must be positive 32-bit integers");
    return n;
}

Time nanoseconds(Clock::time_point a, Clock::time_point b) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(b - a).count();
}

// Linux reports the process-wide resident high-water mark in KiB. Other
// platforms still run and validate the workload, with a null RSS result.
std::optional<Time> peak_rss_bytes() {
    std::ifstream input("/proc/self/status");
    std::string line;
    while (std::getline(input, line)) {
        if (line.compare(0, 6, "VmHWM:") == 0)
            return std::stoull(line.substr(6)) * 1024;
    }
    return std::nullopt;
}

struct GroupCheck {
    std::size_t count = 0;
    const Id* validated = nullptr;
};

int run(int argc, char** argv) {
    if (argc > 4) throw std::invalid_argument("usage: scale [feeds [subscribers [ttl-groups]]]");
    const Id feed_count = argc > 1 ? positive(argv[1]) : 1048576;
    const Id subscribers = argc > 2 ? positive(argv[2]) : 1024;
    const Id groups = argc > 3 ? positive(argv[3]) : std::min<Id>(2, subscribers);
    if (groups > subscribers) throw std::invalid_argument("ttl-groups exceeds subscribers");
    const Id cohort = std::min<Id>(4, feed_count);
    const Id first_short = feed_count - cohort;
    constexpr Id seed = 73129;

    const auto start = Clock::now();
    pulsefold::Monitor::Config policies(2, std::vector<Time>(subscribers));
    for (Id s = 0; s < subscribers; ++s) policies[0][s] = base_ttl + s % groups;
    policies[1] = policies[0];
    policies[1][0] = 3;
    if (subscribers > 1) policies[1].back() = 5;
    std::vector<Id> feed_policy_ids(feed_count, 0);
    std::fill(feed_policy_ids.begin() + first_short, feed_policy_ids.end(), 1);
    pulsefold::Monitor monitor(policies, feed_policy_ids);
    const auto setup_done = Clock::now();

    std::vector<Id> renewal_order(feed_count);
    std::iota(renewal_order.begin(), renewal_order.end(), 0u);
    std::mt19937 engine(seed);
    std::shuffle(renewal_order.begin(), renewal_order.end(), engine);
    const auto shuffle_done = Clock::now();

    std::vector<Event> expected;
    for (Id f = first_short; f < feed_count; ++f) expected.emplace_back(4, f, 0, false);
    if (subscribers > 1)
        for (Id f = first_short; f < feed_count; ++f)
            expected.emplace_back(6, f, subscribers - 1, false);
    for (Id f = first_short; f < feed_count; ++f) {
        expected.emplace_back(6, f, 0, true);
        if (subscribers > 1) expected.emplace_back(6, f, subscribers - 1, true);
    }
    std::size_t events = 0;
    bool valid = true;
    auto emit = [&](Time t, Id f, Id s, bool fresh) {
        if (events >= expected.size() || expected[events] != Event(t, f, s, fresh))
            valid = false;
        ++events;
    };
    const auto arm_start = Clock::now();
    for (Id f = 0; f < feed_count; ++f) monitor.observe(f, 0, emit);
    const auto arm_done = Clock::now();
    for (auto f : renewal_order) monitor.observe(f, 1, emit);
    const auto renew_done = Clock::now();
    if (events) valid = false;
    monitor.advance(6, emit);
    for (Id f = first_short; f < feed_count; ++f) monitor.observe(f, 6, emit);
    if (!valid || events != expected.size()) throw std::runtime_error("scalar transition mismatch");

    // Independently derive the expected group size from the input data.
    // Check its full ID sequence once per immutable view, then compare pointers.
    auto group_index = [groups](Time ttl) -> std::size_t {
        if (ttl == 3) return groups;
        if (ttl == 5) return std::size_t(groups) + 1;
        return static_cast<std::size_t>(ttl - base_ttl);
    };
    std::vector<std::vector<GroupCheck>> checks(2,
        std::vector<GroupCheck>(std::size_t(groups) + 2));
    Time expected_groups = 0;
    for (Id p = 0; p < 2; ++p) {
        for (const auto ttl : policies[p]) ++checks[p][group_index(ttl)].count;
        const auto unique = std::count_if(checks[p].begin(), checks[p].end(),
                                           [](auto& g) { return g.count != 0; });
        expected_groups += Time(unique) * (p ? cohort : first_short);
    }
    const Time logical = Time(feed_count) * subscribers;
    Time group_callbacks = 0, represented = 0, last_time = 0;
    Id last_feed = 0;
    const auto flush_start = Clock::now();
    monitor.advance_groups(UINT64_MAX, [&](Time time, Id feed, pulsefold::SubscriberView view, bool fresh) {
        if (fresh || feed >= feed_count || (group_callbacks &&
            (time < last_time || (time == last_time && feed <= last_feed)))) {
            valid = false;
            return;
        }
        ++group_callbacks;
        represented += view.size();
        last_time = time;
        last_feed = feed;
        const auto policy = feed_policy_ids[feed];
        const auto ttl = time - (policy ? 6 : 1);
        if (!(ttl == 3 || ttl == 5 || (ttl >= base_ttl && ttl - base_ttl < groups))) {
            valid = false;
            return;
        }
        auto& check = checks[policy][group_index(ttl)];
        if (view.size() != check.count || view.size() == 0) valid = false;
        if (!check.validated) {
            if (!std::is_sorted(view.begin(), view.end()) ||
                std::adjacent_find(view.begin(), view.end()) != view.end()) valid = false;
            for (auto s : view)
                if (s >= subscribers || policies[policy][s] != ttl) valid = false;
            check.validated = view.begin();
        } else if (check.validated != view.begin()) valid = false;
    });
    const auto flush_done = Clock::now();
    if (!valid || group_callbacks != expected_groups || represented != logical || monitor.next_wake())
        throw std::runtime_error("group transition mismatch");

    const auto rss = peak_rss_bytes();
    std::cout << "{\n"
              << "  \"feeds\": " << feed_count << ",\n"
              << "  \"subscribers_per_feed\": " << subscribers << ",\n"
              << "  \"logical_relationships\": " << logical << ",\n"
              << "  \"policy_templates\": 2,\n"
              << "  \"regular_policy_ttl_groups\": " << groups << ",\n"
              << "  \"short_deadline_cohort_feeds\": " << cohort << ",\n"
              << "  \"shuffle_seed\": " << seed << ",\n"
              << "  \"setup_ns\": " << nanoseconds(start, setup_done) << ",\n"
              << "  \"shuffle_ns\": " << nanoseconds(setup_done, shuffle_done) << ",\n"
              << "  \"arm_all_ns\": " << nanoseconds(arm_start, arm_done) << ",\n"
              << "  \"renew_all_shuffled_ns\": " << nanoseconds(arm_done, renew_done) << ",\n"
              << "  \"verified_scalar_transitions\": " << events << ",\n"
              << "  \"full_expiry_group_callbacks\": " << group_callbacks << ",\n"
              << "  \"full_expiry_represented_subscriber_transitions\": " << represented << ",\n"
              << "  \"full_expiry_grouped_ns\": " << nanoseconds(flush_start, flush_done) << ",\n"
              << "  \"peak_rss_bytes\": " << (rss ? std::to_string(*rss) : "null") << ",\n"
              << "  \"peak_rss_source\": " << (rss ? "\"/proc/self/status VmHWM\"" : "null") << ",\n"
              << "  \"verified\": true\n}\n";
    return 0;
}
} // namespace

int main(int argc, char** argv) {
    try { return run(argc, argv); }
    catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
