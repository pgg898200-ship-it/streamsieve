/*
 * Publish the most recent StreamSieve z-score for each channel. A live dashboard
 * polls after four observations; a slower observer polls after twelve. Both
 * independently reconstruct the latest scores, even if a hot channel changes
 * several times between polls.
 *
 * From the StreamSieve repository root:
 *   c++ -O2 -std=c++20 -Icsrc -Iafterkey/include afterkey/examples/streamsieve.cpp csrc/streamsieve_capi.cpp -o /tmp/streamsieve-afterkey
 *   /tmp/streamsieve-afterkey
 * Tested against StreamSieve 77a48776092056a7319d91961273abfdee3f7c7e.
 */
#include "afterkey.hpp"
#include "streamsieve_capi.h"

#include <array>
#include <cstdint>
#include <iostream>
#include <memory>
#include <stdexcept>

constexpr std::uint32_t channels = 3;

void check(int result) {
    if (result != SS_OK) throw std::runtime_error("StreamSieve operation failed");
}

double z_score(ss_estream_t* stream, std::uint32_t key) {
    ss_channel_status_t status{};
    check(ss_estream_get_channel(stream, key, &status));
    return status.z_score;
}

struct Reader {
    std::uint64_t cursor = 0;
    std::array<double, channels> scores{};
    std::size_t received = 0;

    void poll(const afterkey::Index<double>& index, ss_estream_t* stream) {
        index.since(cursor, [&](std::uint64_t, std::uint32_t key, const double* score) {
            if (!score) throw std::logic_error("unexpected deletion");
            scores[key] = *score;
            ++received;
        });
        cursor = index.version();

        // Oracle for this example: compare the reader's materialized view with
        // StreamSieve's current scores for *every* channel, including quiet ones.
        for (std::uint32_t key = 0; key < channels; ++key) {
            if (scores[key] != z_score(stream, key))
                throw std::runtime_error("reader disagrees with StreamSieve");
        }
    }
};

int main() {
    try {
        ss_estream_config_t config{};
        ss_estream_default_config(&config, channels);
        config.warmup = 12;
        std::unique_ptr<ss_estream_t, decltype(&ss_estream_destroy)> stream(
            ss_estream_create(&config), ss_estream_destroy);
        if (!stream) throw std::runtime_error("could not create StreamSieve engine");

        for (std::uint32_t tick = 0; tick < config.warmup; ++tick) {
            const double v = double(tick % 5) - 2.0;
            std::array<double, channels> baseline{v, v + 1.0, v - 1.0};
            check(ss_estream_update_row(stream.get(), baseline.data(), channels));
        }

        afterkey::Index<double> index(channels);
        for (std::uint32_t key = 0; key < channels; ++key)
            index.put(key, z_score(stream.get(), key));

        Reader dashboard, observer;
        constexpr std::array<std::uint32_t, 6> schedule{0, 0, 1, 0, 0, 2};
        for (std::uint32_t tick = 1; tick <= 24; ++tick) {
            const auto key = schedule[(tick - 1) % schedule.size()];
            check(ss_estream_update_one(stream.get(), key, double(tick) / 4.0));
            // Only the observed channel's local z-score can change on this tick.
            // A cross-channel metric such as e-BH status requires a fresh panel.
            index.put(key, z_score(stream.get(), key));
            if (tick % 4 == 0) dashboard.poll(index, stream.get());
            if (tick % 12 == 0) observer.poll(index, stream.get());
        }

        std::cout << "verified native StreamSieve scores across " << channels
                  << " channels; revisions=" << index.version()
                  << ", dashboard_rows=" << dashboard.received
                  << ", observer_rows=" << observer.received << '\n';
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
