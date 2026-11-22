#include "recastcrps.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <random>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
using X = std::int32_t;
using I = std::int64_t;
using Point = std::pair<X,double>;
using Result = std::pair<Point,Point>;
struct Tick { std::size_t slot; X value, y; };
struct Case {
    std::string name;
    bool changes;
    std::vector<X> support, initial;
    std::vector<Tick> ticks;
};

Case generate(std::string name, std::size_t n, std::size_t d, int queries, bool changes) {
    Case c{std::move(name),changes,{},{},{}};
    std::mt19937 gen(67012);
    X x = -static_cast<X>(d);
    for (std::size_t i=0; i<d; ++i) { x+=1+gen()%5; c.support.push_back(x); }
    for (std::size_t i=0; i<n; ++i) c.initial.push_back(c.support[gen()%d]);
    for (int i=0; i<queries; ++i)
        c.ticks.push_back({gen()%n,c.support[gen()%d],c.support[gen()%d]+static_cast<int>(gen()%9)-4});
    return c;
}

void consider(Result& r, Point p, bool first) {
    if (first || p.second<r.first.second || (p.second==r.first.second && p.first<r.first.first))
        r.first=p;
    if (first || p.second>r.second.second || (p.second==r.second.second && p.first<r.second.first))
        r.second=p;
}

// For unit weights, rank by a common denominator (n-1)^2. Bound checks in
// this benchmark allow int64 arithmetic throughout the independent baselines.
Result candidates(const std::array<X,4>& xs, I n, X y, I spread, I sum, I left_n, I left_sum) {
    const I kept = n-1, slope = 2*left_n-n;
    const I ay = y*slope+sum-2*left_sum;
    const auto h = [&](X x, I ax) { return ax-kept*std::abs(I(x)-y); };
    const auto scored = [&](X x, I contribution) {
        return Point{x,double(kept*ay-spread+contribution)/double(kept*kept)};
    };
    const auto [lo,hi,before,after]=xs;
    const I h_lo=h(lo,sum-n*lo), h_hi=h(hi,n*hi-sum);
    const I h_before=h(before,ay+(I(before)-y)*slope);
    const I h_after=h(after,ay+(I(after)-y)*slope);
    return {h_lo<=h_hi ? scored(lo,h_lo) : scored(hi,h_hi),
            h_before>=h_after ? scored(before,h_before) : scored(after,h_after)};
}

class Native {
protected:
    recastcrps::Ensemble state_;
public:
    explicit Native(const Case& c) : state_(c.support,c.initial.size()) {
        for (std::size_t i=0;i<c.initial.size();++i) state_.set(i,c.initial[i]);
    }
    void set(std::size_t i,X x) { state_.set(i,x); }
    Result query(X y) const { return state_.withdrawal_extremes(y); }
};

class Exhaustive : public Native {
    std::vector<X> members_;
public:
    explicit Exhaustive(const Case& c) : Native(c), members_(c.initial) {}
    void set(std::size_t i,X x) { Native::set(i,x); members_[i]=x; }
    Result query(X y) const {
        const auto scores=state_.leave_one_out(y);
        Result out{};
        for (std::size_t i=0;i<members_.size();++i) consider(out,{members_[i],scores[i]},i==0);
        return out;
    }
};

class StaticPrefix {
    std::vector<X> members_, ordered_;
    std::vector<I> prefix_;
    I spread_=0;
    bool dirty_=true;
    void build() {
        ordered_=members_;
        std::sort(ordered_.begin(),ordered_.end());
        spread_=0;
        for (std::size_t i=0;i<ordered_.size();++i) {
            spread_+=I(i)*ordered_[i]-prefix_[i];
            prefix_[i+1]=prefix_[i]+ordered_[i];
        }
        dirty_=false;
    }
public:
    explicit StaticPrefix(const Case& c) : members_(c.initial), prefix_(c.initial.size()+1) { build(); }
    void set(std::size_t i,X x) {
        if (members_[i]!=x) { members_[i]=x; dirty_=true; }
    }
    Result query(X y) {
        if (dirty_) build();
        const auto above=std::lower_bound(ordered_.begin(),ordered_.end(),y);
        const X nearest_hi=above==ordered_.end() ? ordered_.back() : *above;
        const X nearest_lo=above==ordered_.begin() ? ordered_.front() : *std::prev(above);
        return candidates({ordered_.front(),ordered_.back(),nearest_lo,nearest_hi},
                          members_.size(),y,spread_,prefix_.back(),above-ordered_.begin(),
                          prefix_[above-ordered_.begin()]);
    }
};

// Keep active ranks in std::set for predecessor/successor, and incrementally
// updated first moments in a Fenwick tree. This comparator independently
// implements both the endpoint shortcut and dynamic scoring.
class DynamicEndpoints {
    struct Moments { I n=0, sum=0; };
    const std::vector<X>& support_;
    std::vector<std::size_t> ranks_, lower_;
    std::vector<std::size_t> counts_;
    std::vector<Moments> tree_;
    std::set<std::size_t> active_;
    Moments total_;
    I spread_=0;

    std::size_t rank(X x) const {
        if (x<=support_.front()) return 0;
        if (x>support_.back()) return support_.size();
        return lower_[I(x)-support_.front()];
    }
    Moments prefix(std::size_t r) const {
        Moments left;
        for (auto i=r;i;i&=i-1) { left.n+=tree_[i].n; left.sum+=tree_[i].sum; }
        return left;
    }
    void adjust(std::size_t r,I delta) {
        const X x=support_[r];
        const auto left=prefix(r);
        spread_+=delta*(I(x)*(2*left.n-total_.n)+total_.sum-2*left.sum);
        total_.n+=delta; total_.sum+=delta*x;
        for (auto i=r+1;i<tree_.size();i+=i&-i) { tree_[i].n+=delta; tree_[i].sum+=delta*x; }
        if (delta>0) { if (counts_[r]++==0) active_.insert(r); }
        else if (--counts_[r]==0) active_.erase(r);
    }
public:
    explicit DynamicEndpoints(const Case& c)
        : support_(c.support), ranks_(c.initial.size()), lower_(I(c.support.back())-c.support.front()+1),
          counts_(c.support.size()), tree_(c.support.size()+1) {
        std::size_t i=0;
        for (std::size_t k=0;k<lower_.size();++k) {
            while (I(support_[i])-support_.front()<I(k)) ++i;
            lower_[k]=i;
        }
        for (std::size_t j=0;j<ranks_.size();++j) { ranks_[j]=rank(c.initial[j]); adjust(ranks_[j],1); }
    }
    void set(std::size_t slot,X x) {
        const auto next=rank(x), old=ranks_[slot];
        if (old!=next) { adjust(old,-1); adjust(next,1); ranks_[slot]=next; }
    }
    Result query(X y) const {
        const auto split=rank(y);
        const auto i=active_.lower_bound(split);
        const auto left=prefix(split);
        const auto hi=i==active_.end() ? *active_.rbegin() : *i;
        const auto lo=i==active_.begin() ? *active_.begin() : *std::prev(i);
        return candidates({support_[*active_.begin()],support_[*active_.rbegin()],support_[lo],support_[hi]},
                          total_.n,y,spread_,total_.sum,left.n,left.sum);
    }
};

void check(const Point& a,const Point& b) {
    if (a.first!=b.first || !std::isfinite(a.second) || std::abs(a.second-b.second)>1e-12*(1+std::abs(b.second)))
        throw std::runtime_error("withdrawal extrema disagree");
}
// Literal held-out pairs, exact integral numerator and shared denominator.
Result brute(const std::vector<X>& members,X y) {
    Result result{};
    I k=members.size()-1;
    for (std::size_t h=0;h<members.size();++h) {
        I a=0,p=0;
        for (std::size_t i=0;i<members.size();++i) if (i!=h) {
            a+=std::abs(I(members[i])-y);
            for (std::size_t j=0;j<i;++j) if (j!=h) p+=std::abs(I(members[i])-members[j]);
        }
        consider(result,{members[h],double(k*a-p)/double(k*k)},h==0);
    }
    return result;
}

void check_all_small() {
    // Exhaust all ordered 2..5-slot tuples on four coordinates, including ties,
    // odd/even counts, gaps, out-of-support observations, and two-member cases.
    for (std::size_t n=2,configs=16;n<=5;++n,configs*=4) {
        Case c{"proof_check",false,{-5,-1,2,6},std::vector<X>(n),{}};
        for (std::size_t j=0;j<configs;++j) {
            auto bits=j;
            for (auto& x:c.initial) { x=c.support[bits%4]; bits/=4; }
            Native state(c); DynamicEndpoints dynamic(c); StaticPrefix fixed(c);
            for (X y=-7;y<=8;++y) {
                const auto ref=brute(c.initial,y);
                for (const auto& p:{state.query(y),dynamic.query(y),fixed.query(y)}) {
                    check(p.first,ref.first); check(p.second,ref.second);
                }
            }
        }
    }
}

template<class Engine> void verify(const Case& c,const std::vector<Result>& out) {
    Engine e(c);
    for (std::size_t i=0;i<c.ticks.size();++i) {
        const auto& t=c.ticks[i];
        if (c.changes) e.set(t.slot,t.value);
        const auto r=e.query(t.y);
        check(r.first,out[i].first); check(r.second,out[i].second);
    }
}
volatile double sink=0;
template<class Engine> double timed(const Case& c) {
    Engine e(c);
    double checksum=0;
    const auto start=std::chrono::steady_clock::now();
    for (const auto& t:c.ticks) {
        if (c.changes) e.set(t.slot,t.value);
        const auto r=e.query(t.y);
        checksum+=r.first.first+r.first.second+r.second.first+r.second.second;
    }
    const auto stop=std::chrono::steady_clock::now(); sink=checksum;
    return std::chrono::duration<double,std::nano>(stop-start).count();
}

void run(const Case& c,int repeats) {
    Exhaustive all(c);
    std::vector<Result> expected;
    for (const auto& t:c.ticks) {
        if (c.changes) all.set(t.slot,t.value);
        expected.push_back(all.query(t.y));
    }
    verify<Native>(c,expected); verify<DynamicEndpoints>(c,expected); verify<StaticPrefix>(c,expected);
    using Runner=double(*)(const Case&);
    std::vector<std::pair<std::string,Runner>> engines{
        {"all_loo",timed<Exhaustive>},{"static_prefix",timed<StaticPrefix>},
        {"ordered_fenwick",timed<DynamicEndpoints>},{"recastcrps",timed<Native>}};
    std::mt19937 gen(38392);
    for (int r=1;r<=repeats;++r) {
        std::shuffle(engines.begin(),engines.end(),gen);
        for (const auto& [name,f]:engines) {
            const double elapsed=f(c);
            std::cout<<c.name<<','<<c.initial.size()<<','<<c.support.size()<<','<<c.ticks.size()
                     <<','<<r<<','<<name<<','<<std::fixed<<std::setprecision(2)<<elapsed
                     <<','<<elapsed/c.ticks.size()<<'\n';
        }
    }
}
} // namespace

int main(int argc,char** argv) {
    const int repeats=argc>1 ? std::stoi(argv[1]) : 5;
    if (repeats<1 || argc>2) throw std::invalid_argument("usage: extremes [positive_repeats]");
    check_all_small();
    std::cout<<"case,slots,support,queries,repeat,engine,elapsed_ns,ns_per_query\n";
    run(generate("small_dynamic",64,128,6000,true),repeats);
    run(generate("large_dynamic",4096,4096,2000,true),repeats);
    run(generate("many_ties_dynamic",8192,32,1200,true),repeats);
    run(generate("large_static",4096,4096,2000,false),repeats);
}
