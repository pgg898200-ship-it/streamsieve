#include "afterkey.hpp"
#include <algorithm>
#include <cstdint>
#include <exception>
#include <fstream>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

struct Event {
    uint64_t time_ms;
    uint32_t key;
    uint64_t value;
};

// Replay one independent reader that polls at UTC-aligned time boundaries.
// Compare complete ordered (revision, key, value) output at every nonempty poll.
// The reference uses the input as its log and a dense last-revision array.
void replay(const std::vector<Event>& events, uint32_t capacity, uint64_t period_ms) {
    afterkey::Index<uint64_t> index(capacity);
    std::vector<uint64_t> stamps(capacity);
    uint64_t cursor = 0, delivered = 0, polls = 0;
    auto poll = [&] {
        uint64_t scan = cursor;
        auto skip_stale = [&] {
            while (scan < index.version() && stamps[events[scan].key] != scan + 1)
                ++scan;
        };
        index.since(cursor, [&](uint64_t rev, uint32_t key, const uint64_t* value) {
            skip_stale();
            if (scan == index.version() || rev != scan + 1 ||
                key != events[scan].key || !value || *value != events[scan].value)
                throw std::runtime_error("index and log disagree");
            ++scan;
            ++delivered;
        });
        skip_stale();
        if (scan != index.version()) throw std::runtime_error("index omitted a key");
        ++polls;
        cursor = index.version();
    };
    uint64_t bucket = events.front().time_ms / period_ms;
    for (const auto& event : events) {
        if (event.time_ms / period_ms != bucket) {
            poll();
            bucket = event.time_ms / period_ms;
        }
        stamps[event.key] = index.put(event.key, event.value);
    }
    poll();
    std::cout << period_ms << ',' << capacity << ',' << polls << ',' << events.size()
              << ',' << delivered << ',' << uint64_t(capacity) * polls << '\n';
}

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "usage: replay prepared-events.tsv\n";
        return 2;
    }
    try {
        std::ifstream source(argv[1]);
        if (!source) throw std::runtime_error("cannot open replay file");
        std::vector<Event> events;
        uint32_t capacity = 0;
        std::string line;
        while (std::getline(source, line)) {
            std::istringstream fields(line);
            Event e{};
            if (!(fields >> e.time_ms >> e.key >> e.value) ||
                (fields >> std::ws && !fields.eof()) ||
                e.key == std::numeric_limits<uint32_t>::max() ||
                (!events.empty() && e.time_ms < events.back().time_ms))
                throw std::runtime_error("invalid event row");
            events.push_back(e);
            capacity = std::max(capacity, e.key + 1);
        }
        if (source.bad() || events.empty()) throw std::runtime_error("unreadable or empty replay");
        std::cout << "poll_ms,keys,nonempty_polls,log_rows,latest_rows,full_map_slots\n";
        for (uint64_t ms : {60000, 3600000, 86400000}) replay(events, capacity, ms);
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
