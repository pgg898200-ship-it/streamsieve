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
// No allocations on reads or writes by the journal after construction.
template<class T>
class Journal {
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
        : limit_(journal_capacity(capacity)), slots_(capacity),
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
        if (cursor > revision_) {
            throw std::out_of_range("Afterkey cursor exceeds current revision");
        }
        if (cursor == revision_) return;

        const ReadScope scope(reading_);
        std::size_t start = packed_count_;
        if (cursor < packed_through_) {
            const Revision* first = packed_revisions_.get();
            const Revision* pos = std::upper_bound(first, first + packed_count_, cursor);
            for (auto i = static_cast<std::size_t>(pos - first); i < packed_count_; ++i) {
                emit(keys_[i], first[i], callback);
            }
        } else {
            start += static_cast<std::size_t>(cursor - packed_through_);
        }
        for (auto i = start; i < count_; ++i) {
            emit(keys_[i], tail_revision(i), callback);
        }
    }

private:
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
    void emit(Key key, Revision revision, F& callback) const {
        if (key != empty) {
            const auto& value = slots_[key].value;
            callback(revision, key, value ? std::addressof(*value) : nullptr);
        }
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
        packed_count_ = dst;
        count_ = dst;
        packed_through_ = revision_;
    }

    Revision append(Key key, Slot& slot) noexcept {
        if (slot.position != unused) keys_[slot.position] = empty;
        if (count_ == limit_) compact();
        keys_[count_] = key;
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
