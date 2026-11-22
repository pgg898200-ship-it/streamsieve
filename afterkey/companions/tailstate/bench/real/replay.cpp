/* Google Borg machine-capacity state replay. Reads borg.py output on stdin. */
#include "tailstate.h"
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <map>
#include <memory>
#include <random>
#include <stdexcept>
#include <vector>

struct Event {
    uint64_t time, value;
    uint32_t key;
    int deleted;
};
using Rows = std::vector<ts_change>;
struct Poll { size_t end; Rows expected; };
using Clock = std::chrono::steady_clock;

static void check(bool ok) { if (!ok) throw std::runtime_error("trace replay mismatch"); }

static std::vector<Poll> windows(const std::vector<Event> &events, uint64_t interval)
{
    std::vector<Poll> polls;
    size_t i = 0;
    /* The initial poll consumes the timestamp-zero snapshot. Include empty
     * periods and one final partial period. Ties retain their source-file order. */
    for (uint64_t deadline = 0; i < events.size(); deadline += interval) {
        std::map<uint32_t, ts_change> latest;
        while (i < events.size() && events[i].time <= deadline) {
            const auto &e = events[i++];
            latest[e.key] = {i, e.value, e.key, e.deleted};
        }
        Rows expected;
        for (const auto &kv : latest) expected.push_back(kv.second);
        std::sort(expected.begin(), expected.end(), [](const auto &a, const auto &b) {
            return a.seq < b.seq;
        });
        polls.push_back({i, std::move(expected)});
    }
    return polls;
}

struct Ordered {
    using Tree = std::map<uint64_t, ts_change>;
    Tree tree;
    std::vector<Tree::iterator> by_key;
    explicit Ordered(uint32_t n): by_key(n, tree.end()) {}
    void write(uint64_t seq, const Event &e) {
        ts_change row = {seq, e.value, e.key, e.deleted};
        auto &it = by_key[e.key];
        if (it == tree.end()) {
            it = tree.emplace_hint(tree.end(), seq, row);
        } else {
            auto node = tree.extract(it);
            node.key() = seq;
            node.mapped() = row;
            it = tree.insert(tree.end(), std::move(node));
        }
    }
    void read(uint64_t cursor, Rows &out) {
        for (auto it = tree.upper_bound(cursor); it != tree.end(); ++it)
            out.push_back(it->second);
    }
};

struct Tailstate {
    std::unique_ptr<ts_store, decltype(&ts_destroy)> store;
    Tailstate(uint32_t n, bool indexed):
        store(indexed ? ts_create_indexed(n) : ts_create(n), ts_destroy) { check(bool(store)); }
    void write(uint64_t, const Event &e) {
        check((e.deleted ? ts_delete(store.get(), e.key) : ts_put(store.get(), e.key, e.value)) == TS_OK);
    }
    void read(uint64_t cursor, Rows &out) {
        ts_iter it;
        check(ts_begin(store.get(), cursor, &it) == TS_OK);
        ts_change row;
        int status;
        while ((status = ts_next(&it, &row)) == TS_ROW) out.push_back(row);
        check(status == TS_DONE);
    }
};

template<typename Store>
static double replay(Store &store, const std::vector<Event> &events,
                     const std::vector<Poll> &polls, uint32_t keys)
{
    Rows out;
    out.reserve(keys);
    size_t applied = 0;
    double total_ms = 0;
    for (const auto &poll : polls) {
        const size_t cursor = applied;
        out.clear();
        const auto start = Clock::now();
        while (applied < poll.end) {
            const auto &event = events[applied++];
            store.write(applied, event);
        }
        store.read(cursor, out);
        total_ms += std::chrono::duration<double, std::milli>(Clock::now() - start).count();
        check(out.size() == poll.expected.size());
        for (size_t i = 0; i < out.size(); ++i) {
            const auto &a = out[i], &b = poll.expected[i];
            check(a.seq == b.seq && a.value == b.value && a.key == b.key && a.deleted == b.deleted);
        }
    }
    return total_ms;
}

int main()
{
    uint32_t keys;
    size_t count;
    check(bool(std::cin >> keys >> count));
    std::vector<Event> events(count);
    for (auto &e : events)
        check(bool(std::cin >> e.time >> e.key >> e.value >> e.deleted) && e.key < keys);
    check(!events.empty());
    std::cout << "input_rows=" << count << " keys=" << keys << " final_time_us=" << events.back().time << '\n';
    std::mt19937 engine(20261122);
    for (uint64_t hours : {1, 24}) {
        auto polls = windows(events, hours * 3600 * UINT64_C(1000000));
        size_t delivered = 0;
        for (const auto &p : polls) delivered += p.expected.size();
        constexpr size_t repeats = 9;
        std::array<std::array<double, repeats>, 3> ms{};
        std::array<unsigned, 3> order = {0, 1, 2};
        for (size_t trial = 0; trial < repeats; ++trial) {
            std::shuffle(order.begin(), order.end(), engine);
            for (unsigned method : order) {
                if (method == 2) {
                    Ordered s(keys);
                    ms[method][trial] = replay(s, events, polls, keys);
                } else {
                    Tailstate s(keys, method == 1);
                    ms[method][trial] = replay(s, events, polls, keys);
                }
            }
        }
        std::cout << "poll_hours=" << hours << " polls=" << polls.size()
                  << " initial_rows=" << polls.front().expected.size()
                  << " subsequent_input=" << count - polls.front().end
                  << " subsequent_delivered=" << delivered - polls.front().expected.size()
                  << " all_delivered=" << delivered << " superseded=" << count - delivered;
        const char *methods[] = {"compact", "indexed", "std_map"};
        for (unsigned i = 0; i < 3; ++i) {
            auto &samples = ms[i];
            std::sort(samples.begin(), samples.end());
            std::cout << ' ' << methods[i] << "_p50_ms=" << samples[repeats / 2];
        }
        std::cout << '\n';
    }
}
