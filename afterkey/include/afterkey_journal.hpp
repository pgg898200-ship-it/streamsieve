#ifndef AFTERKEY_JOURNAL_HPP
#define AFTERKEY_JOURNAL_HPP

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
#include <stdexcept>
#include <type_traits>
#include <utility>
#include <vector>

namespace afterkey {

namespace detail {

// One bit per journal position and one bit per nonempty first-level word.
// The default journal uses an empty base and maintains neither array.
struct NoLivePositions {
    explicit NoLivePositions(std::size_t) noexcept {}
};

class LivePositions {
    using Word = std::uint64_t;
    std::vector<Word> words_;
    std::vector<Word> summary_;

    static std::size_t words_for(std::size_t n) noexcept {
        return n / 64 + (n % 64 != 0);
    }

    static unsigned first_bit(Word word) noexcept {
#if defined(__GNUC__) || defined(__clang__)
        return static_cast<unsigned>(__builtin_ctzll(word));
#else
        unsigned n = 0;
        while ((word & 1) == 0) { word >>= 1; ++n; }
        return n;
#endif
    }

    static void set_prefix(std::vector<Word>& bits, std::size_t n) noexcept {
        auto next = std::fill_n(bits.begin(), n / 64, ~Word{0});
        if (n % 64) *next++ = (Word{1} << (n % 64)) - 1;
        std::fill(next, bits.end(), Word{0});
    }

protected:
    explicit LivePositions(std::size_t capacity)
        : words_(words_for(capacity)), summary_(words_for(words_.size())) {}

    void mark_live(std::size_t pos) noexcept {
        const auto w = pos / 64;
        words_[w] |= Word{1} << (pos % 64);
        summary_[w / 64] |= Word{1} << (w % 64);
    }

    void mark_retired(std::size_t pos) noexcept {
        const auto w = pos / 64;
        words_[w] &= ~(Word{1} << (pos % 64));
        if (!words_[w]) summary_[w / 64] &= ~(Word{1} << (w % 64));
    }

    void packed_prefix(std::size_t n) noexcept {
        set_prefix(words_, n);
        set_prefix(summary_, words_for(n));
    }

    std::size_t next_live(std::size_t pos, std::size_t end) const noexcept {
        auto w = pos / 64;
        if (const auto here = words_[w] & (~Word{0} << (pos % 64)))
            return w * 64 + first_bit(here);

        ++w;
        auto group = w / 64;
        if (group >= summary_.size()) return end;
        Word candidates = summary_[group] & (~Word{0} << (w % 64));
        while (!candidates) {
            if (++group == summary_.size()) return end;
            candidates = summary_[group];
        }
        w = group * 64 + first_bit(candidates);
        return w * 64 + first_bit(words_[w]);
    }
};

} // namespace detail

// Chronological journal for dense keys [0, capacity). Same catch-up semantics
// as Index: each key has one last write (possibly a deletion), reader cursors
// are scoped to this instance, and callback values are borrowed. Serialize
// shared access externally. Nested reads work; writes during reads throw.
// T's special members must not reenter the journal.
//
// A fixed array holds at most 2*capacity chronological key entries. Rewriting
// a key marks its old position empty. On filling the array we pack all live
// entries at the front and remember their original revisions. Every append
// after packing has an implicit revision given by its position.
//
// Writes are O(1) amortized with an O(capacity) compaction pause at most once
// per capacity writes. A recent cursor seeks in O(1); a pre-compaction cursor
// seeks in O(log(1 + capacity)). Traversal scans at most
// min(2*capacity, version()-cursor) entries, possibly including obsolete ones.
// Storage is O(capacity), independent of writes and number of readers.
// Set SparseReads=true to maintain two levels of live-position bits, allowing
// both bounded and full reads to skip runs of retired positions. This reserves
// about 0.254 extra bytes per possible key, plus array bookkeeping and padding.
// No allocations on reads or writes by the journal after construction.
template<class T, bool SparseReads = false>
class Journal : private std::conditional_t<SparseReads, detail::LivePositions, detail::NoLivePositions> {
    using Positions = std::conditional_t<SparseReads, detail::LivePositions, detail::NoLivePositions>;
    static_assert(std::is_nothrow_move_constructible_v<T>,
                  "Afterkey requires a noexcept move constructor for T");
    static_assert(std::is_nothrow_destructible_v<T>,
                  "Afterkey requires a noexcept destructor for T");

    using Key = std::uint32_t;
    using Revision = std::uint64_t;
    static constexpr Key empty = std::numeric_limits<Key>::max();
    static constexpr std::size_t unused = std::numeric_limits<std::size_t>::max();

    struct Slot {
        std::size_t position = unused;
        std::optional<T> value;
    };

    struct ReadScope {
        bool& flag;
        bool previous;

        explicit ReadScope(bool& active) noexcept
            : flag(active), previous(active) { flag = true; }
        ~ReadScope() { flag = previous; }
    };

    static std::size_t journal_capacity(Key keys) {
        if (keys > std::numeric_limits<std::size_t>::max() / 2) {
            throw std::length_error("Afterkey journal capacity exceeds address space");
        }
        return std::size_t{keys} * 2;
    }

public:
    explicit Journal(Key capacity)
        : Positions(journal_capacity(capacity)), limit_(journal_capacity(capacity)), slots_(capacity),
          keys_(new Key[limit_]), packed_revisions_(new Revision[capacity]) {}

    Journal(const Journal&) = delete;
    Journal& operator=(const Journal&) = delete;
    Journal(Journal&&) = delete;
    Journal& operator=(Journal&&) = delete;

    Revision version() const noexcept { return revision_; }

    Revision put(Key key, T value) {
        Slot& slot = writable(key);
        slot.value.emplace(std::move(value));
        return append(key, slot);
    }

    Revision erase(Key key) {
        Slot& slot = writable(key);
        slot.value.reset();
        return append(key, slot);
    }

    template<class F>
    void since(Revision cursor, F&& callback) const {
        visit<false>(cursor, std::nullopt, 0, callback);
    }

    // Emit up to limit live rows (including tombstones). A hint is accepted
    // only when its current revision matches cursor, even across compactions.
    // Superseded journal entries are skipped and do not consume the limit.
    template<class F>
    std::size_t since_page(Revision cursor, std::optional<Key> last_key,
                           std::size_t limit, F&& callback) const {
        return visit<true>(cursor, last_key, limit, callback);
    }

private:
    template<bool Bounded, class F>
    std::size_t visit(Revision cursor, std::optional<Key> last_key,
                      std::size_t limit, F& callback) const {
        if (cursor > revision_) {
            throw std::out_of_range("Afterkey cursor exceeds current revision");
        }
        if ((Bounded && !limit) || cursor == revision_) return 0;

        std::size_t pos = start_after(cursor, last_key);
        const ReadScope scope(reading_);
        std::size_t count = 0;
        if constexpr (SparseReads) {
            while (pos < count_ && (!Bounded || count < limit)) {
                if (keys_[pos] == empty) {
                    pos = this->next_live(pos, count_);
                    if (pos == count_) break;
                }
                const auto revision = pos < packed_count_ ? packed_revisions_[pos] : tail_revision(pos);
                emit(keys_[pos++], revision, callback);
                if constexpr (Bounded) ++count;
            }
            return count;
        }
        for (; pos < packed_count_ && (!Bounded || count < limit); ++pos) {
            if constexpr (Bounded) count += emit(keys_[pos], packed_revisions_[pos], callback);
            else emit(keys_[pos], packed_revisions_[pos], callback);
        }
        for (; pos < count_ && (!Bounded || count < limit); ++pos) {
            if constexpr (Bounded) count += emit(keys_[pos], tail_revision(pos), callback);
            else emit(keys_[pos], tail_revision(pos), callback);
        }
        return count;
    }

    std::size_t start_after(Revision cursor, std::optional<Key> last_key) const {
        // Recent revisions map directly to positions, including holes.
        if (cursor >= packed_through_)
            return packed_count_ + static_cast<std::size_t>(cursor - packed_through_);

        // A live pre-compaction anchor can skip the binary search.
        if (cursor && last_key && *last_key < slots_.size()) {
            const auto pos = slots_[*last_key].position;
            if (pos < packed_count_ && packed_revisions_[pos] == cursor)
                return pos + 1;
        }
        const Revision* first = packed_revisions_.get();
        return static_cast<std::size_t>(std::upper_bound(first, first + packed_count_, cursor) - first);
    }

    Slot& writable(Key key) {
        if (reading_) {
            throw std::logic_error("Afterkey cannot mutate during a read");
        }
        if (key >= slots_.size()) {
            throw std::out_of_range("Afterkey key exceeds capacity");
        }
        if (revision_ == std::numeric_limits<Revision>::max()) {
            throw std::overflow_error("Afterkey revision exhausted");
        }
        return slots_[key];
    }

    template<class F>
    bool emit(Key key, Revision revision, F& callback) const {
        if (key != empty) {
            const auto& value = slots_[key].value;
            callback(revision, key, value ? std::addressof(*value) : nullptr);
            return true;
        }
        return false;
    }

    Revision tail_revision(std::size_t position) const noexcept {
        return packed_through_ + (position - packed_count_) + 1;
    }

    void compact() noexcept {
        std::size_t dst = 0;
        for (std::size_t src = 0; src < count_; ++src) {
            const Key key = keys_[src];
            if (key == empty) continue;
            const Revision rev = src < packed_count_ ? packed_revisions_[src]
                                                     : tail_revision(src);
            keys_[dst] = key;
            packed_revisions_[dst] = rev;
            slots_[key].position = dst++;
        }
        if constexpr (SparseReads) this->packed_prefix(dst);
        packed_count_ = dst;
        count_ = dst;
        packed_through_ = revision_;
    }

    Revision append(Key key, Slot& slot) noexcept {
        if (slot.position != unused) {
            keys_[slot.position] = empty;
            if constexpr (SparseReads) this->mark_retired(slot.position);
        }
        if (count_ == limit_) compact();
        keys_[count_] = key;
        if constexpr (SparseReads) this->mark_live(count_);
        slot.position = count_++;
        return ++revision_;
    }

    const std::size_t limit_;
    std::vector<Slot> slots_;
    std::unique_ptr<Key[]> keys_;
    std::unique_ptr<Revision[]> packed_revisions_;
    std::size_t count_ = 0;
    std::size_t packed_count_ = 0;
    Revision packed_through_ = 0;
    Revision revision_ = 0;
    mutable bool reading_ = false;
};

} // namespace afterkey

#endif // AFTERKEY_JOURNAL_HPP
