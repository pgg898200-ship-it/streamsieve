#include "recastcrps.hpp"

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <exception>
#include <iomanip>
#include <iostream>
#include <limits>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using i32 = std::int32_t;
using u32 = std::uint32_t;
using wide = __int128_t;

constexpr i32 lowest = std::numeric_limits<i32>::min();
constexpr i32 highest = std::numeric_limits<i32>::max();

void require(bool ok, const std::string& message) {
    if (!ok) throw std::runtime_error(message);
}

template <class Action>
void require_throw(const char* label, Action action) {
    try {
        action();
    } catch (const std::exception&) {
        return;
    }
    throw std::runtime_error(std::string(label) + " did not throw");
}

struct Member {
    i32 value = 0;
    u32 weight = 0;
};

wide distance(i32 a, i32 b) {
    const wide delta = static_cast<wide>(a) - b;
    return delta >= 0 ? delta : -delta;
}

// Direct weighted empirical CRPS. Summing each unordered pair once cancels
// the factor 1/2 in the double sum. Keep even adversarial numerators exact.
long double direct_score(const std::vector<Member>& members, i32 observation) {
    wide total = 0;
    wide absolute_error = 0;
    wide pair_distance = 0;
    for (std::size_t i = 0; i < members.size(); ++i) {
        const auto [xi, wi] = members[i];
        total += wi;
        absolute_error += wi * distance(xi, observation);
        for (std::size_t j = 0; j < i; ++j) {
            const auto [xj, wj] = members[j];
            pair_distance += static_cast<wide>(wi) * wj * distance(xi, xj);
        }
    }
    require(total > 0, "empty reference distribution");
    const wide numerator = total * absolute_error - pair_distance;
    return static_cast<long double>(numerator) / static_cast<long double>(total * total);
}

void compare_score(long double result, long double expected, i32 observation) {
    // Respect platforms where long double is binary64. No absolute tolerance:
    // very small nonzero scores and exact zero must survive cancellation.
    const long double tolerance = 16 * std::numeric_limits<long double>::epsilon()
                                  * std::fabs(expected);
    if (std::isfinite(result) && std::fabs(result - expected) <= tolerance) return;
    std::ostringstream message;
    message << std::setprecision(std::numeric_limits<long double>::max_digits10)
            << "score(" << observation << ") returned " << result
            << ", expected " << expected;
    throw std::runtime_error(message.str());
}

struct Fixture {
    recastcrps::Ensemble ensemble;
    std::vector<Member> members;

    Fixture(const std::vector<i32>& support, std::size_t slots)
        : ensemble(support, slots), members(slots) {}

    void set(std::size_t slot, i32 x, u32 weight) {
        ensemble.set(slot, x, weight);
        members.at(slot) = {x, weight};
    }

    void erase(std::size_t slot) {
        ensemble.erase(slot);
        members.at(slot) = {};
    }

    void check(i32 extra_observation = 17) const {
        std::size_t count = 0;
        std::uint64_t total = 0;
        for (const auto& m : members) {
            count += m.weight > 0;
            total += m.weight;
        }
        require(ensemble.count() == count, "incorrect number of active slots");
        require(ensemble.total_weight() == total, "incorrect total weight");
        if (!count) {
            require_throw("score of empty ensemble", [&] { (void)ensemble.score(0); });
            return;
        }
        for (const i32 y : {lowest, -1, 0, 1, highest, extra_observation}) {
            compare_score(ensemble.score(y), direct_score(members, y), y);
        }
    }
};

void test_ties_and_revisions() {
    Fixture f({lowest, -1, 0, 1, highest}, 5);
    f.check();
    f.erase(4);
    f.set(2, 0, 7);
    f.set(3, 0, 2);
    f.set(1, lowest, 3);
    f.set(4, highest, 1);
    f.check();
    f.set(1, 0, 4);
    f.set(3, 0, 2);
    f.ensemble.set(2, 0);
    f.members[2] = {0, 1};  // default weight on replacement
    f.check();
    for (const auto i : {2, 4, 2, 0, 1, 3}) {
        f.erase(i);
        f.check();
    }
    // A previously emptied slot can be reused at a different support value.
    f.set(1, -1, 1);
    f.set(2, 1, 1);
    f.check();
    compare_score(f.ensemble.score(0), 0.5L, 0);
}

void test_failures_preserve_state() {
    Fixture f({lowest, 0, highest}, 3);
    f.set(0, lowest, 9);
    f.set(1, 0, 3);
    const auto reject = [&](const char* label, auto action) {
        require_throw(label, action);
        f.check();
    };
    reject("unknown support point", [&] { f.ensemble.set(0, 42, 2); });
    reject("zero weight", [&] { f.ensemble.set(1, highest, 0); });
    reject("out-of-range set", [&] { f.ensemble.set(3, 0); });
    reject("out-of-range erase", [&] { f.ensemble.erase(3); });
    reject("overflowing weight", [&] { f.ensemble.set(2, 0, std::numeric_limits<u32>::max()); });

    const u32 limit = static_cast<u32>(highest);
    f.set(0, lowest, limit - 3);
    f.check();
    reject("sum overflow on insert", [&] { f.ensemble.set(2, highest, 1); });
    reject("sum overflow on replacement", [&] { f.ensemble.set(1, highest, 4); });
    f.set(0, highest, limit - 3);  // at capacity, replace a slot in place
    f.check();
    f.erase(1);
    f.set(0, 0, limit);
    f.check();
    f.set(0, lowest, limit - 1);
    f.set(2, highest, 1);
    f.check(lowest + 1);
    f.erase(0);
    f.check();
}

void test_support_shapes() {
    // Unsorted input, repeated coordinates, and observations beyond support.
    for (const i32 base : {lowest, -5, highest - 2}) {
        Fixture f({base + 2, base, base + 1, base + 2}, 4);
        f.set(0, base + 1, 17);
        f.set(1, base + 2, 19);
        f.set(3, base, 2);
        f.check(base);
        f.check(base + 2);
        const i32 unknown = base == lowest ? highest : lowest;
        require_throw("outside contiguous support", [&] { f.ensemble.set(3, unknown, 9); });
        f.check(base + 1);
    }
    Fixture single({4}, 2);
    single.set(1, 4, 17);
    single.set(0, 4, 20);
    single.check(4);
}

void check_withdrawals(const Fixture& f, i32 observation, bool check_extrema) {
    const auto omitted = f.ensemble.leave_one_out(observation);
    require(omitted.size() == f.members.size(), "omission vector has wrong length");
    long double lo = std::numeric_limits<long double>::infinity();
    long double hi = -lo;
    i32 low_value = 0;
    i32 high_value = 0;
    for (std::size_t i = 0; i < omitted.size(); ++i) {
        if (!f.members[i].weight || f.ensemble.count() < 2) {
            require(std::isnan(omitted[i]), "undefined omission should be NaN");
            continue;
        }
        auto remaining = f.members;
        remaining[i] = {};
        const auto expected = direct_score(remaining, observation);
        require(omitted[i] == static_cast<double>(expected), "incorrect omission score");
        const i32 value = f.members[i].value;
        if (expected < lo || (expected == lo && value < low_value)) {
            lo = expected;
            low_value = value;
        }
        if (expected > hi || (expected == hi && value < high_value)) {
            hi = expected;
            high_value = value;
        }
    }
    if (!check_extrema) return;
    if (f.ensemble.count() < 2) {
        require_throw("extrema without two active slots",
                      [&] { (void)f.ensemble.withdrawal_extremes(observation); });
        return;
    }
    const auto [minimum, maximum] = f.ensemble.withdrawal_extremes(observation);
    require(minimum.first == low_value && minimum.second == static_cast<double>(lo),
            "incorrect minimum withdrawal or tie break");
    require(maximum.first == high_value && maximum.second == static_cast<double>(hi),
            "incorrect maximum withdrawal or tie break");
}

void test_withdrawals() {
    Fixture f({lowest, -2, -1, 0, 1, 2, highest}, 10);
    check_withdrawals(f, 0, true);
    f.set(1, -1, 1);
    check_withdrawals(f, 0, true);
    f.set(2, 1, 1);
    check_withdrawals(f, 0, true);  // both extrema tied across distinct values
    f.set(9, 1, 1);
    f.set(3, 1, 1);
    f.set(2, -1, 1);
    check_withdrawals(f, 0, true);  // ties at duplicate coordinates
    f.set(2, -1, 3);
    require_throw("weighted extrema", [&] { (void)f.ensemble.withdrawal_extremes(0); });
    check_withdrawals(f, highest, false);
    f.erase(2);
    check_withdrawals(f, lowest, true);
    std::mt19937 rng(30280001);
    const std::vector<i32> values = {lowest, -2, -1, 0, 1, 2, highest};
    for (unsigned step = 0; step < 800; ++step) {
        const auto slot = rng() % f.members.size();
        if (rng() % 5 == 0) f.erase(slot);
        else f.set(slot, values[rng() % values.size()], 1);
        check_withdrawals(f, values[rng() % values.size()], true);
        check_withdrawals(f, 3, true);
    }
}

void test_replay() {
    const std::vector<i32> support = {lowest, -1001, -9, -1, 0, 1, 7, 99, 1001, highest};
    for (const u32 seed : {1u, 2026u, 535353u}) {
        Fixture f(support, 15);
        std::mt19937 rng(seed);
        for (unsigned step = 0; step < 1200; ++step) {
            const auto slot = rng() % f.members.size();
            if (rng() % 4 == 0) {
                f.erase(slot);
            } else {
                const auto x = support[rng() % support.size()];
                const u32 weight = rng() % 3 == 0 ? 1 : 1 + rng() % 1000000;
                f.set(slot, x, weight);
            }
            const i32 y = static_cast<i32>(rng() % 2000001) - 1000000;
            try {
                f.check(y);
            } catch (const std::exception& e) {
                throw std::runtime_error("seed " + std::to_string(seed)
                                       + ", step " + std::to_string(step) + ": " + e.what());
            }
        }
    }
}

}  // namespace

int main() {
    try {
        test_ties_and_revisions();
        test_failures_preserve_state();
        test_support_shapes();
        test_withdrawals();
        test_replay();
        std::cout << "CRPS pairwise oracle, limits, revisions and replay passed\n";
    } catch (const std::exception& e) {
        std::cerr << "test failure: " << e.what() << '\n';
        return EXIT_FAILURE;
    }
}
