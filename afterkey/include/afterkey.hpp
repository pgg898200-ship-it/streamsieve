#ifndef AFTERKEY_HPP
#define AFTERKEY_HPP

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

// Latest state of a fixed key space [0, capacity). Every write, including
// erasing an absent key, consumes one revision. Each touched key retains one
// entry, including tombstones, until this index is destroyed.
//
// Index is single-threaded. Serialize access externally if threads share it.
// Cursors belong to the index instance that issued them. A callback may run
// nested reads. Its value pointer is borrowed for the duration of the call.
// As with standard containers, T's special members must not reenter the index.
template<class T>
class Index {
    static_assert(std::is_nothrow_move_constructible_v<T>,
                  "Afterkey requires a noexcept move constructor for T");
    static_assert(std::is_nothrow_destructible_v<T>,
                  "Afterkey requires a noexcept destructor for T");

    using Key = std::uint32_t;
    using Revision = std::uint64_t;
    static constexpr Key end = std::numeric_limits<Key>::max();

    struct Slot {
        Revision revision = 0;
        Key older = end;
        Key newer = end;
        std::optional<T> value;
    };

    // Restore the previous flag for both nested reads and throwing callbacks.
    struct ReadScope {
        bool& flag;
        bool previous;

        explicit ReadScope(bool& active) noexcept
            : flag(active), previous(active) { flag = true; }
        ~ReadScope() { flag = previous; }
    };

public:
    explicit Index(Key capacity) : slots_(capacity) {}
    Index(const Index&) = delete;
    Index& operator=(const Index&) = delete;
    Index(Index&&) = delete;
    Index& operator=(Index&&) = delete;

    Revision version() const noexcept { return revision_; }

    Revision put(Key key, T value) {
        Slot& slot = writable(key);
        slot.value.emplace(std::move(value));
        return advance(key, slot);
    }

    Revision erase(Key key) {
        Slot& slot = writable(key);
        slot.value.reset();
        return advance(key, slot);
    }

    // Visit each key touched strictly after cursor exactly once, in increasing
    // last-write revision order. Null denotes deletion. Cursor 0 includes all
    // touched keys. Cost is O(1 + keys returned), with no temporary allocation.
    template<class F>
    void since(Revision cursor, F&& callback) const {
        since_page(cursor, std::nullopt, std::numeric_limits<std::size_t>::max(),
                   std::forward<F>(callback));
    }

    // Emit up to limit rows and return the number delivered. Keep the last
    // emitted (revision, key) for the next call. A matching nonzero cursor
    // and key seek directly to their successor; any other hint falls back to
    // the usual exact cursor seek. All callbacks share since()'s read guard.
    template<class F>
    std::size_t since_page(Revision cursor, std::optional<Key> last_key,
                           std::size_t limit, F&& callback) const {
        if (cursor > revision_) {
            throw std::out_of_range("Afterkey cursor exceeds current revision");
        }
        if (!limit || cursor == revision_) return 0;

        const ReadScope scope(reading_);
        std::size_t delivered = 0;
        for (Key key = first_after(cursor, last_key);
             key != end && delivered < limit; key = slots_[key].newer) {
            const Slot& slot = slots_[key];
            callback(slot.revision, key,
                     slot.value ? std::addressof(*slot.value) : nullptr);
            ++delivered;
        }
        return delivered;
    }

private:
    // Called only when there is a newer row. A matching hint still points to
    // its last write. If it was overwritten, seek by revision instead.
    Key first_after(Revision cursor, std::optional<Key> last_key) const noexcept {
        if (cursor && last_key && *last_key < slots_.size() &&
            slots_[*last_key].revision == cursor) {
            return slots_[*last_key].newer;
        }
        const Slot& oldest = slots_[oldest_];
        if (cursor <= oldest.revision)
            return cursor == oldest.revision ? oldest.newer : oldest_;

        Key first = newest_;
        Key older = slots_[first].older;
        while (older != end && slots_[older].revision > cursor) {
            first = older;
            older = slots_[first].older;
        }
        return first;
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

    Revision advance(Key key, Slot& slot) noexcept {
        if (key != newest_) {
            // A touched non-tail entry has a successor. Unlink it before
            // appending, leaving untouched entries out of the list entirely.
            if (slot.revision != 0) {
                slots_[slot.newer].older = slot.older;
                if (slot.older == end) oldest_ = slot.newer;
                else slots_[slot.older].newer = slot.newer;
            }
            slot.older = newest_;
            slot.newer = end;
            if (newest_ == end) oldest_ = key;
            else slots_[newest_].newer = key;
            newest_ = key;
        }
        slot.revision = ++revision_;
        return slot.revision;
    }

    std::vector<Slot> slots_;
    Key oldest_ = end;
    Key newest_ = end;
    Revision revision_ = 0;
    mutable bool reading_ = false;
};

} // namespace afterkey

#endif // AFTERKEY_HPP
