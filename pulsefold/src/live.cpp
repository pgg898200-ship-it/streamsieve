#include "arguments.hpp"
#include "pulsefold.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <climits>
#include <iostream>
#include <optional>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <unistd.h>

namespace {
using arguments::Id;
using arguments::Time;
using Clock = std::chrono::steady_clock;

constexpr char usage[] =
    "Usage: pulsefold-live FEED_COUNT TTL_MS[,TTL_MS...]\n"
    "Read newline-delimited decimal feed IDs from stdin. Silence advances time.\n"
    "Print and flush 'TIME FEED SUBSCRIBER stale|fresh' per transition.\n"
    "TIME is monotonic milliseconds since startup. EOF ends the monitor.\n";

int wait_ms(std::optional<Time> wake, Time now) {
    if (!wake) return -1;
    if (*wake <= now) return 0;
    return static_cast<int>(std::min<Time>(*wake - now, INT_MAX));
}
void emit(Time time, Id feed, Id subscriber, bool fresh) {
    std::cout << time << ' ' << feed << ' ' << subscriber << (fresh ? " fresh\n" : " stale\n")
              << std::flush;
}
void check_output() {
    if (!std::cout) throw std::runtime_error("output write failed");
}

void run(Id feeds, const std::vector<Time>& thresholds) {
    pulsefold::Monitor monitor({thresholds}, std::vector<Id>(feeds, 0));
    const auto origin = Clock::now();
    const auto now = [&] {
        return static_cast<Time>(std::chrono::duration_cast<std::chrono::milliseconds>(
            Clock::now() - origin).count());
    };
    std::array<char, 64> line;
    std::size_t used = 0;
    std::size_t line_number = 1;
    const auto observe_line = [&] {
        std::string_view value(line.data(), used);
        if (!value.empty() && value.back() == '\r') value.remove_suffix(1);
        try {
            const auto id = arguments::integer(value);
            if (id >= feeds) throw std::out_of_range("unknown feed ID");
            monitor.observe(static_cast<Id>(id), now(), emit);
        } catch (const std::exception& ex) {
            throw std::runtime_error("input line " + std::to_string(line_number) + ": " + ex.what());
        }
        check_output();
        ++line_number;
        used = 0;
    };

    pollfd input{STDIN_FILENO, POLLIN, 0};
    std::array<char, 4096> chunk;
    for (;;) {
        monitor.advance(now(), emit);
        check_output();
        // The elapsed time while callbacks flushed output counts toward the next deadline.
        const int ready = ::poll(&input, 1, wait_ms(monitor.next_wake(), now()));
        if (ready < 0) {
            if (errno == EINTR) continue;
            throw std::system_error(errno, std::generic_category(), "poll stdin");
        }
        if (ready == 0) continue;
        if (input.revents & POLLNVAL) throw std::runtime_error("invalid stdin descriptor");
        if (!(input.revents & (POLLIN | POLLHUP))) throw std::runtime_error("stdin poll error");
        const ssize_t bytes = ::read(STDIN_FILENO, chunk.data(), chunk.size());
        if (bytes < 0) {
            if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
            throw std::system_error(errno, std::generic_category(), "read stdin");
        }
        if (bytes == 0) {
            if (used) observe_line();
            monitor.advance(now(), emit);
            check_output();
            return;
        }
        for (ssize_t i = 0; i < bytes; ++i) {
            if (chunk[i] == '\n') {
                observe_line();
            } else {
                if (used == line.size())
                    throw std::runtime_error("input line " + std::to_string(line_number) +
                                             " exceeds 64 bytes");
                line[used++] = chunk[i];
            }
        }
    }
}
} // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--help") {
        std::cout << usage;
        return 0;
    }
    if (argc != 3) {
        std::cerr << usage;
        return 2;
    }
    std::ios::sync_with_stdio(false);
    try {
        run(arguments::feed_count(argv[1]), arguments::thresholds(argv[2]));
    } catch (const std::exception& ex) {
        std::cerr << "pulsefold-live: " << ex.what() << '\n';
        return 1;
    }
}
