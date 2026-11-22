#include "recastcrps.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <random>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
using Int = std::int64_t;
struct Member { std::size_t rank; std::int32_t value; std::uint32_t weight; };
struct Edit { std::size_t slot; Member member; std::int32_t observation; };
struct Case {
    std::string name;
    std::vector<std::int32_t> support;
    std::vector<Member> initial;
    std::vector<Edit> edits;
};

Case generate(std::string name, std::size_t n, std::size_t d, std::size_t edits) {
    Case c{std::move(name), {}, {}, {}};
    std::mt19937 gen(82631);
    std::int32_t x = -static_cast<std::int32_t>(d);
    for (std::size_t i=0; i<d; ++i) {
        x += 1 + gen() % 8;
        c.support.push_back(x);
    }
    auto generate_member = [&]() {
        const auto rank = gen() % d;
        return Member{rank, c.support[rank], static_cast<std::uint32_t>(1 + gen()%4)};
    };
    for (std::size_t i=0; i<n; ++i) c.initial.push_back(generate_member());
    for (std::size_t i=0; i<edits; ++i) {
        const auto slot = gen() % n;
        auto m = generate_member();
        if (slot && gen()%23 == 0) m.weight = 0;
        c.edits.push_back({slot, m, c.support[gen()%d] + 2});
    }
    return c;
}

struct Moments {
    Int w = 0, wx = 0, ay = 0, p = 0;
    void add(Int x, Int weight, Int y) {
        p += weight * (x*w - wx);
        w += weight;
        wx += weight*x;
        ay += weight * std::abs(x-y);
    }
    Int distance(Int x, Int smaller_mass, Int smaller_moment) const {
        return x*(2*smaller_mass-w) + wx - 2*smaller_moment;
    }
    double without(const Member& m, Int ax, Int y) const {
        const Int kept = w-m.weight;
        if (!m.weight || !kept) return std::numeric_limits<double>::quiet_NaN();
        const auto new_ay = ay-m.weight*std::abs(Int(m.value)-y);
        const auto new_p = p-m.weight*ax;
        // Scenario bounds cover all integer intermediates including the numerator.
        return static_cast<double>(kept*new_ay-new_p) / static_cast<double>(kept*kept);
    }
};

// Batch baseline: sort once by member value and perform two prefix scans.
class SortOnce {
    std::vector<Member> members_;
    std::vector<std::size_t> order_;
public:
    explicit SortOnce(const Case& c) : members_(c.initial), order_(c.initial.size()) {}
    void edit(const Edit& e) { members_[e.slot] = e.member; }
    std::vector<double> loo(std::int32_t y) {
        std::iota(order_.begin(), order_.end(), 0);
        std::sort(order_.begin(), order_.end(), [&](auto a, auto b) {
            return members_[a].value < members_[b].value;
        });
        Moments totals;
        for (auto i : order_) totals.add(members_[i].value, members_[i].weight, y);
        std::vector<double> out(members_.size());
        Int w = 0, wx = 0;
        for (auto i : order_) {
            const auto& m = members_[i];
            out[i] = totals.without(m, totals.distance(m.value,w,wx), y);
            w += m.weight;
            wx += Int(m.weight)*m.value;
        }
        return out;
    }
};

// Batch baseline for a declared quantized support: two linear rank scans
// plus one member scan. Cache the rank of each member at insertion.
class RankScan {
    std::vector<Member> members_;
    const std::vector<std::int32_t>& support_;
    std::vector<Int> weights_, distances_;
public:
    explicit RankScan(const Case& c)
        : members_(c.initial), support_(c.support), weights_(c.support.size()), distances_(c.support.size()) {
        for (const auto& m : members_) weights_[m.rank] += m.weight;
    }
    void edit(const Edit& e) {
        weights_[members_[e.slot].rank] -= members_[e.slot].weight;
        // The caller supplies a value. Include conversion to its compressed rank.
        Member replacement = e.member;
        replacement.rank = std::lower_bound(support_.begin(),support_.end(),replacement.value)-support_.begin();
        members_[e.slot] = replacement;
        weights_[replacement.rank] += replacement.weight;
    }
    std::vector<double> loo(std::int32_t y) {
        Moments totals;
        for (std::size_t i=0; i<support_.size(); ++i) totals.add(support_[i],weights_[i],y);
        Int w = 0, wx = 0;
        for (std::size_t i=0; i<support_.size(); ++i) {
            distances_[i] = totals.distance(support_[i],w,wx);
            w += weights_[i];
            wx += Int(support_[i])*weights_[i];
        }
        std::vector<double> out(members_.size());
        for (std::size_t i=0; i<members_.size(); ++i)
            out[i] = totals.without(members_[i],distances_[members_[i].rank],y);
        return out;
    }
};

class Native {
protected:
    recastcrps::Ensemble state_;
public:
    explicit Native(const Case& c) : state_(c.support,c.initial.size()) {
        for (std::size_t i=0; i<c.initial.size(); ++i)
            state_.set(i,c.initial[i].value,c.initial[i].weight);
    }
    void edit(const Edit& e) {
        if (e.member.weight) state_.set(e.slot,e.member.value,e.member.weight);
        else state_.erase(e.slot);
    }
    std::vector<double> loo(std::int32_t y) { return state_.leave_one_out(y); }
};

class RoundTrip : public Native {
    std::vector<Member> members_;
public:
    explicit RoundTrip(const Case& c) : Native(c), members_(c.initial) {}
    void edit(const Edit& e) { Native::edit(e); members_[e.slot] = e.member; }
    std::vector<double> loo(std::int32_t y) {
        std::vector<double> out(members_.size(),std::numeric_limits<double>::quiet_NaN());
        if (state_.count() < 2) return out;
        for (std::size_t i=0; i<members_.size(); ++i) {
            const auto& m = members_[i];
            if (!m.weight) continue;
            state_.erase(i);
            out[i] = static_cast<double>(state_.score(y));
            state_.set(i,m.value,m.weight);
        }
        return out;
    }
};

void check(double actual, double expected) {
    if (std::isnan(actual) && std::isnan(expected)) return;
    if (!std::isfinite(actual) || !std::isfinite(expected) ||
        std::abs(actual-expected) > 1e-12*(1+std::abs(expected)))
        throw std::runtime_error("leave-one-out results differ");
}

// One explicit weighted-pair score per held-out member, with no rank algebra.
std::vector<double> quadratic(const std::vector<Member>& members, Int y) {
    std::vector<double> out(members.size());
    for (std::size_t h=0; h<members.size(); ++h) {
        Int w=0, a=0, p=0;
        for (std::size_t i=0; i<members.size(); ++i) {
            if (i==h) continue;
            const auto& m = members[i];
            w += m.weight;
            a += m.weight * std::abs(Int(m.value)-y);
            for (std::size_t j=0; j<i; ++j)
                if (j!=h) p += Int(m.weight)*members[j].weight*std::abs(Int(m.value)-members[j].value);
        }
        out[h] = w && members[h].weight ? double(w*a-p)/double(w*w) : std::numeric_limits<double>::quiet_NaN();
    }
    return out;
}

template<class Engine> void verify(const Case& c, const std::vector<std::vector<double>>& expected) {
    Engine e(c);
    for (std::size_t j=0; j<c.edits.size(); ++j) {
        e.edit(c.edits[j]);
        const auto scores = e.loo(c.edits[j].observation);
        if (scores.size()!=c.initial.size()) throw std::runtime_error("result length differs");
        for (std::size_t i=0; i<scores.size(); ++i) check(scores[i],expected[j][i]);
    }
}

volatile double sink = 0;
template<class Engine> double timed(const Case& c) {
    Engine e(c);
    // Match consumers that use selected slots. All slots are verified outside timing.
    double checksum=0;
    const auto begin = std::chrono::steady_clock::now();
    for (const auto& edit : c.edits) {
        e.edit(edit);
        const auto scores = e.loo(edit.observation);
        checksum += scores[0];
    }
    const auto end = std::chrono::steady_clock::now();
    sink = checksum;
    return std::chrono::duration<double,std::nano>(end-begin).count();
}

void run(const Case& c, int repeats, bool oracle=false) {
    std::vector<std::vector<double>> expected;
    if (oracle) {
        auto members=c.initial;
        for (const auto& rev : c.edits) {
            members[rev.slot] = rev.member;
            expected.push_back(quadratic(members,rev.observation));
        }
    } else {
        SortOnce s(c);
        for (const auto& rev : c.edits) { s.edit(rev); expected.push_back(s.loo(rev.observation)); }
    }
    verify<SortOnce>(c,expected);
    verify<RankScan>(c,expected);
    verify<Native>(c,expected);
    verify<RoundTrip>(c,expected);
    if (oracle) return;
    using Runner = double(*)(const Case&);
    std::vector<std::pair<std::string,Runner>> engines{
        {"sort_once",timed<SortOnce>}, {"rank_scan",timed<RankScan>},
        {"erase_restore",timed<RoundTrip>}, {"recastcrps",timed<Native>}};
    std::mt19937 gen(9332);
    for (int r=1; r<=repeats; ++r) {
        std::shuffle(engines.begin(),engines.end(),gen);
        for (const auto& [name,f] : engines) {
            const double elapsed=f(c);
            std::cout << c.name << ',' << c.initial.size() << ',' << c.support.size() << ',' << c.edits.size()
                      << ',' << r << ',' << name << ',' << std::fixed << std::setprecision(2) << elapsed
                      << ',' << elapsed / c.edits.size() << '\n';
        }
    }
}
} // namespace

int main(int argc,char** argv) {
    const int repeats = argc>1 ? std::stoi(argv[1]) : 5;
    if (repeats<1 || argc>2) throw std::invalid_argument("usage: attribution [positive_repeats]");
    run(generate("oracle",11,19,200),1,true);
    std::cout << "case,slots,support,revisions,repeat,engine,elapsed_ns,ns_per_all_member_query\n";
    run(generate("small",16,64,2000),repeats);
    run(generate("many_ties",4096,32,200),repeats);
    run(generate("large",4096,4096,160),repeats);
    run(generate("sparse_active",64,65536,160),repeats);
}
