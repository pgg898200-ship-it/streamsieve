#include "recastcrps.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <random>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

using Value = std::int32_t;
using Weight = std::uint32_t;
using Integer = std::int64_t;
using Member = std::pair<Value, Weight>;

struct Revision {
    std::size_t slot;
    Member member;
    Value observation;
};

struct Workload {
    std::string name;
    std::vector<Value> support;
    std::vector<Member> initial;
    std::vector<Revision> revisions;
    std::size_t score_every;
};

// All benchmark integer intermediates are bounded by 2^63. This shortcut
// is valid for this driver only, not for the library's entire input domain.
long double finish(Integer deviation, Integer spread, Integer mass) {
    if (mass == 0) throw std::logic_error("driver queried an empty ensemble");
    const long double w = static_cast<long double>(mass);
    return (deviation * w - spread) / (w * w);
}

long double score_ordered(const std::vector<Member>& members, Value observation) {
    Integer mass = 0, moment = 0, spread = 0, deviation = 0;
    for (const auto& [value, weight] : members) {
        const auto x = static_cast<Integer>(value);
        deviation += weight * std::abs(x - observation);
        spread += weight * (x * mass - moment);
        mass += weight;
        moment += weight * x;
    }
    return finish(deviation, spread, mass);
}

class Resort {
    std::vector<Member> slots_;
    std::vector<Member> scratch_;
public:
    Resort(const std::vector<Value>&, std::size_t n) : slots_(n) { scratch_.reserve(n); }
    void set(std::size_t i, Value x, Weight w) { slots_[i] = {x, w}; }
    void erase(std::size_t i) { slots_[i] = {}; }
    long double score(Value y) {
        scratch_.clear();
        for (const auto& m : slots_) if (m.second) scratch_.push_back(m);
        std::sort(scratch_.begin(), scratch_.end());
        return score_ordered(scratch_, y);
    }
};

class Ordered {
    std::vector<Member> slots_;
    std::vector<Member> order_;
public:
    Ordered(const std::vector<Value>&, std::size_t n) : slots_(n) { order_.reserve(n); }
    void erase(std::size_t i) {
        if (slots_[i].second) {
            order_.erase(std::lower_bound(order_.begin(), order_.end(), slots_[i]));
            slots_[i] = {};
        }
    }
    void set(std::size_t i, Value x, Weight w) {
        const Member member{x, w};
        if (slots_[i] == member) return;
        erase(i);
        order_.insert(std::lower_bound(order_.begin(), order_.end(), member), member);
        slots_[i] = member;
    }
    long double score(Value y) const { return score_ordered(order_, y); }
};

// A flat quantized histogram is cheap to revise and scans only distinct
// support cells at scoring time. A dense inverse rank table deliberately
// gives it constant-time updates on all cases in this benchmark.
class Histogram {
    struct Slot { std::size_t rank = 0; Weight w = 0; };
    std::vector<Slot> slots_;
    std::vector<Value> support_;
    std::vector<Integer> weights_;
    std::vector<std::size_t> rank_;
public:
    Histogram(const std::vector<Value>& s, std::size_t n)
        : slots_(n), support_(s), weights_(s.size()), rank_(Integer(s.back())-s.front()+1) {
        for (std::size_t i=0; i<s.size(); ++i) rank_[Integer(s[i])-s.front()] = i;
    }
    void erase(std::size_t i) {
        weights_[slots_[i].rank] -= slots_[i].w;
        slots_[i].w = 0;
    }
    void set(std::size_t i, Value x, Weight w) {
        erase(i);
        const auto rank = rank_[Integer(x)-support_.front()];
        slots_[i] = {rank, w};
        weights_[rank] += w;
    }
    long double score(Value y) const {
        Integer mass = 0, moment = 0, spread = 0, deviation = 0;
        for (std::size_t i=0; i<weights_.size(); ++i) {
            const auto x = Integer(support_[i]), w = weights_[i];
            spread += w * (x * mass - moment);
            deviation += w * std::abs(x - y);
            mass += w;
            moment += w * x;
        }
        return finish(deviation, spread, mass);
    }
};

// Two Fenwick moments and one maintained pairwise-spread total. Ranks are
// arithmetic for a regular lattice, binary-searched for an irregular support.
class Fenwick {
    struct Sums { Integer w = 0, wx = 0; };
    struct Slot { std::size_t rank = 0; Weight weight = 0; };
    std::vector<Value> support_;
    std::vector<Slot> slots_;
    std::vector<Sums> tree_;
    Sums total_;
    Integer spread_ = 0;
    Integer step_ = 0;

    std::size_t lower_rank(Value x) const {
        if (step_) {
            if (x <= support_.front()) return 0;
            if (x > support_.back()) return support_.size();
            return (static_cast<Integer>(x) - support_.front() + step_ - 1) / step_;
        }
        return std::lower_bound(support_.begin(), support_.end(), x) - support_.begin();
    }
    Integer deviation(Value x, std::size_t n_less) const {
        Sums lower;
        for (auto i = n_less; i; i &= i - 1) {
            lower.w += tree_[i].w;
            lower.wx += tree_[i].wx;
        }
        return static_cast<Integer>(x) * (2 * lower.w - total_.w) + total_.wx - 2 * lower.wx;
    }
    void change(std::size_t rank, Integer weight) {
        const Value x = support_[rank];
        const Integer wx = x * weight;
        spread_ += weight * deviation(x, rank);
        total_.w += weight;
        total_.wx += wx;
        for (auto i = rank + 1; i < tree_.size(); i += i & -i) {
            tree_[i].w += weight;
            tree_[i].wx += wx;
        }
    }
public:
    Fenwick(const std::vector<Value>& support, std::size_t n)
        : support_(support), slots_(n), tree_(support.size() + 1) {
        if (support.size() >= 2) {
            step_ = Integer(support[1]) - support[0];
            for (std::size_t i = 2; i < support.size(); ++i)
                if (Integer(support[i]) - support[i - 1] != step_) { step_ = 0; break; }
        }
    }
    void erase(std::size_t i) {
        if (slots_[i].weight) change(slots_[i].rank, -Integer(slots_[i].weight));
        slots_[i] = {};
    }
    void set(std::size_t i, Value x, Weight w) {
        const auto rank = lower_rank(x);
        auto& old = slots_[i];
        if (old.weight && old.rank == rank) {
            if (old.weight != w) change(rank, Integer(w) - old.weight);
        } else {
            if (old.weight) change(old.rank, -Integer(old.weight));
            change(rank, w);
        }
        old = {rank, w};
    }
    long double score(Value y) const { return finish(deviation(y, lower_rank(y)), spread_, total_.w); }
};

template<class Engine> void apply(Engine& e, std::size_t i, Member m) {
    if (m.second) e.set(i, m.first, m.second);
    else e.erase(i);
}

long double pair_oracle(const std::vector<Member>& members, Value y) {
    Integer mass = 0, deviation = 0, spread = 0;
    for (std::size_t i = 0; i < members.size(); ++i) {
        const auto [x, w] = members[i];
        mass += w;
        deviation += w * std::abs(Integer(x) - y);
        for (std::size_t j = 0; j < i; ++j)
            spread += Integer(w) * members[j].second * std::abs(Integer(x) - members[j].first);
    }
    return finish(deviation, spread, mass);
}

void check(long double actual, long double expected) {
    if (!std::isfinite(actual) || std::abs(actual - expected) > 1e-10L * (1 + std::abs(expected)))
        throw std::runtime_error("score differs from independent calculation");
}

Workload workload(std::string name, std::size_t n, int d, int steps, int radius,
                  std::size_t score_every, bool irregular, bool equal_weights = false) {
    Workload w{std::move(name), {}, {}, {}, score_every};
    std::mt19937 gen(726381);
    Value value = -3 * d;
    for (int j = 0; j < d; ++j) {
        value += irregular ? 1 + static_cast<int>(gen() % 8) : 1;
        w.support.push_back(value);
    }
    std::vector<int> ranks(n);
    for (auto& index : ranks) {
        index = gen() % d;
        w.initial.emplace_back(w.support[index], equal_weights ? 1 : 1 + gen() % 4);
    }
    for (int j = 0; j < steps; ++j) {
        const auto slot = gen() % n;
        int& index = ranks[slot];
        index = radius ? std::clamp(index + static_cast<int>(gen() % (2*radius+1)) - radius, 0, d-1)
                       : static_cast<int>(gen() % d);
        // Slot zero remains active so a score is always defined, even for tiny n.
        const Weight weight = slot && gen() % 23 == 0 ? 0 : (equal_weights ? 1 : 1 + gen() % 4);
        const Value y = w.support[gen() % d] + static_cast<int>(gen() % 9) - 4;
        w.revisions.push_back({slot, {w.support[index], weight}, y});
    }
    return w;
}

template<class Engine> void precheck(const Workload& w, const std::vector<long double>& oracle) {
    Engine e(w.support, w.initial.size());
    for (std::size_t i = 0; i < w.initial.size(); ++i) apply(e, i, w.initial[i]);
    for (std::size_t i = 0; i < w.revisions.size(); ++i) {
        const auto& r = w.revisions[i];
        apply(e, r.slot, r.member);
        check(e.score(r.observation), oracle[i]);
    }
}

template<class Engine> double timed(const Workload& w, std::vector<long double>& scores) {
    Engine e(w.support, w.initial.size());
    for (std::size_t i = 0; i < w.initial.size(); ++i) apply(e, i, w.initial[i]);
    std::size_t count = 0, since = 0;
    const auto start = std::chrono::steady_clock::now();
    for (const auto& r : w.revisions) {
        apply(e, r.slot, r.member);
        if (++since == w.score_every) {
            scores[count++] = e.score(r.observation);
            since = 0;
        }
    }
    const auto stop = std::chrono::steady_clock::now();
    if (count != scores.size()) throw std::logic_error("score count mismatch");
    return std::chrono::duration<double, std::nano>(stop - start).count();
}

void run_case(const Workload& w, int repeats) {
    using Runner = double(*)(const Workload&, std::vector<long double>&);
    std::vector<std::pair<std::string,Runner>> engines{
        {"resort", timed<Resort>}, {"sorted_update", timed<Ordered>},
        {"histogram", timed<Histogram>}, {"fenwick", timed<Fenwick>}, {"recastcrps", timed<recastcrps::Ensemble>}};
    std::vector<long double> expected(w.revisions.size() / w.score_every);
    timed<Resort>(w, expected);
    std::vector<long double> scores(expected.size());
    std::mt19937 order(9324);
    for (int r = 1; r <= repeats; ++r) {
        std::shuffle(engines.begin(), engines.end(), order);
        for (const auto& [name, run] : engines) {
            double elapsed = run(w, scores);
            for (std::size_t i = 0; i < scores.size(); ++i) check(scores[i], expected[i]);
            std::cout << w.name << ',' << w.initial.size() << ',' << w.support.size()
                      << ',' << w.revisions.size() << ',' << w.score_every << ',' << scores.size()
                      << ',' << r << ',' << name << ',' << std::fixed << std::setprecision(2)
                      << elapsed << ',' << elapsed / w.revisions.size() << '\n';
        }
    }
}
} // namespace

int main(int argc, char** argv) {
    int repeats = argc > 1 ? std::stoi(argv[1]) : 5;
    if (repeats < 1 || argc > 2) throw std::invalid_argument("usage: benchmark [positive_repeats]");
    const auto tiny = workload("oracle", 13, 37, 3000, 0, 1, true);
    auto members = tiny.initial;
    std::vector<long double> oracle;
    for (const auto& r : tiny.revisions) {
        members[r.slot] = r.member;
        oracle.push_back(pair_oracle(members, r.observation));
    }
    precheck<Resort>(tiny, oracle);
    precheck<Ordered>(tiny, oracle);
    precheck<Histogram>(tiny, oracle);
    precheck<Fenwick>(tiny, oracle);
    precheck<recastcrps::Ensemble>(tiny, oracle);

    std::cout << "case,slots,support,revisions,score_every,scores,repeat,engine,elapsed_ns,ns_per_revision\n";
    run_case(workload("tiny", 8, 128, 120000, 3, 1, true), repeats);
    run_case(workload("small", 64, 256, 30000, 3, 1, true), repeats);
    run_case(workload("large_local", 4096, 4096, 2500, 3, 1, true), repeats);
    run_case(workload("equal_local", 4096, 4096, 2500, 3, 1, true, true), repeats);
    run_case(workload("large_global", 4096, 4096, 2500, 0, 1, true), repeats);
    run_case(workload("tied_lattice", 8192, 32, 1000, 2, 1, false), repeats);
    run_case(workload("dense_global", 4096, 4096, 2500, 0, 1, false), repeats);
    run_case(workload("score_per_64", 4096, 4096, 40000, 3, 64, true), repeats);
    run_case(workload("score_per_4096", 4096, 4096, 81920, 0, 4096, true), repeats);
}
