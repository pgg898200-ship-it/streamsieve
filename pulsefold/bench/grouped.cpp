#include "../include/pulsefold.hpp"
#include "baselines.hpp"
#include "workloads.hpp"
#include "wide_radix.hpp"

#include <array>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>

namespace {
using namespace pulsefold::benchmarks;
using Policies = std::vector<std::vector<Time>>;

struct View {
    const Id* members;
    std::size_t length;
    const Id* begin() const { return members; }
    const Id* end() const { return members + length; }
    std::size_t size() const { return length; }
};

// Independent compilation: recover a policy's stable subscriber ordering,
// one expiry TTL per distinct value and one shared list of member IDs.
struct Policy {
    explicit Policy(const std::vector<Time>& times) : members(times.size()), rank(times.size()) {
        std::iota(members.begin(), members.end(), Id{0});
        std::stable_sort(members.begin(), members.end(), [&](Id a, Id b) { return times[a] < times[b]; });
        for (std::size_t i = 0; i < members.size(); ++i) {
            const Id id = members[i];
            rank[id] = i;
            if (i == 0 || times[id] != thresholds.back()) {
                thresholds.push_back(times[id]);
                starts.push_back(i);
            }
        }
        starts.push_back(members.size());
    }
    View range(std::size_t first, std::size_t past) const {
        return {members.data() + starts[first], starts[past] - starts[first]};
    }
    std::vector<Id> members;
    std::vector<std::size_t> rank, starts;
    std::vector<Time> thresholds;
};

struct Case {
    Scenario schedule;
    Id members, groups;
};

constexpr Case cases[] = {
    {{"small-healthy",       64, 1, 6, false, Pattern::healthy}, 256, 4},
    {{"large-mixed",       4096, 1, 6, false, Pattern::mixed}, 1024, 4},
    {{"rotating-outages",  1024, 1, 3, false, Pattern::loss}, 1024, 8},
    {{"synchronous-storm",  512, 1, 4, true, Pattern::storm}, 1024, 1},
    {{"all-unique",         256, 1, 3, false, Pattern::loss}, 128, 128},
};

struct Work {
    Trace trace;
    Policies policies;
    std::vector<Id> feed_policy;
    std::vector<Policy> compiled;
    Work(const Case& c, Time seed) : trace(generate(c.schedule, seed)),
                                    feed_policy(c.schedule.feeds, 0) {
        // Generate two explicitly shared policies only when the case has a
        // distinct short-SLO cohort. IDs in each TTL group are interleaved.
        const bool mixed = c.schedule.pattern == Pattern::mixed;
        for (unsigned p = 0; p < (mixed ? 2u : 1u); ++p) {
            std::vector<Time> times;
            const Time period = c.schedule.feeds;
            for (Id id = 0; id < c.members; ++id) {
                const Time group = id % c.groups;
                const Time ttl = p == 1
                    ? period / 2 + group * period / c.groups
                    : c.schedule.min_periods * period + group * 2 * period / c.groups;
                times.push_back(ttl);
            }
            policies.push_back(times);
            compiled.emplace_back(times);
        }
        if (mixed) {
            for (Id f = 0; f < c.schedule.feeds; f += 32) feed_policy[f] = 1;
        }
        trace.config.clear(); // The shared policies above are the measured contracts.
    }
};

template<bool Tiled>
class GroupScan {
    const Work& work_;
    struct Feed { Time last = 0; std::size_t cursor = 0; bool armed = false; };
    std::vector<Feed> feeds_;
    static constexpr std::size_t block_size = 32;
    std::vector<Time> block_min_;
    using Expiry = std::tuple<Time, Id, std::size_t>;
    std::vector<Expiry> expired_;
    Time earliest_ = std::numeric_limits<Time>::max();
public:
    explicit GroupScan(const Work& work) : work_(work), feeds_(work.feed_policy.size()) {
        if constexpr (Tiled)
            block_min_.assign((feeds_.size() + block_size - 1) / block_size, earliest_);
    }

    template<class Emit>
    void advance_groups(Time now, Emit&& emit) {
        if (now < earliest_) return;
        earliest_ = std::numeric_limits<Time>::max();
        expired_.clear();
        if constexpr (Tiled) {
            for (std::size_t block = 0; block < block_min_.size(); ++block) {
                auto& next = block_min_[block];
                if (next <= now) {
                    next = std::numeric_limits<Time>::max();
                    const auto first = block * block_size;
                    scan(first, std::min(first + block_size, feeds_.size()), now, next);
                }
                earliest_ = std::min(earliest_, next);
            }
        } else {
            scan(0, feeds_.size(), now, earliest_);
        }
        if (!std::is_sorted(expired_.begin(), expired_.end()))
            std::sort(expired_.begin(), expired_.end());
        for (const auto& [time, f, g] : expired_)
            emit(time, f, work_.compiled[work_.feed_policy[f]].range(g, g + 1), false);
    }
    template<class Emit>
    void observe_groups(Id f, Time now, Emit&& emit) {
        advance_groups(now, emit);
        auto& feed = feeds_[f];
        const auto& p = work_.compiled[work_.feed_policy[f]];
        if (feed.cursor) emit(now, f, p.range(0, feed.cursor), true);
        feed = {now, 0, true};
        const Time due = now + p.thresholds.front();
        earliest_ = std::min(earliest_, due);
        if constexpr (Tiled) {
            auto& next = block_min_[f / block_size];
            next = std::min(next, due);
        }
    }
private:
    void scan(std::size_t first, std::size_t past, Time now, Time& next) {
        for (std::size_t index = first; index < past; ++index) {
            const auto f = static_cast<Id>(index);
            auto& feed = feeds_[f];
            if (!feed.armed) continue;
            const auto& p = work_.compiled[work_.feed_policy[f]];
            while (feed.cursor < p.thresholds.size()) {
                const Time due = feed.last + p.thresholds[feed.cursor];
                if (due > now) {
                    next = std::min(next, due);
                    break;
                }
                expired_.emplace_back(due, f, feed.cursor++);
            }
        }
    }
};

// One radix-queue virtual subscriber per distinct TTL. This competitor also
// shares its full member lists across feeds and can deliver whole views.
template<class Queue>
class GroupRadix {
    const Work& work_;
    Queue engine_;
    static Policies group_ttls(const Work& work) {
        Policies ttls;
        ttls.reserve(work.feed_policy.size());
        for (Id policy : work.feed_policy) ttls.push_back(work.compiled[policy].thresholds);
        return ttls;
    }
public:
    explicit GroupRadix(const Work& work) : work_(work), engine_(group_ttls(work)) {}
    template<class Emit>
    void advance_groups(Time now, Emit&& emit) {
        engine_.advance(now, [&](Time t, Id f, Id g, bool fresh) {
            emit(t, f, work_.compiled[work_.feed_policy[f]].range(g, g + 1), fresh);
        });
    }
    template<class Emit>
    void observe_groups(Id f, Time now, Emit&& emit) {
        std::size_t recovered = 0;
        engine_.observe(f, now, [&](Time t, Id feed, Id g, bool fresh) {
            if (fresh) { recovered = g + 1; return; }
            emit(t, feed, work_.compiled[work_.feed_policy[feed]].range(g, g + 1), false);
        });
        if (recovered) emit(now, f, work_.compiled[work_.feed_policy[f]].range(0, recovered), true);
    }
};
using BinaryGroupRadix = GroupRadix<pulsefold::bench::LazyRadix>;
using WideGroupRadix = GroupRadix<pulsefold::bench::WideRadix>;
struct Product : pulsefold::Monitor {
    explicit Product(const Work& work) : Monitor(work.policies, work.feed_policy) {}
};
template<class Engine, class Emit>
void apply(Engine& engine, Operation op, Emit& emit) {
    if (op.feed == poll) engine.advance_groups(op.time, emit);
    else engine.observe_groups(op.feed, op.time, emit);
}

using Record = std::tuple<std::size_t, Time, Id, std::size_t, std::size_t, bool>;

// Compare the actual ordered transition streams before timing. Resolve each
// immutable view once by checking EVERY subscriber against the canonical
// policy member list. A record then names that exact interval in that policy.
template<class Engine>
void verify(const Work& work, std::vector<Record>& expected, bool capture) {
    Engine engine(work);
    std::set<std::tuple<Id, std::uintptr_t, std::size_t>> checked;
    std::size_t index = 0;
    for (std::size_t i = 0; i < work.trace.operations.size(); ++i) {
        auto check = [&](Time t, Id feed, auto view, bool fresh) {
            const Id policy = work.feed_policy.at(feed);
            const auto& p = work.compiled.at(policy);
            if (!view.size()) throw std::logic_error("empty callback");
            const auto offset = p.rank.at(*view.begin());
            const auto key = std::make_tuple(policy, reinterpret_cast<std::uintptr_t>(view.begin()), view.size());
            if (checked.insert(key).second &&
                (view.size() > p.members.size() - offset ||
                 !std::equal(view.begin(), view.end(), p.members.begin() + offset)))
                throw std::logic_error("group membership mismatch");
            const Record r{i, t, feed, offset, view.size(), fresh};
            if (capture) expected.push_back(r);
            else if (index >= expected.size() || r != expected[index])
                throw std::logic_error("ordered group stream mismatch");
            ++index;
        };
        apply(engine, work.trace.operations[i], check);
    }
    if (index != expected.size()) throw std::logic_error("group count mismatch");
}

struct Totals {
    Time groups = 0, members = 0, checksum = 0;
    template<class V>
    void operator()(Time t, Id f, V view, bool fresh) {
        ++groups;
        members += view.size();
        checksum += mix(t ^ (Time(f) << 32) ^ *view.begin() ^ mix(view.size()) ^ fresh);
    }
};
struct Result {
    Totals totals;
    double mean, p99;
};
template<class Engine>
Result run(const Work& work) {
    Engine engine(work);
    const auto& trace = work.trace;
    Totals warm, totals;
    for (std::size_t i = 0; i < trace.warmup; ++i) apply(engine, trace.operations[i], warm);
    const auto count = trace.operations.size() - trace.warmup;
    std::vector<double> samples;
    samples.reserve(count / batch_size);
    double wall_ns = 0;
    for (auto b = trace.warmup; b < trace.operations.size(); b += batch_size) {
        const auto e = std::min(b + batch_size, trace.operations.size());
        const auto start = Clock::now();
        for (auto i = b; i < e; ++i) apply(engine, trace.operations[i], totals);
        const double ns = std::chrono::duration<double, std::nano>(Clock::now() - start).count();
        wall_ns += ns;
        if (e - b == batch_size) samples.push_back(ns / batch_size);
    }
    std::sort(samples.begin(), samples.end());
    return {totals, wall_ns / count, samples[(99 * samples.size() - 1) / 100]};
}

unsigned long long number(const std::string& s) {
    if (s.empty() || s.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("expected a nonnegative integer");
    return std::stoull(s);
}
int benchmark(int argc, char** argv) {
    unsigned reps = 5;
    Time seed = 20261122;
    std::string selected;
    for (int i = 1; i < argc; ++i) {
        const std::string flag = argv[i];
        if (flag == "--help") {
            std::cout << "grouped [--repeats N] [--seed N] [--scenario NAME]\n";
            return 0;
        }
        if (i + 1 == argc) throw std::invalid_argument("missing value for " + flag);
        const std::string val = argv[++i];
        if (flag == "--repeats") {
            auto n = number(val);
            if (!n || n > 1000) throw std::invalid_argument("repeats must be in [1,1000]");
            reps = static_cast<unsigned>(n);
        } else if (flag == "--seed") seed = number(val);
        else if (flag == "--scenario") selected = val;
        else throw std::invalid_argument("unknown option " + flag);
    }
    constexpr std::array<const char*, 5> names = {"pulsefold", "group-scan", "group-radix", "group-wide-radix", "group-tiled-scan"};
    using Fn = Result(*)(const Work&);
    const std::array<Fn, names.size()> runners = {run<Product>, run<GroupScan<false>>, run<BinaryGroupRadix>, run<WideGroupRadix>, run<GroupScan<true>>};
    std::cout << "scenario,seed,repeat,run_order,engine,feeds,subscribers_per_feed,distinct_ttls,"
                 "warmup_ops,measured_ops,group_callbacks,represented_transitions,checksum,batch_size,"
                 "mean_ns_per_op,p99_batch_mean_ns_per_op\n";
    bool found = false;
    for (const auto& c : cases) {
        if (!selected.empty() && selected != c.schedule.name) continue;
        found = true;
        const Work work(c, seed);
        std::vector<Record> expected;
        verify<Product>(work, expected, true);
        verify<GroupScan<false>>(work, expected, false);
        verify<GroupScan<true>>(work, expected, false);
        verify<BinaryGroupRadix>(work, expected, false);
        verify<WideGroupRadix>(work, expected, false);
        for (unsigned rep = 0; rep < reps; ++rep) {
            for (unsigned order = 0; order < names.size(); ++order) {
                const auto id = (rep + order) % names.size();
                const auto r = runners[id](work);
                std::cout << c.schedule.name << ',' << seed << ',' << rep + 1 << ',' << order + 1 << ','
                          << names[id] << ',' << c.schedule.feeds << ',' << c.members << ',' << c.groups
                          << ',' << work.trace.warmup << ',' << work.trace.operations.size() - work.trace.warmup
                          << ',' << r.totals.groups << ',' << r.totals.members << ',' << r.totals.checksum
                          << ',' << batch_size << ',' << std::fixed << std::setprecision(3) << r.mean << ','
                          << r.p99 << '\n';
            }
        }
    }
    if (!found) throw std::invalid_argument("unknown scenario " + selected);
    return 0;
}
} // namespace
int main(int argc, char** argv) {
    try { return benchmark(argc, argv); }
    catch (const std::exception& e) { std::cerr << e.what() << '\n'; return EXIT_FAILURE; }
}
