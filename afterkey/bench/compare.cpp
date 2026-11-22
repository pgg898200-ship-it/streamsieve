#include "../include/afterkey.hpp"
#include "../include/afterkey_journal.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <ctime>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <list>
#include <stdexcept>
#include <string>
#include <vector>

// All implementations expose the most recent change per key in revision order,
// including tombstones. Each reader advances its own cursor independently.
using U64 = std::uint64_t;
using Key = std::uint32_t;

struct Row {
    U64 revision{};
    U64 value{};
    bool present{};
};

class State {
protected:
    std::vector<Row> rows;
    U64 clock{};
    void change(Key k, U64 v, bool present) { rows[k] = {++clock, v, present}; }
    template<class Sink>
    void emit(Key k, Sink& sink) const {
        const Row& r = rows[k];
        sink(r.revision, k, r.present ? &r.value : nullptr);
    }
public:
    State(std::size_t capacity, std::size_t) : rows(capacity) {}
};

// Strong append-log baseline: cursor directly addresses the first candidate,
// then an array lookup skips stale log entries. Reserve even its future history.
class Log : public State {
    std::vector<Key> log;
    void write(Key k, U64 value, bool present) {
        change(k, value, present);
        log.push_back(k);
    }
public:
    Log(std::size_t n, std::size_t writes) : State(n, writes) { log.reserve(n + writes); }
    void put(Key k, U64 v) { write(k, v, true); }
    void erase(Key k) { write(k, 0, false); }
    template<class Sink> void since(U64 cursor, Sink&& sink) const {
        for (std::size_t i = cursor; i < log.size(); ++i) {
            Key k = log[i];
            if (rows[k].revision == i + 1) emit(k, sink);
        }
    }
};

// Latest stamp per slot. Scratch is reused. Sort the matches to honor the
// same oldest-to-newest callback order as the log and recency-list variants.
class Scan : public State {
    std::vector<Key> matches;
public:
    Scan(std::size_t n, std::size_t writes) : State(n, writes) { matches.reserve(n); }
    void put(Key k, U64 v) { change(k, v, true); }
    void erase(Key k) { change(k, 0, false); }
    template<class Sink> void since(U64 cursor, Sink&& sink) {
        matches.clear();
        for (std::size_t k = 0; k < rows.size(); ++k) {
            if (rows[k].revision > cursor) matches.push_back(static_cast<Key>(k));
        }
        std::sort(matches.begin(), matches.end(), [this](Key a, Key b) {
            return rows[a].revision < rows[b].revision;
        });
        for (Key k : matches) emit(k, sink);
    }
};

// Equivalent recency algorithm built from std::list. splice() reuses the node.
// Reverse discovery + forward emission is needed for ordered cursor results.
class List {
    struct Node { Key key; Row row; };
    std::list<Node> order;
    std::vector<std::list<Node>::iterator> position;
    U64 clock{};
    void write(Key k, U64 v, bool present) {
        auto i = position[k];
        if (i == order.end()) position[k] = order.insert(i, {k, {++clock, v, present}});
        else {
            i->row = {++clock, v, present};
            order.splice(order.end(), order, i);
        }
    }
public:
    List(std::size_t n, std::size_t) : position(n, order.end()) {}
    void put(Key k, U64 v) { write(k, v, true); }
    void erase(Key k) { write(k, 0, false); }
    template<class Sink> void since(U64 cursor, Sink&& sink) const {
        if (cursor == clock) return;
        auto start = order.begin();
        if (start->row.revision <= cursor) {
            start = order.end();
            while (start != order.begin()) {
                auto prev = std::prev(start);
                if (prev->row.revision <= cursor) break;
                start = prev;
            }
        }
        for (auto i = start; i != order.end(); ++i) {
            const Row& r = i->row;
            sink(r.revision, i->key, r.present ? &r.value : nullptr);
        }
    }
};

class Afterkey : public afterkey::Index<U64> {
public:
    Afterkey(std::size_t n, std::size_t) : afterkey::Index<U64>(n) {}
};

class Journal : public afterkey::Journal<U64> {
public:
    Journal(std::size_t n, std::size_t) : afterkey::Journal<U64>(n) {}
};

enum class Kind { put, erase, read };
struct Op { Kind kind; Key key; U64 argument; };
struct Scenario {
    const char* name;
    unsigned hot_keys;      // zero spans the full registry
    unsigned writes_per_poll;
    unsigned readers;
};
constexpr Scenario scenarios[] = {
    {"hot-lag", 16, 2048, 16},       // 16 readers each revisit the same hot set
    {"uniform-lag", 0, 2048, 16},    // same read schedule; little compaction
    {"frequent", 0, 4, 1},          // short suffix is favorable to the raw log
    {"write-only", 0, 0, 0}         // overhead when nobody reads
};

static U64 random_word(U64& state) {
    state += 0x9e3779b97f4a7c15ULL;
    U64 z = state;
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static std::vector<Op> trace(const Scenario& s, std::size_t n, std::size_t writes) {
    std::vector<Op> ops;
    std::vector<bool> live(n, true);
    std::vector<U64> cursors(s.readers, n);
    U64 rng = 0xc004a17;
    auto keyspace = s.hot_keys ? std::min(n, static_cast<std::size_t>(s.hot_keys)) : n;
    for (std::size_t i = 1; i <= writes; ++i) {
        Key k = static_cast<Key>(random_word(rng) % keyspace);
        U64 v = random_word(rng);
        bool remove = (v & 15) == 0 && live[k];
        live[k] = !remove;
        ops.push_back({remove ? Kind::erase : Kind::put, k, v});
        // Stagger polls across the read period. After an initial partial
        // period each independent cursor observes exactly writes_per_poll writes.
        if (s.readers) {
            unsigned stride = s.writes_per_poll / s.readers;
            if (i % stride == 0) {
                auto& cursor = cursors[(i / stride - 1) % cursors.size()];
                ops.push_back({Kind::read, 0, cursor});
                cursor = n + i;
            }
        }
    }
    return ops;
}

struct Result { U64 digest; U64 emitted; double seconds; double cpu_seconds; };
template<class T>
static Result run(std::size_t capacity, std::size_t writes, const std::vector<Op>& ops) {
    T index(capacity, writes);
    for (std::size_t k = 0; k < capacity; ++k) index.put(static_cast<Key>(k), k);
    U64 digest = 0xcbf29ce484222325ULL;
    U64 emitted = 0;
    auto consume = [&](U64 rev, Key key, const U64* value) {
        digest = (digest ^ rev) * 1099511628211ULL;
        digest = (digest ^ key) * 1099511628211ULL;
        digest = (digest ^ (value ? *value : 0)) * 1099511628211ULL;
        digest ^= (value != nullptr);
        ++emitted;
    };
    std::clock_t cpu_start = std::clock();
    auto start = std::chrono::steady_clock::now();
    for (const auto& op : ops) {
        if (op.kind == Kind::put) index.put(op.key, op.argument);
        else if (op.kind == Kind::erase) index.erase(op.key);
        else {
            digest ^= op.argument;
            index.since(op.argument, consume);
        }
    }
    auto end = std::chrono::steady_clock::now();
    std::clock_t cpu_end = std::clock();
    // Consume final state (outside timing), including in write-only workloads.
    U64 timed_emitted = emitted;
    index.since(capacity, consume);
    return {digest, timed_emitted, std::chrono::duration<double>(end-start).count(),
            static_cast<double>(cpu_end - cpu_start) / CLOCKS_PER_SEC};
}

using Run = Result(*)(std::size_t, std::size_t, const std::vector<Op>&);
constexpr std::array<Run, 5> methods{run<Afterkey>, run<Journal>, run<Log>, run<List>, run<Scan>};
constexpr std::array<const char*, 5> names{"afterkey", "afterkey-journal", "cursor-log", "std-list", "stamp-scan"};

static double median(const std::vector<double>& sorted) {
    auto n = sorted.size();
    return (sorted[(n-1)/2] + sorted[n/2]) / 2;
}

static std::size_t number(const char* arg, const char* name, std::size_t limit) {
    std::string s(arg);
    if (s.empty() || s.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument(name);
    auto n = std::stoull(s);
    if (n == 0 || n > limit) throw std::out_of_range(name);
    return static_cast<std::size_t>(n);
}

int main(int argc, char** argv) {
    try {
        if (argc > 5 || (argc > 1 && std::string(argv[1]) == "--help")) {
            std::cerr << "Usage: compare [capacity=16384] [writes=32768] [repetitions=5] [scenario]\n"
                      << "Scenarios: hot-lag uniform-lag frequent write-only\n";
            return argc > 5 ? 2 : 0;
        }
        std::size_t n = argc > 1 ? number(argv[1], "capacity", UINT32_MAX - 1) : 16384;
        std::size_t w = argc > 2 ? number(argv[2], "writes", UINT32_MAX) : 32768;
        std::size_t reps = argc > 3 ? number(argv[3], "repetitions", 1000) : 5;
        std::cout << "scenario,method,capacity,writes,reads,emitted,repetitions,min_ns_op,median_ns_op,max_ns_op,median_mixed_mops_s,median_cpu_ns_op,checksum\n";
        bool matched = false;
        for (auto s : scenarios) {
            if (argc == 5 && s.name != std::string(argv[4])) continue;
            matched = true;
            auto ops = trace(s, n, w);
            std::array<std::vector<double>, methods.size()> times;
            std::array<std::vector<double>, methods.size()> cpu_times;
            // Independently materialized row stamps + ordered output serve as oracle.
            Result expected = run<Scan>(n, w, ops);
            for (std::size_t trial = 0; trial <= reps; ++trial) {
                for (std::size_t j = 0; j < methods.size(); ++j) {
                    auto m = (trial + j) % methods.size();
                    double elapsed = 0, cpu = 0;
                    std::size_t runs = 0;
                    do {
                        Result r = methods[m](n, w, ops);
                        if (r.digest != expected.digest || r.emitted != expected.emitted)
                            throw std::runtime_error(std::string(s.name) + ": incorrect output for " + names[m]);
                        elapsed += r.seconds;
                        cpu += r.cpu_seconds;
                        ++runs;
                    } while (trial && elapsed < 0.008);
                    if (trial) {
                        double ns_per_op = 1e9 / (runs * static_cast<double>(ops.size()));
                        times[m].push_back(elapsed * ns_per_op);
                        cpu_times[m].push_back(cpu * ns_per_op);
                    }
                }
            }
            for (std::size_t m = 0; m < methods.size(); ++m) {
                auto& t = times[m];
                std::sort(t.begin(), t.end());
                auto& cpu = cpu_times[m];
                std::sort(cpu.begin(), cpu.end());
                std::cout << s.name << ',' << names[m] << ',' << n << ',' << w << ','
                          << ops.size()-w << ',' << expected.emitted << ',' << reps << ','
                          << std::fixed << std::setprecision(3) << t.front() << ',' << median(t)
                          << ',' << t.back() << ',' << 1000 / median(t) << ',' << median(cpu) << ',' << expected.digest << '\n';
            }
        }
        if (!matched) throw std::invalid_argument("unknown scenario");
    } catch (const std::exception& e) {
        std::cerr << "compare: " << e.what() << '\n';
        return 1;
    }
}
