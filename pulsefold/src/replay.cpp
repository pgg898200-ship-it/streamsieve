#include "arguments.hpp"
#include "pulsefold.hpp"

#include <iostream>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

namespace {
using arguments::Id;
using arguments::Time;

constexpr char usage[] =
    "Usage: pulsefold FEED_COUNT TTL[,TTL...] [--summary]\n"
    "Read lines 'TIME FEED' or 'TIME tick' from stdin. Times and TTLs use the same unit.\n"
    "Print 'TIME FEED SUBSCRIBER stale|fresh' for every transition.\n";
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--help") {
        std::cout << usage;
        return 0;
    }
    const bool summary = argc == 4 && std::string_view(argv[3]) == "--summary";
    if (argc != 3 && !summary) {
        std::cerr << usage;
        return 2;
    }
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    std::size_t row = 0;
    try {
        const auto count = arguments::feed_count(argv[1]);
        const std::vector<Id> policy_ids(count, 0);
        pulsefold::Monitor monitor({arguments::thresholds(argv[2])}, policy_ids);
        Time stale = 0, fresh = 0, observations = 0, ticks = 0;
        auto emit = [&](Time t, Id feed, Id sub, bool recovered) {
            ++(recovered ? fresh : stale);
            if (!summary)
                std::cout << t << ' ' << feed << ' ' << sub
                          << (recovered ? " fresh\n" : " stale\n");
        };
        std::string line;
        while (std::getline(std::cin, line)) {
            ++row;
            std::istringstream fields(line);
            std::string timestamp, feed, extra;
            if (!(fields >> timestamp)) continue;
            if (!(fields >> feed) || fields >> extra)
                throw std::invalid_argument("expected exactly TIME FEED or TIME tick");
            const auto t = arguments::integer(timestamp);
            if (feed == "tick") {
                monitor.advance(t, emit);
                ++ticks;
            } else {
                const auto id = arguments::integer(feed);
                if (id >= count) throw std::out_of_range("unknown feed ID");
                monitor.observe(static_cast<Id>(id), t, emit);
                ++observations;
            }
        }
        if (!std::cin.eof() || std::cin.bad()) throw std::runtime_error("input read failed");
        if (summary)
            std::cout << "{\"observations\":" << observations << ",\"ticks\":" << ticks
                      << ",\"stale\":" << stale << ",\"fresh\":" << fresh << "}\n";
        std::cout.flush();
        if (!std::cout) throw std::runtime_error("output write failed");
    } catch (const std::exception& ex) {
        std::cerr << "pulsefold" << (row ? " input line " + std::to_string(row) : "")
                  << ": " << ex.what() << '\n';
        return 1;
    }
}
