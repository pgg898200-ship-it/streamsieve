#ifndef PULSEFOLD_HPP
#define PULSEFOLD_HPP

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

namespace pulsefold {

// Immutable, nonempty slice of a compiled policy. The view is valid until
// that policy's owning monitor is destroyed or its storage is replaced.
class SubscriberView {
public:
    const std::uint32_t* begin() const noexcept { return first_; }
    const std::uint32_t* end() const noexcept { return first_ + size_; }
    std::size_t size() const noexcept { return size_; }
private:
    friend class Monitor;
    SubscriberView(const std::uint32_t* first, std::size_t size) noexcept
        : first_(first), size_(size) {}
    const std::uint32_t* first_;
    std::size_t size_;
};

// Single-owner event-time monitor. Drive it in nondecreasing timestamp order.
// A callback receives (timestamp, feed_id, subscriber_id, fresh) and must
// neither throw nor reenter the monitor. Valid updates and dispatch allocate no monitor storage.
// Initially feeds are unknown. Their first observation arms all subscribers
// silently. A deadline equal to an observation time expires before the refresh.
// Among simultaneous expiries, order is (feed_id, subscriber_id). Recoveries
// for one observation are emitted in (TTL, subscriber_id) order.
class Monitor {
public:
    using Config = std::vector<std::vector<std::uint64_t>>;

    // In this form a feed's index is also its policy index.
    explicit Monitor(const Config& per_feed_ttls) {
        compile(per_feed_ttls);
        feeds_.reserve(policies_.size());
        for (std::size_t i = 0; i < policies_.size(); ++i) {
            feeds_.push_back(Feed{0, static_cast<std::uint32_t>(i), 0});
        }
        build_tiles();
    }

    // Each policy lists TTLs in subscriber-ID order. Share a policy across
    // feeds to store its thresholds and subscribers just once.
    Monitor(const Config& policies, const std::vector<std::uint32_t>& feed_policy_ids) {
        compile(policies);
        check_size(feed_policy_ids.size());
        feeds_.reserve(feed_policy_ids.size());
        for (const auto id : feed_policy_ids) {
            if (id >= policies_.size()) throw std::out_of_range("unknown policy ID");
            feeds_.push_back(Feed{0, id, 0});
        }
        build_tiles();
    }

    Monitor(const Monitor&) = delete;
    Monitor& operator=(const Monitor&) = delete;
    Monitor(Monitor&& other) noexcept { *this = std::move(other); }
    Monitor& operator=(Monitor&& other) noexcept {
        if (this == &other) return *this;
        policies_ = std::move(other.policies_);
        feeds_ = std::move(other.feeds_);
        tiles_ = std::move(other.tiles_);
        armed_ = std::exchange(other.armed_, 0);
        earliest_ = std::exchange(other.earliest_, inf);
        watermark_ = std::exchange(other.watermark_, 0);
        return *this;
    }

    // Earliest scheduled check. A healthy refresh can postpone the actual
    // deadline, so this hint may cause an early wake but never a late wake.
    // Empty means no currently armed, representable check.
    std::optional<std::uint64_t> next_wake() const noexcept {
        if (!armed_) return std::nullopt;
        return earliest_;
    }

    template <class Emit>
    void observe(std::uint32_t id, std::uint64_t now, Emit&& emit) {
        observe_groups(id, now, [&](auto time, auto feed, SubscriberView group, bool fresh) {
            for (auto subscriber : group) emit(time, feed, subscriber, fresh);
        });
    }

    template <class Emit>
    void advance(std::uint64_t now, Emit&& emit) {
        advance_groups(now, [&](auto time, auto feed, SubscriberView group, bool fresh) {
            for (auto subscriber : group) emit(time, feed, subscriber, fresh);
        });
    }

    // The grouped API avoids expanding member IDs: one immutable view per
    // expired TTL group, or one contiguous prefix for an observed recovery.
    template <class Emit>
    void observe_groups(std::uint32_t id, std::uint64_t now, Emit&& emit) {
        if (id >= feeds_.size()) throw std::out_of_range("unknown feed ID");
        advance_groups(now, emit);
        auto& feed = feeds_[id];
        // Renewing a healthy feed can only move its deadline later. Replace
        // one exact leaf and let earlier ancestor hints stand until a check.
        if (feed.cursor == 0 && feed.first_ttl) {
            const auto ttl = feed.first_ttl;
            if (fits(now, ttl)) {
                tiles_[0][id] = now + ttl;
            } else {
                feed.first_ttl = 0;
                --armed_;
                tiles_[0][id] = inf;
            }
            return;
        }
        const auto& policy = policies_[feed.policy];
        if (policy.groups.empty()) return;
        if (feed.cursor != 0) {
            const auto count = policy.groups[feed.cursor - 1].end;
            emit(now, id, SubscriberView{policy.members.data(), count}, true);
            feed.cursor = 0;
        }

        const auto ttl = policy.groups.front().ttl;
        if (!fits(now, ttl)) {
            if (feed.first_ttl) {
                --armed_;
                feed.first_ttl = 0;
            }
            tiles_[0][id] = inf;
            return;
        }
        if (!feed.first_ttl) {
            ++armed_;
            feed.first_ttl = ttl;
        }
        const auto due = now + ttl;
        earliest_ = std::min(earliest_, due);
        tiles_[0][id] = due;
        // A new feed or a recovery can bring a deadline forward. Correct
        // ancestors only as far as their lower bounds would be too late.
        std::size_t tile = id;
        for (std::size_t level = 1; level < tiles_.size(); ++level) {
            tile /= lanes;
            auto& hint = tiles_[level][tile];
            if (due >= hint) break;
            hint = due;
        }
    }

    template <class Emit>
    void advance_groups(std::uint64_t now, Emit&& emit) {
        if (now == watermark_) return;
        if (now < watermark_) throw std::invalid_argument("time moved backwards");
        watermark_ = now;
        if (now < earliest_ || (!armed_ && earliest_ == inf)) return;
        const std::size_t root = tiles_.size() - 1;
        auto earliest = earliest_ == now && armed_ ? now : repair(root, 0, now);
        while (armed_ && earliest <= now) {
            earliest = fire(root, 0, earliest, emit);
        }
        earliest_ = earliest;
    }

private:
    struct Group { std::uint64_t ttl; std::uint32_t end; };
    struct Policy {
        std::vector<Group> groups;
        std::vector<std::uint32_t> members;
    };
    struct Feed {
        std::uint64_t first_ttl; // Zero when no representable deadline is armed.
        std::uint32_t policy;
        std::uint32_t cursor; // Length of the stale prefix, measured in TTL groups.
    };

    std::vector<Policy> policies_;
    std::vector<Feed> feeds_;
    static constexpr std::size_t lanes = 16;
    static constexpr auto inf = std::numeric_limits<std::uint64_t>::max();
    // Level zero holds exact deadlines. Each higher entry is a conservative
    // minimum for a contiguous tile of its children. A full tile has 16 lanes.
    std::vector<std::vector<std::uint64_t>> tiles_;
    std::size_t armed_ = 0;
    std::uint64_t earliest_ = inf;
    void build_tiles() {
        auto count = std::max<std::size_t>(feeds_.size(), 1);
        tiles_.emplace_back(count, inf);
        do {
            count = 1 + (count - 1) / lanes;
            tiles_.emplace_back(count, inf);
        } while (count > 1);
    }
    std::uint64_t watermark_ = 0;

    static void check_size(std::size_t size) {
        if (size > std::numeric_limits<std::uint32_t>::max())
            throw std::length_error("configuration exceeds 32-bit index space");
    }

    void compile(const Config& policies) {
        check_size(policies.size());
        policies_.reserve(policies.size());
        for (const auto& ttls : policies) {
            check_size(ttls.size());
            auto& policy = policies_.emplace_back();
            policy.members.reserve(ttls.size());
            for (std::size_t i = 0; i < ttls.size(); ++i) {
                if (ttls[i] == 0) throw std::invalid_argument("TTL must be positive");
                policy.members.push_back(static_cast<std::uint32_t>(i));
            }
            std::sort(policy.members.begin(), policy.members.end(),
                [&](auto a, auto b) {
                    return ttls[a] == ttls[b] ? a < b : ttls[a] < ttls[b];
                });
            for (std::uint32_t i = 0; i < policy.members.size(); ++i) {
                const auto ttl = ttls[policy.members[i]];
                if (policy.groups.empty() || policy.groups.back().ttl != ttl)
                    policy.groups.push_back(Group{ttl, i + 1});
                else
                    policy.groups.back().end = i + 1;
            }
        }
    }

    static bool fits(std::uint64_t last, std::uint64_t ttl) noexcept {
        return ttl <= std::numeric_limits<std::uint64_t>::max() - last;
    }
    // A clock jump may pass several obsolete hints. Read only due subtrees
    // and repair their minima before firing the earliest actual deadline.
    std::uint64_t repair(std::size_t level, std::size_t node, std::uint64_t now) {
        const auto& children = tiles_[level - 1];
        const auto first = node * lanes;
        const auto end = std::min(first + lanes, children.size());
        auto result = inf;
        if (level == 1) {
            for (auto i = first; i < end; ++i) result = std::min(result, children[i]);
        } else {
            for (auto i = first; i < end; ++i) {
                const auto v = children[i] <= now ? repair(level - 1, i, now) : children[i];
                result = std::min(result, v);
            }
        }
        return tiles_[level][node] = result;
    }
    // Left-to-right visitation yields ascending feed IDs for all ties.
    template<class Emit>
    std::uint64_t fire(std::size_t level, std::size_t node, std::uint64_t when, Emit& emit) {
        auto& children = tiles_[level - 1];
        const auto first = node * lanes;
        const auto end = std::min(first + lanes, children.size());
        auto result = inf;
        for (auto i = first; i < end; ++i) {
            auto value = children[i];
            if (value == when) {
                if (level > 1) {
                    value = fire(level - 1, i, when, emit);
                } else if (feeds_[i].first_ttl) {
                    auto& feed = feeds_[i];
                    const auto& policy = policies_[feed.policy];
                    const auto origin = when - policy.groups[feed.cursor].ttl;
                    const auto end_id = policy.groups[feed.cursor].end;
                    const auto start_id = feed.cursor ? policy.groups[feed.cursor - 1].end : 0;
                    emit(when, static_cast<std::uint32_t>(i),
                         SubscriberView{policy.members.data() + start_id, end_id - start_id}, false);
                    ++feed.cursor;
                    if (feed.cursor == policy.groups.size() ||
                        !fits(origin, policy.groups[feed.cursor].ttl)) {
                        --armed_;
                        feed.first_ttl = 0;
                        value = inf;
                    } else {
                        value = origin + policy.groups[feed.cursor].ttl;
                    }
                    children[i] = value;
                }
            }
            result = std::min(result, value);
        }
        return tiles_[level][node] = result;
    }
};

} // namespace pulsefold

#endif
