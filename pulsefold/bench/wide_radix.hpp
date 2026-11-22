#pragma once
#include "baselines.hpp"

namespace pulsefold::bench {

// A four-bit radix queue trades extra bucket storage for fewer redistributions
// than the one-bit LazyRadix. A two-level occupancy bitmap finds the lowest
// occupied bucket, and a cached lower bound serves clock polls between checks.
class WideRadix {
    using Id = detail::Id;
    using Time = detail::Time;
    using Slot = std::size_t;
    static constexpr Slot absent = std::numeric_limits<Slot>::max();
    struct Timer {
        Time check = 0;
        Slot slot = absent;
    };
    struct Bucket {
        std::vector<Id> ids;
        Time earliest = detail::max_time;
    };

    std::vector<detail::Feed> feeds_;
    std::vector<Timer> timers_;
    std::array<Bucket, 256> buckets_;
    std::vector<Id> fired_;
    std::array<std::uint64_t, 4> occupied_{};
    unsigned pages_ = 0;
    Time hint_ = detail::max_time;
    Time base_ = 0;
    Time now_ = 0;

    void mark(unsigned index) {
        occupied_[index >> 6] |= std::uint64_t{1} << (index & 63);
        pages_ |= 1u << (index >> 6);
    }
    void unmark(unsigned index) {
        auto& word = occupied_[index >> 6];
        word &= ~(std::uint64_t{1} << (index & 63));
        if (!word) pages_ &= ~(1u << (index >> 6));
    }
    // Identify the highest differing nibble, then its value. Since check >=
    // base_, this digit is strictly larger than the corresponding base digit.
    unsigned slot(Time check) const {
        const auto difference = check ^ base_;
        if (!difference) return 0;
        const unsigned shift = (width(difference) - 1) & ~3u;
        return shift * 4 + (static_cast<unsigned>(check >> shift) & 15u);
    }

    static unsigned width(std::uint64_t value) {
#if defined(__GNUC__) || defined(__clang__)
        return value ? 64u - static_cast<unsigned>(__builtin_clzll(value)) : 0u;
#else
        unsigned bits = 0;
        while (value) { ++bits; value >>= 1; }
        return bits;
#endif
    }

    static unsigned first_set(std::uint64_t mask) {
#if defined(__GNUC__) || defined(__clang__)
        return static_cast<unsigned>(__builtin_ctzll(mask));
#else
        unsigned bit = 0;
        while (!(mask & 1)) { ++bit; mask >>= 1; }
        return bit;
#endif
    }

    void insert(Id id, Time check) {
        const unsigned index = slot(check);
        auto& bucket = buckets_[index];
        if (bucket.ids.empty()) {
            bucket.earliest = check;
            if (index) mark(index);
        } else {
            bucket.earliest = std::min(bucket.earliest, check);
        }
        hint_ = std::min(hint_, check);
        timers_[id] = {check, bucket.ids.size()};
        bucket.ids.push_back(id);
    }

    void remove(Id id) {
        const auto timer = timers_[id];
        const unsigned index = slot(timer.check);
        auto& bucket = buckets_[index];
        const Id last = bucket.ids.back();
        bucket.ids[timer.slot] = last;
        timers_[last].slot = timer.slot;
        bucket.ids.pop_back();
        timers_[id].slot = absent;
        if (index && bucket.ids.empty()) unmark(index);
        // The cached minimum may now be early. A due check verifies it.
    }

    // All keys in the first occupied nonzero bucket precede all keys in higher
    // buckets. Redistributing about its minimum strictly reduces bucket index.
    bool prepare(Time now) {
        if (!buckets_[0].ids.empty()) return true;
        if (!pages_) {
            hint_ = detail::max_time;
            return false;
        }
        const unsigned page = first_set(pages_);
        const unsigned index = page * 64 + first_set(occupied_[page]);
        auto& bucket = buckets_[index];
        if (bucket.earliest > now) {
            hint_ = bucket.earliest;
            return false;
        }
        Time minimum = detail::max_time;
        for (Id id : bucket.ids) minimum = std::min(minimum, timers_[id].check);
        bucket.earliest = minimum;
        if (minimum > now) {
            hint_ = minimum;
            return false;
        }
        base_ = minimum;
        unmark(index);
        for (Id id : bucket.ids) insert(id, timers_[id].check);
        bucket.ids.clear();
        return true;
    }

public:
    explicit WideRadix(const std::vector<std::vector<Time>>& schema)
        : feeds_(detail::make_feeds(schema)), timers_(schema.size()) {}

    template<class Emit>
    void advance(Time now, Emit&& emit) {
        if (now < now_) throw std::invalid_argument("time must be nondecreasing");
        now_ = now;
        if (now < hint_) return;
        while (prepare(now)) {
            const Time check = base_;
            fired_.clear();
            for (Id id : buckets_[0].ids) {
                timers_[id].slot = absent;
                Time due;
                if (!feeds_[id].next(due)) continue;
                if (due == check) fired_.push_back(id);
                else insert(id, due);
            }
            buckets_[0].ids.clear();
            // Deferred checks have no observable order. Only simultaneous real
            // expiries need sorting to preserve (timestamp, feed, subscriber).
            std::sort(fired_.begin(), fired_.end());
            for (Id id : fired_) {
                auto& feed = feeds_[id];
                const Time ttl = check - feed.last;
                do {
                    emit(check, id, feed.levels[feed.stale].id, false);
                    ++feed.stale;
                } while (feed.stale < feed.levels.size() && feed.levels[feed.stale].ttl == ttl);
                Time due;
                if (feed.next(due)) insert(id, due);
            }
        }
    }

    template<class Emit>
    void observe(Id feed, Time now, Emit&& emit) {
        if (feed >= feeds_.size()) throw std::out_of_range("unknown feed");
        advance(now, emit);
        auto& state = feeds_[feed];
        // In the healthy armed state the next deadline can only move later.
        if (state.armed && state.stale == 0) {
            state.last = now;
            return;
        }
        state.refresh(feed, now, emit);
        Time due;
        const bool schedulable = state.next(due);
        if (timers_[feed].slot != absent) {
            if (schedulable && timers_[feed].check <= due) return;
            remove(feed);
        }
        if (schedulable) insert(feed, due);
    }
};

}  // namespace pulsefold::bench
