/* CPU only. Compare first-page latency for workers resuming a long backlog.
 * All implementations receive the same input and return the same ordered rows.
 * The map is a keyed, update-in-place ordered index, with a cached iterator per
 * key, a reusable node on overwrite, and end-hinted monotonic insertion. */
#include "tailstate.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <iterator>
#include <map>
#include <memory>
#include <numeric>
#include <random>
#include <utility>
#include <vector>

using Clock = std::chrono::steady_clock;
constexpr uint32_t keys = 65536;
constexpr size_t page_size = 16;
constexpr size_t requests = 256;
constexpr int rounds = 5;

struct Cursor { uint64_t seq; uint32_t key; };
using Page = std::array<ts_change, page_size>;

static void check(bool pass)
{
    if (!pass) {
        std::fputs("page benchmark verification failed\n", stderr);
        std::exit(EXIT_FAILURE);
    }
}

static double millis(Clock::time_point start)
{
    return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
}

/* Count the allocator's actual node type after std::map rebinds its allocator.
 * One live ordered index at a time. Includes requested storage, excludes the
 * allocator's own bookkeeping, as does ts_store_bytes. */
static size_t node_bytes;

template<typename T>
struct NodeAllocator {
    using value_type = T;
    NodeAllocator() = default;
    template<typename U> NodeAllocator(const NodeAllocator<U> &) {}
    T *allocate(size_t count)
    {
        T *p = std::allocator<T>{}.allocate(count);
        node_bytes += count * sizeof(T);
        return p;
    }
    void deallocate(T *ptr, size_t count)
    {
        node_bytes -= count * sizeof(T);
        std::allocator<T>{}.deallocate(ptr, count);
    }
    template<typename U> bool operator==(const NodeAllocator<U> &) const { return true; }
    template<typename U> bool operator!=(const NodeAllocator<U> &) const { return false; }
};

struct Ordered {
    struct Entry { uint64_t value; uint32_t key; bool deleted; };
    using Tree = std::map<uint64_t, Entry, std::less<uint64_t>,
                          NodeAllocator<std::pair<const uint64_t, Entry>>>;
    Tree tree;
    std::vector<Tree::iterator> by_key = std::vector<Tree::iterator>(keys, tree.end());

    size_t bytes() const
    {
        return sizeof(*this) + node_bytes + by_key.capacity() * sizeof(Tree::iterator);
    }

    void write(uint64_t seq, const Entry &entry)
    {
        auto &current = by_key[entry.key];
        if (current == tree.end()) {
            current = tree.emplace_hint(tree.end(), seq, entry);
            return;
        }
        auto node = tree.extract(current);
        node.key() = seq;
        node.mapped() = entry;
        current = tree.insert(tree.end(), std::move(node));
    }

    Page page(Cursor cursor, bool use_hint) const
    {
        auto anchor = by_key[cursor.key];
        Tree::const_iterator it;
        if (use_hint && anchor->first == cursor.seq)
            it = std::next(anchor);
        else
            it = tree.upper_bound(cursor.seq);
        Page result;
        for (auto &row : result) {
            check(it != tree.end());
            row = {it->first, it->second.value, it->second.key, it->second.deleted};
            ++it;
        }
        return result;
    }
};

static Page ts_page(const ts_store *store, Cursor cursor, bool use_hint)
{
    ts_iter it;
    const int status = use_hint ? ts_resume(store, cursor.seq, cursor.key, &it)
                                : ts_begin(store, cursor.seq, &it);
    check(status == TS_OK);
    Page result;
    for (auto &row : result)
        check(ts_next(&it, &row) == TS_ROW);
    return result;
}

static void check_equal(const Page &actual, const Page &expected)
{
    for (size_t i = 0; i < page_size; ++i) {
        const auto &a = actual[i], &b = expected[i];
        check(a.seq == b.seq && a.key == b.key && a.value == b.value && a.deleted == b.deleted);
    }
}

static uint64_t digest(const Page &page)
{
    uint64_t sum = 0;
    for (const auto &row : page)
        sum = (sum ^ row.seq ^ row.value) * 31u + row.key + (unsigned)row.deleted;
    return sum;
}

int main()
{
    const auto generate_start = Clock::now();
    std::mt19937 engine(20261122);
    std::vector<uint32_t> initial(keys), rewrites;
    std::iota(initial.begin(), initial.end(), 0u);
    std::shuffle(initial.begin(), initial.end(), engine);
    for (size_t i = 0; i < keys; i += 4)
        rewrites.push_back(initial[i]);
    std::shuffle(rewrites.begin(), rewrites.end(), engine);

    /* For matched cursor ages, pick adjacent original records one of which
     * has since been overwritten. All are in the middle half of initial load. */
    std::array<std::vector<Cursor>, 2> cursors;
    std::uniform_int_distribution<uint32_t> block(keys / 16, 3 * keys / 16 - 1);
    for (size_t i = 0; i < requests; ++i) {
        const uint32_t base = 4 * block(engine);
        cursors[0].push_back({base + 2u, initial[base + 1u]});
        cursors[1].push_back({base + 1u, initial[base]});
    }
    std::printf("keys=%u rewrites=%zu page=%zu requests_per_cohort=%zu rounds=%d "
                "seed=20261122 input_generation_ms=%.3f\n",
                keys, rewrites.size(), page_size, requests, rounds, millis(generate_start));

    using Store = std::unique_ptr<ts_store, decltype(&ts_destroy)>;
    Store compact(nullptr, ts_destroy), indexed(nullptr, ts_destroy);
    std::unique_ptr<Ordered> ordered;
    std::array<unsigned, 3> build_order{0, 1, 2};
    std::array<std::array<double, rounds>, 3> loads{}, updates{};
    for (int r = 0; r < rounds; ++r) {
        compact.reset();
        indexed.reset();
        ordered.reset();
        check(node_bytes == 0);
        std::shuffle(build_order.begin(), build_order.end(), engine);
        for (unsigned implementation : build_order) {
            auto start = Clock::now();
            uint64_t seq = 0;
            if (implementation == 2) {
                ordered = std::make_unique<Ordered>();
                for (uint32_t key : initial) {
                    ++seq;
                    ordered->write(seq, {seq, key, false});
                }
                loads[implementation][r] = millis(start);
                start = Clock::now();
                for (uint32_t key : rewrites) {
                    ++seq;
                    const bool deleted = key % 7 == 0;
                    ordered->write(seq, {deleted ? 0 : seq, key, deleted});
                }
            } else {
                Store &store = implementation == 0 ? compact : indexed;
                store.reset(implementation == 0 ? ts_create(keys) : ts_create_indexed(keys));
                check(store != nullptr);
                for (uint32_t key : initial)
                    check(ts_put(store.get(), key, ++seq) == TS_OK);
                loads[implementation][r] = millis(start);
                start = Clock::now();
                for (uint32_t key : rewrites) {
                    ++seq;
                    check((key % 7 == 0 ? ts_delete(store.get(), key) : ts_put(store.get(), key, seq)) == TS_OK);
                }
            }
            updates[implementation][r] = millis(start);
        }
    }
    const char *implementations[] = {"compact", "indexed", "map"};
    const size_t bytes[] = {ts_store_bytes(compact.get()), ts_store_bytes(indexed.get()), ordered->bytes()};
    for (unsigned i = 0; i < 3; ++i) {
        auto &build = loads[i], &write = updates[i];
        std::sort(build.begin(), build.end());
        std::sort(write.begin(), write.end());
        std::printf("store=%-7s live_requested_bytes=%zu build_p50_ms=%.3f rewrite_p50_ms=%.3f\n",
                    implementations[i], bytes[i], build[rounds / 2], write[rounds / 2]);
    }
    const char *methods[] = {"compact_begin", "compact_resume", "indexed_begin", "indexed_resume",
                             "map_seek", "map_hint"};
    std::array<unsigned, 6> order = {0, 1, 2, 3, 4, 5};
    std::vector<Cursor> misses;
    const char *windows[] = {"recent", "recent", "older", "older", "recent"};
    const char *anchors[] = {"valid", "overwritten", "valid", "overwritten", "four_misses"};
    for (size_t cohort = 0; cohort < 5; ++cohort) {
        if (cohort == cursors.size()) {
            /* Age every sampled sequence out of the recent-key ring without
             * disturbing the other records or their relative revision order. */
            const uint32_t hot_key = rewrites.back();
            uint64_t seq = ts_head(compact.get());
            for (uint32_t i = 0; i < keys; ++i) {
                ++seq;
                check(ts_put(compact.get(), hot_key, seq) == TS_OK);
                check(ts_put(indexed.get(), hot_key, seq) == TS_OK);
                ordered->write(seq, {seq, hot_key, false});
            }
            std::printf("additional_hot_key_writes=%u before older-window cohorts\n", keys);
        }
        if (cohort == 4) {
            const uint64_t start_seq = ts_head(compact.get());
            uint64_t seq = start_seq;
            auto overwrite = [&](uint32_t key) {
                ++seq;
                check(ts_put(compact.get(), key, seq) == TS_OK);
                check(ts_put(indexed.get(), key, seq) == TS_OK);
                ordered->write(seq, {seq, key, false});
            };
            for (uint32_t key : initial) overwrite(key);
            /* In this separate input, both the anchor and its four immediate
             * successors have been superseded. All sampled cursors fit in the
             * recent ring, but the tree must locate the first surviving row. */
            /* Vary the offset to sample the entire revision epoch. */
            auto first_of_run = [](uint32_t block) {
                return 64 * block + (7 * block) % 60;
            };
            for (uint32_t block = 0; block < keys / 64; ++block)
                for (size_t i = 0; i < 5; ++i) overwrite(initial[first_of_run(block) + i]);
            std::uniform_int_distribution<uint32_t> group(keys / 256, 3 * keys / 256 - 1);
            for (size_t i = 0; i < requests; ++i) {
                const uint32_t base = first_of_run(group(engine));
                misses.push_back({start_seq + base + 1, initial[base]});
            }
            std::printf("four-miss workload refreshes %u keys, then rewrites 5 per group of 64\n", keys);
        }
        auto &work = cohort == 4 ? misses : cursors[cohort % cursors.size()];
        for (auto cursor : work) {
            auto expected = ordered->page(cursor, false);
            for (const ts_store *store : {compact.get(), indexed.get()}) {
                check_equal(ts_page(store, cursor, true), expected);
                check_equal(ts_page(store, cursor, false), expected);
            }
            check_equal(ordered->page(cursor, true), expected);
        }
        std::array<std::vector<double>, 6> times;
        std::array<uint64_t, 6> checksums{};
        for (int r = 0; r < rounds; ++r) {
            std::shuffle(work.begin(), work.end(), engine);
            std::shuffle(order.begin(), order.end(), engine);
            for (unsigned method : order) {
                auto read_page = [&](Cursor cursor) {
                    if (method >= 4)
                        return ordered->page(cursor, method == 5);
                    return ts_page(method < 2 ? compact.get() : indexed.get(), cursor,
                                   (method % 2) == 1);
                };
                /* Warm the same working set for every method to avoid letting
                 * the preceding trial determine which implementation is hot. */
                for (auto cursor : work)
                    checksums[method] += digest(read_page(cursor));
                for (auto cursor : work) {
                    const auto begin = Clock::now();
                    checksums[method] += digest(read_page(cursor));
                    const auto elapsed = Clock::now() - begin;
                    times[method].push_back(std::chrono::duration<double, std::nano>(elapsed).count());
                }
            }
        }
        for (unsigned method = 0; method < order.size(); ++method) {
            check(checksums[method] == checksums[0]);
            auto &samples = times[method];
            std::sort(samples.begin(), samples.end());
            std::printf("window=%-6s anchor=%-11s method=%-14s p50_ns=%.0f p95_ns=%.0f digest=%016llx\n",
                        windows[cohort], anchors[cohort], methods[method],
                        samples[samples.size() / 2], samples[samples.size() * 95 / 100],
                        static_cast<unsigned long long>(checksums[method]));
        }
    }
}
