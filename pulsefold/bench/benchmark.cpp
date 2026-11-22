#include "../include/pulsefold.hpp"
#include "baselines.hpp"
#include "workloads.hpp"
#include "wide_radix.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <numeric>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using namespace pulsefold::benchmarks;

template<class Engine>
void apply(Engine& engine, const Operation& op, Digest& digest) {
    if (op.feed == poll) engine.advance(op.time, digest);
    else engine.observe(op.feed, op.time, digest);
}

struct Result {
    Digest digest;
    double average, p99, worst;
};
template<class Engine>
Result run(const Trace& trace) {
    Engine engine(trace.config);
    Digest warm;
    for (std::size_t i = 0; i < trace.warmup; ++i) {
        warm.operation = i;
        apply(engine, trace.operations[i], warm);
    }
    Digest digest;
    const std::size_t count = trace.operations.size() - trace.warmup;
    std::vector<double> batches;
    batches.reserve((count + batch_size - 1) / batch_size);
    double elapsed = 0;
    for (std::size_t begin = trace.warmup; begin < trace.operations.size(); begin += batch_size) {
        const auto end = std::min(begin + batch_size, trace.operations.size());
        const auto before = Clock::now();
        for (std::size_t i = begin; i < end; ++i) {
            digest.operation = i;
            apply(engine, trace.operations[i], digest);
        }
        const double ns = std::chrono::duration<double, std::nano>(Clock::now() - before).count();
        elapsed += ns;
        // Tail measurements use complete equal-width batches only.
        if (end - begin == batch_size) batches.push_back(ns / batch_size);
    }
    std::sort(batches.begin(), batches.end());
    const auto rank99 = (99 * batches.size() - 1) / 100;
    return {digest, elapsed / count, batches[rank99], batches.back()};
}

Time parse_unsigned(const std::string& value) {
    if (value.empty() || value.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("expected an unsigned decimal integer: " + value);
    return std::stoull(value);
}

// Input is a logical event-time replay. The four freshness contracts are
// explicit experimental overlays and are independent of the sensor firmware.
Trace read_trace(const std::string& path) {
    Trace trace;
    trace.config.assign(54, {45'000'000, 90'000'000, 180'000'000, 600'000'000});
    std::ifstream input(path);
    if (!input) throw std::invalid_argument("could not open " + path);
    std::size_t line_no = 0;
    for (std::string line; std::getline(input, line); ) {
        ++line_no;
        const auto comment = line.find('#');
        if (comment != std::string::npos) line.resize(comment);
        std::istringstream fields(line);
        std::string stamp, feed, extra;
        if (!(fields >> stamp)) continue;
        try {
            if (!(fields >> feed) || fields >> extra)
                throw std::invalid_argument("expected timestamp_us and feed_id");
            const Time t = parse_unsigned(stamp);
            const auto f = parse_unsigned(feed);
            if (f >= trace.config.size()) throw std::invalid_argument("feed ID must be in [0,53]");
            if (!trace.operations.empty() && t < trace.operations.back().time)
                throw std::invalid_argument("timestamps must be nondecreasing");
            trace.operations.push_back({t, static_cast<Id>(f)});
        } catch (const std::exception& error) {
            throw std::runtime_error(path + ":" + std::to_string(line_no) + ": " + error.what());
        }
    }
    if (input.bad()) throw std::runtime_error("error while reading " + path);
    trace.warmup = trace.operations.size() / 10;
    if (trace.operations.size() - trace.warmup < batch_size)
        throw std::invalid_argument("trace needs at least 128 measured operations after 10% warmup");
    return trace;
}

int benchmark(int argc, char** argv) {
    Time seed = 20261122;
    unsigned repeats = 5;
    std::string selected, trace_path;
    for (int i = 1; i < argc; ++i) {
        const std::string flag(argv[i]);
        if (flag == "--list") {
            for (const auto& sc : scenarios) std::cout << sc.name << '\n';
            return 0;
        }
        if (flag == "--help") {
            std::cout << "pulsefold-bench [--seed N] [--repeats N] [--scenario NAME | --trace FILE] [--list]\n";
            return 0;
        }
        if (i + 1 == argc) throw std::invalid_argument("missing value for " + flag);
        std::string value(argv[++i]);
        if (flag == "--scenario") selected = value;
        else if (flag == "--trace") trace_path = value;
        else if (flag == "--seed") seed = parse_unsigned(value);
        else if (flag == "--repeats") {
            const auto n = parse_unsigned(value);
            if (!n || n > 1000) throw std::invalid_argument("repeats must be in [1,1000]");
            repeats = static_cast<unsigned>(n);
        } else throw std::invalid_argument("unknown option " + flag);
    }
    if (!selected.empty() && !trace_path.empty())
        throw std::invalid_argument("choose either --scenario or --trace");
    constexpr std::array<const char*, 5> names = {
        "pulsefold", "eager-feed-heap", "feed-scan", "lazy-radix", "wide-radix"
    };
    using Run = Result(*)(const Trace&);
    const std::array<Run, names.size()> runs = {
        run<pulsefold::Monitor>, run<pulsefold::bench::EagerFeedHeap>, run<pulsefold::bench::FeedScan>,
        run<pulsefold::bench::LazyRadix>, run<pulsefold::bench::WideRadix>
    };
    std::cout << "scenario,seed,repeat,run_order,engine,feeds,subscribers_per_feed,warmup_ops,"
                 "observes,advances,fresh,stale,digest_sum,digest_xor,batch_size,mean_ns_per_op,"
                 "p99_batch_mean_ns_per_op,max_batch_mean_ns_per_op\n";
    auto measure = [&](const char* label, const Trace& trace, const std::string& seed_label) {
        const auto first = trace.operations.begin() + trace.warmup;
        const auto observes = std::count_if(first, trace.operations.end(),
                                            [](Operation op) { return op.feed != poll; });
        const auto advances = std::distance(first, trace.operations.end()) - observes;
        for (unsigned rep = 0; rep < repeats; ++rep) {
            std::array<Result, names.size()> results;
            for (unsigned order = 0; order < runs.size(); ++order) {
                const auto index = (order + rep) % runs.size();
                results[index] = runs[index](trace);
            }
            for (unsigned index = 0; index < results.size(); ++index) {
                const auto& r = results[index];
                if (!r.digest.matches(results.front().digest))
                    throw std::logic_error(std::string("callback mismatch in ") + label + "/" + names[index]);
                const auto order = (index + runs.size() - rep % runs.size()) % runs.size();
                std::cout << label << ',' << seed_label << ',' << rep + 1 << ',' << order + 1 << ','
                          << names[index] << ',' << trace.config.size() << ',' << trace.config.front().size() << ','
                          << trace.warmup << ',' << observes << ',' << advances << ','
                          << r.digest.fresh << ',' << r.digest.stale << ',' << r.digest.sum << ','
                          << r.digest.parity << ',' << batch_size << ',' << std::fixed << std::setprecision(3)
                          << r.average << ',' << r.p99 << ',' << r.worst << '\n';
            }
        }
    };
    if (!trace_path.empty()) {
        measure("external-trace", read_trace(trace_path), "");
        return 0;
    }
    bool found = false;
    for (const auto& sc : scenarios) {
        if (!selected.empty() && selected != sc.name) continue;
        found = true;
        measure(sc.name, generate(sc, seed), std::to_string(seed));
    }
    if (!found) throw std::invalid_argument("unknown scenario " + selected);
    return 0;
}
} // namespace

int main(int argc, char** argv) {
    try { return benchmark(argc, argv); }
    catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return EXIT_FAILURE;
    }
}
