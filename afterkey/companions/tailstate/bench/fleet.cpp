/* Sequential single-process fleet replay. C++17 + the Tailstate C99 library.
 * See fleet.md for the trace and timing scope. */
#include "tailstate.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cinttypes>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <iterator>
#include <map>
#include <memory>
#include <random>
#include <vector>

namespace {
constexpr uint32_t keys = 1u << 20;
constexpr uint32_t subscribers = 10000;
constexpr uint32_t waves = 12;
constexpr uint32_t writes_per_wave = 65536;
constexpr uint32_t page_size = 16;
constexpr uint32_t hot_width = 32768;
constexpr uint32_t seed = 20261122;
using Clock = std::chrono::steady_clock;

void require(bool ok)
{
    if (!ok) {
        std::fputs("fleet verification failed\n", stderr);
        std::exit(EXIT_FAILURE);
    }
}

struct Cursor { uint64_t seq; uint32_t key; };
struct Page {
    std::array<ts_change, page_size> rows;
    uint32_t count = 0;
};

struct Wave {
    std::vector<uint32_t> writes;
    std::vector<uint32_t> order;
};

struct Trace {
    std::vector<uint32_t> initial;
    std::vector<Cursor> cursors;
    std::array<Wave, waves> schedule;
};

void shuffle(std::vector<uint32_t> &items, std::mt19937 &rng)
{
    for (size_t i = items.size(); i > 1; --i)
        std::swap(items[i - 1], items[rng() % i]);
}

Trace make_trace()
{
    Trace trace;
    std::mt19937 rng(seed);
    trace.initial.resize(keys);
    for (uint32_t i = 0; i < keys; ++i)
        trace.initial[i] = i;
    shuffle(trace.initial, rng);
    for (uint32_t i = 0; i < subscribers; ++i) {
        /* Half have a deep initial backlog. Half have recently connected. */
        uint32_t index = i % 2 ? keys - 1 - rng() % 4096 : rng() % (keys / 2);
        trace.cursors.push_back({index + 1u, trace.initial[index]});
    }
    for (uint32_t w = 0; w < waves; ++w) {
        Wave &wave = trace.schedule[w];
        wave.writes.reserve(writes_per_wave);
        for (uint32_t i = 0; i < writes_per_wave; ++i) {
            uint32_t key = rng() % keys;
            if (i % 10 != 0)
                key = (w * hot_width + key % hot_width) % keys;
            wave.writes.push_back(key);
        }
        wave.order.resize(subscribers);
        for (uint32_t i = 0; i < subscribers; ++i)
            wave.order[i] = i;
        shuffle(wave.order, rng);
    }
    return trace;
}

ts_change change(uint32_t key, uint64_t seq)
{
    bool deleted = seq > keys && seq % 11 == 0;
    uint64_t value = seq * UINT64_C(0x9e3779b97f4a7c15) + key;
    return ts_change{seq, deleted ? 0 : value, key, deleted};
}

struct Ordered {
    struct Entry { uint64_t value; uint32_t key; int deleted; };
    using Tree = std::map<uint64_t, Entry>;
    Tree tree;
    std::vector<Tree::iterator> by_key = std::vector<Tree::iterator>(keys, tree.end());

    void write(const ts_change &row)
    {
        Entry entry = {row.value, row.key, row.deleted};
        auto &current = by_key[row.key];
        if (current == tree.end()) {
            current = tree.emplace_hint(tree.end(), row.seq, entry);
        } else {
            auto node = tree.extract(current);
            node.key() = row.seq;
            node.mapped() = entry;
            current = tree.insert(tree.end(), std::move(node));
        }
    }

    bool anchor_live(Cursor cursor) const { return by_key[cursor.key]->first == cursor.seq; }

    Page page(Cursor cursor) const
    {
        auto anchor = by_key[cursor.key];
        Tree::const_iterator it = anchor->first == cursor.seq
                                  ? std::next(anchor) : tree.upper_bound(cursor.seq);
        Page result;
        while (result.count < page_size && it != tree.end()) {
            result.rows[result.count++] = {it->first, it->second.value, it->second.key,
                                            it->second.deleted};
            ++it;
        }
        return result;
    }
};

struct Indexed {
    std::unique_ptr<ts_store, decltype(&ts_destroy)> store{ts_create_indexed(keys), ts_destroy};
    Indexed() { require(bool(store)); }

    void write(const ts_change &row)
    {
        int status = row.deleted ? ts_delete(store.get(), row.key)
                                 : ts_put(store.get(), row.key, row.value);
        require(status == TS_OK);
    }

    Page page(Cursor cursor) const
    {
        ts_iter iter;
        require(ts_resume(store.get(), cursor.seq, cursor.key, &iter) == TS_OK);
        Page result;
        while (result.count < page_size) {
            int status = ts_next(&iter, &result.rows[result.count]);
            if (status == TS_DONE) break;
            require(status == TS_ROW);
            ++result.count;
        }
        return result;
    }
};

bool equals(const ts_change &a, const ts_change &b)
{
    return a.seq == b.seq && a.key == b.key && a.value == b.value && a.deleted == b.deleted;
}

void advance(Cursor &cursor, const Page &page)
{
    if (page.count) {
        const auto &last = page.rows[page.count - 1];
        cursor = {last.seq, last.key};
    }
}

/* Verify every row in lockstep outside the timed runs. */
void verify(const Trace &trace)
{
    Ordered ordered;
    Indexed indexed;
    uint64_t seq = 0, valid = 0, stale = 0, rows = 0;
    std::vector<uint64_t> lag;
    auto cursors = trace.cursors;
    auto write = [&](uint32_t key) {
        auto row = change(key, ++seq);
        ordered.write(row);
        indexed.write(row);
    };
    for (auto key : trace.initial) write(key);
    for (const Wave &wave : trace.schedule) {
        for (auto key : wave.writes) write(key);
        for (auto id : wave.order) {
            Cursor &cursor = cursors[id];
            lag.push_back(seq - cursor.seq);
            if (ordered.anchor_live(cursor)) ++valid; else ++stale;
            auto expected = ordered.page(cursor);
            auto actual = indexed.page(cursor);
            require(actual.count == expected.count);
            for (uint32_t i = 0; i < actual.count; ++i)
                require(equals(actual.rows[i], expected.rows[i]));
            rows += actual.count;
            advance(cursor, actual);
        }
    }
    std::sort(lag.begin(), lag.end());
    std::printf("verified_rows=%" PRIu64 " valid_hints=%" PRIu64 " stale_hints=%" PRIu64
                " lag_p50=%" PRIu64 " lag_p95=%" PRIu64 " indexed_reserved_bytes=%zu\n",
                rows, valid, stale, lag[lag.size()/2], lag[lag.size()*95/100],
                ts_store_bytes(indexed.store.get()));
}

double elapsed(Clock::time_point start)
{
    return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
}

struct Result { double build, write, read; uint64_t digest; };

template <typename Backend>
Result measure(const Trace &trace)
{
    auto cursors = trace.cursors;
    auto start = Clock::now();
    Backend backend;
    uint64_t seq = 0;
    for (auto key : trace.initial)
        backend.write(change(key, ++seq));
    Result result{elapsed(start), 0, 0, UINT64_C(0xcbf29ce484222325)};
    for (const Wave &wave : trace.schedule) {
        start = Clock::now();
        for (auto key : wave.writes)
            backend.write(change(key, ++seq));
        result.write += elapsed(start);
        start = Clock::now();
        for (auto id : wave.order) {
            auto page = backend.page(cursors[id]);
            advance(cursors[id], page);
            result.digest = (result.digest ^ id ^ page.count) * UINT64_C(1099511628211);
            for (uint32_t i = 0; i < page.count; ++i) {
                const auto &row = page.rows[i];
                result.digest = (result.digest ^ row.seq ^ row.value) * UINT64_C(1099511628211)
                                + row.key + (uint64_t)row.deleted;
            }
        }
        result.read += elapsed(start);
    }
    return result;
}
}

int main()
{
    auto trace = make_trace();
    std::printf("keys=%u subscribers=%u waves=%u writes_per_wave=%u page_size=%u seed=%u\n",
                keys, subscribers, waves, writes_per_wave, page_size, seed);
    verify(trace);
    uint64_t digest = 0;
    constexpr std::array<bool, 6> order{false, true, true, false, false, true};
    for (size_t i = 0; i < order.size(); ++i) {
        auto result = order[i] ? measure<Ordered>(trace) : measure<Indexed>(trace);
        if (i == 0) digest = result.digest;
        require(digest == result.digest);
        std::printf("run=%zu engine=%s initial_build_ms=%.3f writes_ms=%.3f "
                    "pages_ms=%.3f mixed_ms=%.3f digest=%016" PRIx64 "\n",
                    i+1, order[i] ? "std_map" : "tailstate_indexed", result.build,
                    result.write, result.read, result.write + result.read, result.digest);
    }
}
