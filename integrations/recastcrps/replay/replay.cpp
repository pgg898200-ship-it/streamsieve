#include "recastcrps.hpp"

#include <array>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
constexpr std::size_t members = 31;

std::vector<std::string> split(const std::string& line) {
    std::istringstream stream(line);
    std::vector<std::string> fields;
    std::string field;
    while (std::getline(stream, field, ',')) fields.push_back(field);
    return fields;
}

// A separate evaluation of the definition on this small, equally weighted panel.
long double direct_score(const std::array<int, members>& values, std::size_t active, int observation) {
    std::int64_t distances = 0;
    std::int64_t pair_distances = 0;
    for (std::size_t i = 0; i < active; ++i) {
        distances += std::abs(values[i] - observation);
        for (std::size_t j = 0; j < i; ++j)
            pair_distances += std::abs(values[i] - values[j]);
    }
    const auto n = static_cast<std::int64_t>(active);
    return static_cast<long double>(n * distances - pair_distances) / (n * n);
}

void replay(const char* path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error(std::string("cannot open ") + path);
    std::string line;
    std::getline(input, line);
    const auto header = split(line);
    if (header.size() != members + 3 || header[0] != "target_utc" ||
        header[1] != "init_utc" || header[2] != "observed_kt" || header[3] != "AC00")
        throw std::runtime_error("unexpected CSV schema");
    for (std::size_t slot = 1; slot < members; ++slot) {
        if (header[3 + slot] != std::string(slot < 10 ? "AP0" : "AP") + std::to_string(slot))
            throw std::runtime_error("unexpected ensemble member");
    }

    std::vector<std::int32_t> support(256);
    std::iota(support.begin(), support.end(), 0);
    std::optional<recastcrps::Ensemble> ensemble;
    std::array<int, members> values{};
    std::string current_target, previous_init;
    std::size_t cycles = 0, targets = 0, updates = 0, changed = 0, unchanged = 0, active = 0;
    int observation = 0;
    long double cycle_sum = 0;
    while (std::getline(input, line)) {
        const auto fields = split(line);
        if (fields.size() != header.size()) throw std::runtime_error("incomplete ensemble row");
        const int verifying_intensity = std::stoi(fields[2]);
        if (fields[0] != current_target) {
            if (fields[0] <= current_target) throw std::runtime_error("unordered target");
            current_target = fields[0];
            previous_init.clear();
            observation = verifying_intensity;
            ensemble.emplace(support, members);
            active = 0;
            ++targets;
        }
        if (fields[1] <= previous_init || fields[1] >= current_target || verifying_intensity != observation)
            throw std::runtime_error("inconsistent forecast cycle");
        previous_init = fields[1];
        long double latest = 0;
        for (std::size_t slot = 0; slot < members; ++slot) {
            const int next = std::stoi(fields[3 + slot]);
            if (slot == active) ++active;
            else if (values[slot] == next) ++unchanged;
            else ++changed;
            values[slot] = next;
            ensemble->set(slot, next);
            latest = ensemble->score(observation);
            if (latest != direct_score(values, active, observation))
                throw std::runtime_error("score mismatch at " + current_target + "/" +
                                         fields[1] + "/" + header[3 + slot]);
            ++updates;
        }
        cycle_sum += latest;
        ++cycles;
    }
    if (!cycles) throw std::runtime_error("no complete cycles");
    std::cout << std::fixed << std::setprecision(8)
              << "targets=" << targets << " cycles=" << cycles << " members=" << members
              << " verified_updates=" << updates << " revised_values=" << changed
              << " unchanged_resubmissions=" << unchanged << '\n'
              << "mean_complete_cycle_crps_kt=" << cycle_sum / cycles << '\n';
}
}  // namespace

int main(int argc, char** argv) {
    if (argc > 2) {
        std::cerr << "usage: replay [path-to-lee_gefs.csv]\n";
        return 2;
    }
    try {
        replay(argc == 2 ? argv[1] : "replay/lee_gefs.csv");
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
