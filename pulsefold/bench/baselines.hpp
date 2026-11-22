#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <tuple>
#include <utility>
#include <vector>

namespace pulsefold::bench {

namespace detail {

using Time = std::uint64_t;
using Id = std::uint32_t;
constexpr Time max_time = std::numeric_limits<Time>::max();

struct Subscriber {
    Time ttl;
    Id id;
};

// All subscribers at a feed share its last observation. The stale subscribers
// form a prefix ordered by (TTL, subscriber id).
struct Feed {
    std::vector<Subscriber> levels;
    std::size_t stale = 0;
    Time last = 0;
    bool armed = false;

    bool next(Time& deadline) const {
        if (!armed || stale == levels.size() || levels[stale].ttl > max_time - last) {
            return false;
        }
        deadline = last + levels[stale].ttl;
        return true;
    }

    template<class Emit>
    void refresh(Id feed, Time now, Emit& emit) {
        for (std::size_t i = 0; i < stale; ++i) {
            emit(now, feed, levels[i].id, true);
        }
        last = now;
        stale = 0;
        armed = true;
    }
};

inline std::vector<Feed> make_feeds(const std::vector<std::vector<Time>>& schema) {
    if (schema.size() > std::numeric_limits<Id>::max()) {
        throw std::length_error("too many feeds");
    }
    std::vector<Feed> feeds(schema.size());
    for (std::size_t f = 0; f < schema.size(); ++f) {
        const auto& ttls = schema[f];
        auto& levels = feeds[f].levels;
        if (ttls.size() > std::numeric_limits<Id>::max()) {
            throw std::length_error("too many subscribers");
        }
        levels.reserve(ttls.size());
        for (std::size_t i = 0; i < ttls.size(); ++i) {
            if (ttls[i] == 0) throw std::invalid_argument("TTL must be positive");
            levels.push_back({ttls[i], static_cast<Id>(i)});
        }
        std::sort(levels.begin(), levels.end(), [](Subscriber a, Subscriber b) {
            return std::tie(a.ttl, a.id) < std::tie(b.ttl, b.id);
        });
    }
    return feeds;
}

}  // namespace detail

// One addressable timer per feed. The heap is preallocated; renewals overwrite
// a deadline in place and sift only in the affected direction.
class EagerFeedHeap {
    using Id = detail::Id;
    using Time = detail::Time;
    using Slot = std::size_t;
    static constexpr Slot absent = std::numeric_limits<Slot>::max();

    struct Timer {
        Time due;
        Id feed;
    };
    std::vector<detail::Feed> feeds_;
    std::vector<Timer> heap_;
    std::vector<Slot> positions_;
    Time now_ = 0;

    static bool before(Timer a, Timer b) {
        return std::tie(a.due, a.feed) < std::tie(b.due, b.feed);
    }
    void place(Slot slot, Timer timer) {
        heap_[slot] = timer;
        positions_[timer.feed] = slot;
    }
    void sift_up(Slot slot, Timer timer) {
        while (slot != 0) {
            const Slot parent = (slot - 1) / 2;
            if (!before(timer, heap_[parent])) break;
            place(slot, heap_[parent]);
            slot = parent;
        }
        place(slot, timer);
    }
    void sift_down(Slot slot, Timer timer) {
        while (slot < heap_.size() / 2) {
            Slot child = 2 * slot + 1;
            if (child + 1 < heap_.size() && before(heap_[child + 1], heap_[child])) ++child;
            if (!before(heap_[child], timer)) break;
            place(slot, heap_[child]);
            slot = child;
        }
        place(slot, timer);
    }
    void replace(Slot slot, Timer old, Timer timer) {
        if (before(timer, old)) sift_up(slot, timer);
        else if (before(old, timer)) sift_down(slot, timer);
        else place(slot, timer);
    }
    void schedule(Id feed) {
        Time due;
        const Slot slot = positions_[feed];
        if (feeds_[feed].next(due)) {
            const Timer timer{due, feed};
            if (slot == absent) {
                heap_.push_back(timer);
                sift_up(heap_.size() - 1, timer);
            } else if (heap_[slot].due != due) {
                replace(slot, heap_[slot], timer);
            }
        } else if (slot != absent) {
            const Timer old = heap_[slot];
            const Timer last = heap_.back();
            heap_.pop_back();
            positions_[feed] = absent;
            if (slot < heap_.size()) replace(slot, old, last);
        }
    }

public:
    explicit EagerFeedHeap(const std::vector<std::vector<Time>>& schema)
        : feeds_(detail::make_feeds(schema)), positions_(schema.size(), absent) {
        heap_.reserve(schema.size());
    }

    template<class Emit>
    void advance(Time now, Emit&& emit) {
        if (now < now_) throw std::invalid_argument("time must be nondecreasing");
        now_ = now;
        while (!heap_.empty() && heap_.front().due <= now) {
            const Timer head = heap_.front();
            auto& feed = feeds_[head.feed];
            const Time ttl = head.due - feed.last;
            do {
                emit(head.due, head.feed, feed.levels[feed.stale].id, false);
                ++feed.stale;
            } while (feed.stale < feed.levels.size() && feed.levels[feed.stale].ttl == ttl);
            schedule(head.feed);
        }
    }

    template<class Emit>
    void observe(Id feed, Time now, Emit&& emit) {
        if (feed >= feeds_.size()) throw std::out_of_range("unknown feed");
        advance(now, emit);
        feeds_[feed].refresh(feed, now, emit);
        schedule(feed);
    }
};

// Polling baseline with a cached conservative minimum. Polls are O(1) before
// that time; when it arrives, one sequential scan computes the new minimum.
class FeedScan {
    using Id = detail::Id;
    using Time = detail::Time;
    using Event = std::tuple<Time, Id, Id>;
    std::vector<detail::Feed> feeds_;
    std::vector<Event> expired_;
    Time earliest_ = detail::max_time;
    Time now_ = 0;

public:
    explicit FeedScan(const std::vector<std::vector<Time>>& schema)
        : feeds_(detail::make_feeds(schema)) {}

    template<class Emit>
    void advance(Time now, Emit&& emit) {
        if (now < now_) throw std::invalid_argument("time must be nondecreasing");
        now_ = now;
        if (now < earliest_) return;
        earliest_ = detail::max_time;
        expired_.clear();
        for (std::size_t i = 0; i < feeds_.size(); ++i) {
            auto& feed = feeds_[i];
            const Id id = static_cast<Id>(i);
            Time due;
            while (feed.next(due)) {
                if (due > now) {
                    earliest_ = std::min(earliest_, due);
                    break;
                }
                expired_.emplace_back(due, id, feed.levels[feed.stale++].id);
            }
        }
        std::sort(expired_.begin(), expired_.end());
        for (const auto& [time, feed, sub] : expired_) emit(time, feed, sub, false);
    }

    template<class Emit>
    void observe(Id feed, Time now, Emit&& emit) {
        if (feed >= feeds_.size()) throw std::out_of_range("unknown feed");
        advance(now, emit);
        feeds_[feed].refresh(feed, now, emit);
        Time due;
        if (feeds_[feed].next(due)) earliest_ = std::min(earliest_, due);
    }
};

// Exact 64-bit monotone radix queue. Refreshes leave a later effective expiry
// behind its existing early check. After a recovery, a shorter TTL can move
// that check earlier; a per-feed vector slot makes removal constant time.
class LazyRadix {
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
    std::array<Bucket, 65> buckets_;
    std::vector<Id> fired_;
    std::uint64_t nonzero_ = 0;  // bit n means bucket n + 1 has entries
    Time base_ = 0;
    Time now_ = 0;

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
        const unsigned index = width(check ^ base_);
        auto& bucket = buckets_[index];
        if (bucket.ids.empty()) {
            bucket.earliest = check;
            if (index) nonzero_ |= std::uint64_t{1} << (index - 1);
        } else {
            bucket.earliest = std::min(bucket.earliest, check);
        }
        timers_[id] = {check, bucket.ids.size()};
        bucket.ids.push_back(id);
    }

    void remove(Id id) {
        const auto timer = timers_[id];
        const unsigned index = width(timer.check ^ base_);
        auto& bucket = buckets_[index];
        const Id last = bucket.ids.back();
        bucket.ids[timer.slot] = last;
        timers_[last].slot = timer.slot;
        bucket.ids.pop_back();
        timers_[id].slot = absent;
        if (index && bucket.ids.empty()) nonzero_ &= ~(std::uint64_t{1} << (index - 1));
        // The cached minimum may now be early. A due check verifies it.
    }

    // All keys in the first occupied nonzero bucket precede all keys in higher
    // buckets. Redistributing about its minimum strictly reduces bucket index.
    bool prepare(Time now) {
        if (!buckets_[0].ids.empty()) return true;
        if (!nonzero_) return false;
        const unsigned index = 1 + first_set(nonzero_);
        auto& bucket = buckets_[index];
        if (bucket.earliest > now) return false;
        Time minimum = detail::max_time;
        for (Id id : bucket.ids) minimum = std::min(minimum, timers_[id].check);
        bucket.earliest = minimum;
        if (minimum > now) return false;
        base_ = minimum;
        nonzero_ &= ~(std::uint64_t{1} << (index - 1));
        for (Id id : bucket.ids) insert(id, timers_[id].check);
        bucket.ids.clear();
        return true;
    }

public:
    explicit LazyRadix(const std::vector<std::vector<Time>>& schema)
        : feeds_(detail::make_feeds(schema)), timers_(schema.size()) {}

    template<class Emit>
    void advance(Time now, Emit&& emit) {
        if (now < now_) throw std::invalid_argument("time must be nondecreasing");
        now_ = now;
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
        const bool healthy = state.armed && state.stale == 0;
        state.refresh(feed, now, emit);
        if (healthy) return;
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
