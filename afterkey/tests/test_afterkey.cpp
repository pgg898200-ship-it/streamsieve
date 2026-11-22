#include "../include/afterkey.hpp"
#include "../include/afterkey_journal.hpp"

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>

namespace {

using Revision = std::uint64_t;
using Key = std::uint32_t;
using Change = std::tuple<Revision, Key, std::optional<int>>;
using Changes = std::vector<Change>;

void check(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}

template <typename F>
void expect_exception(F action, const char* message) {
    try {
        action();
    } catch (const std::exception&) {
        return;
    }
    throw std::runtime_error(message);
}

template <typename Index>
Changes read(const Index& index, Revision since) {
    Changes result;
    index.since(since, [&](Revision rev, Key key, const int* value) {
        result.emplace_back(rev, key, value ? std::optional<int>(*value) : std::nullopt);
    });
    return result;
}

// Retain every write, then independently compute last-write-wins in the query window.
Changes oracle(const Changes& history, Revision cursor, Key capacity) {
    std::vector<bool> seen(capacity);
    Changes answer;
    for (auto it = history.rbegin(); it != history.rend(); ++it) {
        const auto& [revision, key, value] = *it;
        if (revision <= cursor) break;
        if (!seen[key]) {
            seen[key] = true;
            answer.push_back(*it);
        }
    }
    std::reverse(answer.begin(), answer.end());
    return answer;
}

template <typename Index>
void expect_read(const Index& index, Revision cursor, const Changes& expected, const char* source) {
    const Changes actual = read(index, cursor);
    auto mismatch = std::mismatch(expected.begin(), expected.end(), actual.begin(), actual.end());
    if (mismatch.first != expected.end() || mismatch.second != actual.end()) {
        auto describe = [](auto it, auto end) -> std::string {
            if (it == end) return "<end>";
            const auto& [rev, key, value] = *it;
            return "(rev=" + std::to_string(rev) + ", key=" + std::to_string(key)
                   + ", value=" + (value ? std::to_string(*value) : "tombstone") + ")";
        };
        throw std::runtime_error(std::string(source) + " at version " + std::to_string(index.version())
            + ", cursor " + std::to_string(cursor)
            + ", row " + std::to_string(mismatch.first - expected.begin())
            + ": expected " + describe(mismatch.first, expected.end())
            + ", found " + describe(mismatch.second, actual.end()));
    }
}

template <typename Index>
void check_read(const Index& index, const Changes& history, Revision cursor, Key capacity) {
    expect_read(index, cursor, oracle(history, cursor, capacity), "full-history comparison");
}

void compaction_boundaries() {
    // Warm every key, then alternate sweeps (recent contiguous revisions) with
    // repeated writes to one key (holes in both packed prefix and recent tail).
    // Capacity one exercises a compaction whose entire previous prefix is stale.
    for (Key capacity : {1u, 2u, 7u, 13u}) {
        afterkey::Index<int> linked(capacity);
        afterkey::Journal<int> journal(capacity);
        Changes history;
        for (Revision rev = 1; rev <= 23 * capacity; ++rev) {
            const auto cycle = (rev - 1) / capacity;
            Key key = static_cast<Key>((rev - 1) % capacity);
            if (cycle % 3 == 1) key = static_cast<Key>(cycle % capacity);
            std::optional<int> value;
            if (rev % 4 == 0) {
                linked.erase(key);
                journal.erase(key);
            } else {
                value = static_cast<int>(rev);
                linked.put(key, *value);
                journal.put(key, *value);
            }
            history.emplace_back(rev, key, value);
            // All cursors at every intermediate state include exact cuts on
            // either side of each compaction and each subsequent tail append.
            for (Revision cursor = 0; cursor <= rev; ++cursor) {
                const Changes expected = oracle(history, cursor, capacity);
                expect_read(linked, cursor, expected, "Index compaction workload");
                expect_read(journal, cursor, expected, "Journal compaction workload");
            }
        }
    }
}

template <template <typename> class Engine>
void lifecycle() {
    Engine<int> index(4);
    check(index.version() == 0 && read(index, 0).empty(), "initial state");
    check(index.put(3, 9) == 1, "put returns its revision");
    index.put(0, 1);
    const Revision slow = index.version();
    check(index.erase(3) == 3, "erase returns its revision");
    const Revision fast = index.version();
    index.erase(2);  // previously absent
    index.erase(2);  // duplicate deletion still advances revision
    index.put(3, 42);
    check(index.version() == 6, "each write must advance revision");
    check(read(index, fast) == Changes{{5, 2, std::nullopt}, {6, 3, 42}}, "fast cursor");
    check(read(index, slow) == read(index, fast), "restored value coalesces deletion");
    check(read(index, 0) == Changes{{2, 0, 1}, {5, 2, std::nullopt}, {6, 3, 42}},
          "slowest cursor reports current state in revision order");
    check(read(index, index.version()).empty(), "reader already caught up");
    index.put(3, 42);
    check(read(index, 6) == Changes{{7, 3, 42}}, "identical put still counts");
    index.erase(3);
    check(read(index, 7) == Changes{{8, 3, std::nullopt}}, "visible deletion");
}

template <template <typename> class Engine>
void random_history() {
    constexpr Key capacity = 67;
    for (unsigned seed : {0u, 19u, 351u}) {
        std::mt19937 rng(seed);
        Engine<int> index(capacity);
        Changes history;
        std::vector<Revision> readers(6);
        for (Revision revision = 1; revision <= 1800; ++revision) {
            // Interleave a hot key with sparse changes to many other keys.
            Key key = (rng() % 4 == 0) ? 0 : static_cast<Key>(rng() % capacity);
            std::optional<int> payload;
            if (rng() % 3 == 0) {
                index.erase(key);
            } else {
                payload = static_cast<int>(rng() % 4);
                index.put(key, *payload);
            }
            history.emplace_back(revision, key, payload);
            check(index.version() == revision, "version drift");
            auto& reader = readers[rng() % readers.size()];
            check_read(index, history, reader, capacity);
            if (rng() % 3 == 0) reader = revision;
            if (revision % 23 == 0) check_read(index, history, rng() % (revision + 1), capacity);
        }
        for (Revision cursor = 0; cursor <= history.size(); ++cursor)
            check_read(index, history, cursor, capacity);
    }
}

template <template <typename> class Engine>
void rejected_operations() {
    Engine<int> empty(0);
    check(empty.version() == 0 && read(empty, 0).empty(), "zero capacity");
    expect_exception([&] { empty.put(0, 5); }, "put into zero capacity");
    expect_exception([&] { empty.erase(0); }, "erase from zero capacity");

    Engine<int> index(2);
    index.put(1, 4);
    const Changes expected{{1, 1, 4}};
    for (Key key : {Key{2}, std::numeric_limits<Key>::max()}) {
        expect_exception([&] { index.put(key, 8); }, "out-of-range put");
        expect_exception([&] { index.erase(key); }, "out-of-range erase");
    }
    bool visited = false;
    expect_exception([&] {
        index.since(2, [&](auto, auto, const int*) { visited = true; });
    }, "future cursor");
    check(!visited && index.version() == 1 && read(index, 0) == expected,
          "rejections must leave state intact");
}

template <template <typename> class Engine>
void callback_rules() {
    struct Stop {};
    Engine<int> index(4);
    index.put(0, 10);
    index.put(1, 20);
    index.erase(2);
    const Changes expected = read(index, 0);
    int outer = 0;
    index.since(0, [&](Revision, Key, const int*) {
        ++outer;
        check(read(index, 0) == expected, "nested full read");
        index.since(2, [&](Revision rev, Key key, const int* v) {
            check(rev == 3 && key == 2 && v == nullptr, "nested read");
            expect_exception([&] { index.put(0, 99); }, "put during nested read");
        });
        try {
            index.since(0, [](Revision, Key, const int*) { throw Stop{}; });
        } catch (Stop) {
            expect_exception([&] { index.put(2, 9); }, "write after a nested callback throws");
        }
        expect_exception([&] { index.erase(3); }, "write after nested read");
    });
    check(outer == 3 && index.version() == 3 && read(index, 0) == expected,
          "outer read disturbed by nested callbacks");

    int calls = 0;
    bool caught = false;
    try {
        index.since(0, [&](Revision, Key, const int*) {
            if (++calls == 2) throw Stop{};
        });
    } catch (Stop) {
        caught = true;
    }
    check(caught && calls == 2, "exception must propagate immediately");
    index.put(0, 100);
    check(read(index, 3) == Changes{{4, 0, 100}}, "write after interrupted read");
}

template <template <typename> class Engine>
void move_only() {
    Engine<std::unique_ptr<int>> index(2);
    index.put(0, std::make_unique<int>(1));
    index.put(1, std::make_unique<int>(2));
    index.put(0, std::make_unique<int>(3));
    index.erase(1);
    std::vector<int> values;
    index.since(0, [&](Revision, Key, const std::unique_ptr<int>* p) {
        values.push_back(p ? **p : -1);
    });
    check(values == std::vector<int>({3, -1}), "move-only replacement and erase");
}

template <template <typename> class Engine>
void failed_copy() {
    struct CopyFails {
        int value;
        explicit CopyFails(int v) : value(v) {}
        CopyFails(const CopyFails&) { throw std::runtime_error("copy failed"); }
        CopyFails(CopyFails&&) noexcept = default;
    };
    Engine<CopyFails> index(1);
    index.put(0, CopyFails{9});
    const CopyFails next{11};
    expect_exception([&] { index.put(0, next); }, "copy failure propagates");
    int observed = 0;
    index.since(0, [&](Revision, Key, const CopyFails* value) { observed = value->value; });
    check(index.version() == 1 && observed == 9, "copy failure preserves previous state");
}

template <template <typename> class Engine>
void run_suite(const char* name) {
    try {
        lifecycle<Engine>();
        random_history<Engine>();
        rejected_operations<Engine>();
        callback_rules<Engine>();
        move_only<Engine>();
        failed_copy<Engine>();
    } catch (const std::exception& error) {
        throw std::runtime_error(std::string(name) + ": " + error.what());
    }
}

}  // namespace

int main() {
    try {
        run_suite<afterkey::Index>("Index");
        run_suite<afterkey::Journal>("Journal");
        compaction_boundaries();
    } catch (const std::exception& error) {
        std::cerr << "Afterkey test failure: " << error.what() << '\n';
        return EXIT_FAILURE;
    }
    std::cout << "Afterkey tests passed\n";
}
