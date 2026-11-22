#ifndef RECASTCRPS_HPP
#define RECASTCRPS_HPP

#ifndef __SIZEOF_INT128__
#error "RecastCRPS requires a C++17 compiler with __int128 support (GCC or Clang)."
#endif

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <utility>
#include <vector>

namespace recastcrps {

// Empirical (weighted, plug-in) CRPS in the input coordinate's units.
// P is the sum of w_i*w_j*abs(x_i-x_j) over unordered pairs. Adding a
// member (x,w) adds w*A(x) to P, where A(x) = sum_i w_i*abs(x_i-x).
// A Fenwick tree of weights and weighted coordinates evaluates A using
// two prefix sums. The exact score is (W*A(observation)-P) / W^2.
// The final conversions and division may round. GCC or Clang with __int128 is required.
class Ensemble {
public:
    Ensemble(std::vector<std::int32_t> support, std::size_t slots)
        : support_(std::move(support)), slots_(slots) {
        std::sort(support_.begin(), support_.end());
        support_.erase(std::unique(support_.begin(), support_.end()), support_.end());
        tree_.resize(support_.size() + 1);
        while (selection_step_ <= support_.size() / 2) selection_step_ *= 2;
        if (!support_.empty()) {
            const auto span = static_cast<std::int64_t>(support_.back()) - support_.front();
            contiguous_ = static_cast<std::uint64_t>(span) + 1 == support_.size();
            // When nearby integer coordinates cover most gaps, an insertion-rank
            // table avoids binary search for both forecasts and observations.
            // Cap this optional index at 1 MiB and at eight entries per value.
            const auto width = static_cast<std::uint64_t>(span) + 1;
            if (!contiguous_ && width <= 262144 && (width + 7) / 8 <= support_.size()) {
                nearby_ranks_.reserve(static_cast<std::size_t>(width));
                std::uint32_t rank = 0;
                for (std::int64_t value = support_.front(); value <= support_.back(); ++value) {
                    if (support_[rank] < value) ++rank;
                    nearby_ranks_.push_back(rank);
                }
            }
        }
    }

    void set(std::size_t slot, std::int32_t value, std::uint32_t weight = 1) {
        auto& member = slots_.at(slot);
        if (weight == 0) {
            throw std::invalid_argument("member weight must be positive");
        }
        if (total_weight_ - member.weight + weight > std::numeric_limits<std::int32_t>::max()) {
            throw std::overflow_error("total weight exceeds INT32_MAX");
        }
        const auto index = lower_bound_index(value);
        if (index == support_.size() || support_[index] != value) {
            throw std::invalid_argument("member value is outside the declared support");
        }
        if (member.weight != 0 && member.index == index) {
            const auto delta = static_cast<std::int64_t>(weight) - member.weight;
            if (delta != 0) {
                adjust(index, delta);
                member.weight = weight;
            }
            return;
        }
        if (member.weight != 0) {
            adjust(member.index, -static_cast<std::int64_t>(member.weight));
        } else {
            ++count_;
        }
        adjust(index, weight);
        member = {index, weight};
    }

    // Erasing a slot that is already empty is a no-op.
    void erase(std::size_t slot) {
        auto& member = slots_.at(slot);
        if (member.weight != 0) {
            adjust(member.index, -static_cast<std::int64_t>(member.weight));
            member.weight = 0;
            --count_;
        }
    }

    long double score(std::int32_t observation) const {
        if (total_weight_ == 0) {
            throw std::domain_error("cannot score an empty ensemble");
        }
        const auto distance = absolute_sum(observation, lower_bound_index(observation));
        const Wide numerator = static_cast<Wide>(total_weight_) * distance - pair_sum_;
        return static_cast<long double>(numerator) /
               static_cast<long double>(total_weight_ * total_weight_);
    }

    // Slot i receives the CRPS after excluding all its weight. Empty slots
    // and exclusions leaving an empty ensemble receive NaN.
    std::vector<double> leave_one_out(std::int32_t observation) const {
        std::vector<double> result(slots_.size(), std::numeric_limits<double>::quiet_NaN());
        if (count_ < 2) return result;
        const auto deviation = absolute_sum(observation, lower_bound_index(observation));
        // A dense support sweep shares deviation computations across slots.
        // For sparse occupancy, query only the active slots via the tree.
        const auto distances = support_.size() / 2 <= count_ ? support_distances()
                                                              : std::vector<std::int64_t>{};
        for (std::size_t i = 0; i < slots_.size(); ++i) {
            const auto& member = slots_[i];
            if (member.weight == 0) continue;
            const auto value = support_[member.index];
            const auto at_member = distances.empty() ? absolute_sum(value, member.index)
                                                     : distances[member.index];
            const auto delta = static_cast<std::int64_t>(value) - observation;
            const auto removed_deviation = member.weight * (delta >= 0 ? delta : -delta);
            const auto mass = total_weight_ - member.weight;
            const Wide spread = pair_sum_ - static_cast<Wide>(member.weight) * at_member;
            const Wide numerator = static_cast<Wide>(mass) * (deviation - removed_deviation) - spread;
            result[i] = static_cast<double>(static_cast<long double>(numerator) /
                                            static_cast<long double>(mass * mass));
        }
        return result;
    }

    // For a unit-weight ensemble, return the minimizing and maximizing
    // withdrawals as (forecast value, score after exclusion). In a tie,
    // return the smaller value. A forecast value can occupy several slots.
    std::pair<std::pair<std::int32_t, double>, std::pair<std::int32_t, double>>
    withdrawal_extremes(std::int32_t observation) const {
        if (count_ < 2 || static_cast<std::uint64_t>(total_weight_) != count_) {
            throw std::domain_error("withdrawal_extremes requires at least two unit-weight members");
        }
        const auto mass = total_weight_ - 1;
        const auto split = lower_bound_index(observation);
        const auto left = prefix(split);
        const auto at_observation = deviation_from_left(observation, left);
        const Wide common = static_cast<Wide>(mass) * at_observation - pair_sum_;
        // Only H(x)=A(x)-(n-1)|x-y| depends on the withdrawn value x. In
        // each gap between active forecasts, let L be the mass below the gap.
        // H has slope 2L-1 left of y and 2L-2n+1 right of y. Since 1<=L<n,
        // both sides are strictly monotone up to y. Thus the minimum is at
        // an occupied endpoint and the maximum brackets y.
        const auto candidate = [&](std::int32_t value, std::int64_t at_value) {
            const auto delta = static_cast<std::int64_t>(value) - observation;
            const auto h = at_value - mass * (delta >= 0 ? delta : -delta);
            return std::make_pair(value, h);
        };
        const auto first_value = support_[select_rank(0)];
        const auto last_value = support_[select_rank(mass)];
        const auto first = candidate(first_value, weighted_sum_ - total_weight_ * first_value);
        const auto last = candidate(last_value, total_weight_ * last_value - weighted_sum_);
        const auto minimum = first.second <= last.second ? first : last;
        auto maximum = first;
        if (left.weight == total_weight_) {
            maximum = last;
        } else if (left.weight > 0) {
            // No active forecast lies strictly between either neighbor and
            // y, so one prefix supplies the slope for both distances.
            const auto slope = 2 * left.weight - total_weight_;
            const auto neighbor = [&](std::int64_t ordinal) {
                const auto value = support_[select_rank(ordinal)];
                const auto delta = static_cast<std::int64_t>(value) - observation;
                return candidate(value, at_observation + slope * delta);
            };
            const auto below = neighbor(left.weight - 1);
            const auto above = neighbor(left.weight);
            maximum = below.second >= above.second ? below : above;
        }
        const auto scored = [&](const std::pair<std::int32_t, std::int64_t>& choice) {
            const auto score = static_cast<long double>(common + choice.second) /
                               static_cast<long double>(mass * mass);
            return std::make_pair(choice.first, static_cast<double>(score));
        };
        return {scored(minimum), scored(maximum)};
    }

    std::size_t count() const noexcept { return count_; }
    std::uint64_t total_weight() const noexcept { return static_cast<std::uint64_t>(total_weight_); }

private:
    using Wide = __int128;
    struct Slot {
        std::size_t index = 0;
        std::uint32_t weight = 0;
    };
    struct Sums {
        std::int64_t weight = 0;
        std::int64_t weighted_value = 0;
    };

    std::vector<std::int32_t> support_;
    std::vector<Slot> slots_;
    std::vector<std::uint32_t> nearby_ranks_;
    std::vector<Sums> tree_;
    std::size_t count_ = 0;
    std::size_t selection_step_ = 1;
    bool contiguous_ = false;
    std::int64_t total_weight_ = 0;
    std::int64_t weighted_sum_ = 0;
    Wide pair_sum_ = 0;

    std::size_t lower_bound_index(std::int32_t value) const noexcept {
        if (contiguous_) {
            const auto offset = static_cast<std::int64_t>(value) - support_.front();
            if (offset <= 0) return 0;
            if (static_cast<std::uint64_t>(offset) >= support_.size()) return support_.size();
            return static_cast<std::size_t>(offset);
        }
        if (!nearby_ranks_.empty()) {
            const auto offset = static_cast<std::int64_t>(value) - support_.front();
            if (offset <= 0) return 0;
            if (static_cast<std::uint64_t>(offset) >= nearby_ranks_.size()) return support_.size();
            return nearby_ranks_[static_cast<std::size_t>(offset)];
        }
        return static_cast<std::size_t>(
            std::lower_bound(support_.begin(), support_.end(), value) - support_.begin());
    }

    // Values below the split contribute x*W_left-S_left. The remaining
    // values contribute S_right-x*W_right. Their sum is at most
    // INT32_MAX*(UINT32_MAX), which fits in int64_t.
    Sums prefix(std::size_t split) const noexcept {
        Sums left;
        for (auto i = split; i != 0; i &= i - 1) {
            left.weight += tree_[i].weight;
            left.weighted_value += tree_[i].weighted_value;
        }
        return left;
    }

    std::int64_t deviation_from_left(std::int32_t x, const Sums& left) const noexcept {
        const auto from_left = x * left.weight - left.weighted_value;
        const auto from_right = (weighted_sum_ - left.weighted_value) -
                                x * (total_weight_ - left.weight);
        return from_left + from_right;
    }

    std::int64_t absolute_sum(std::int32_t x, std::size_t split) const noexcept {
        return deviation_from_left(x, prefix(split));
    }

    // Zero-based weighted order statistic by Fenwick binary lifting.
    std::size_t select_rank(std::int64_t ordinal) const noexcept {
        std::size_t index = 0;
        for (auto step = selection_step_; step; step >>= 1) {
            const auto next = index + step;
            if (next < tree_.size() && tree_[next].weight <= ordinal) {
                ordinal -= tree_[next].weight;
                index = next;
            }
        }
        return index;
    }

    // Across the gap [x_i, x_(i+1)], the slope of A is
    // 2*weight_at_or_below(x_i) - W. Decode all inclusive prefix weights
    // from the Fenwick tree, then reuse the storage for A at each coordinate.
    std::vector<std::int64_t> support_distances() const {
        std::vector<std::int64_t> values(support_.size());
        for (std::size_t i = 1; i < tree_.size(); ++i) {
            const auto before = i & (i - 1);
            values[i - 1] = tree_[i].weight + (before == 0 ? 0 : values[before - 1]);
        }
        auto deviation = weighted_sum_ - total_weight_ * support_.front();
        for (std::size_t i = 1; i < support_.size(); ++i) {
            const auto gap = static_cast<std::int64_t>(support_[i]) - support_[i - 1];
            const auto slope = 2 * values[i - 1] - total_weight_;
            values[i - 1] = deviation;
            deviation += gap * slope;
        }
        values.back() = deviation;
        return values;
    }

    void adjust(std::size_t index, std::int64_t delta) noexcept {
        const auto value = support_[index];
        pair_sum_ += static_cast<Wide>(delta) * absolute_sum(value, index);
        const auto weighted_delta = delta * value;
        for (auto i = index + 1; i < tree_.size(); i += i & -i) {
            tree_[i].weight += delta;
            tree_[i].weighted_value += weighted_delta;
        }
        total_weight_ += delta;
        weighted_sum_ += weighted_delta;
    }
};

}  // namespace recastcrps

#endif  // RECASTCRPS_HPP
