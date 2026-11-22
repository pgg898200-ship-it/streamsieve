#ifndef PULSEFOLD_ARGUMENTS_HPP
#define PULSEFOLD_ARGUMENTS_HPP

#include <charconv>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <vector>

namespace arguments {
using Time = std::uint64_t;
using Id = std::uint32_t;

inline Time integer(std::string_view text) {
    Time value;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
    if (parsed.ec != std::errc() || parsed.ptr != text.data() + text.size())
        throw std::invalid_argument("expected an unsigned decimal integer");
    return value;
}

inline Id feed_count(std::string_view text) {
    const auto value = integer(text);
    if (value == 0 || value > std::numeric_limits<Id>::max())
        throw std::invalid_argument("feed count outside 1..UINT32_MAX");
    return static_cast<Id>(value);
}

inline std::vector<Time> thresholds(std::string_view text) {
    std::vector<Time> values;
    while (true) {
        const auto comma = text.find(',');
        values.push_back(integer(text.substr(0, comma)));
        if (comma == text.npos) return values;
        text.remove_prefix(comma + 1);
    }
}
} // namespace arguments

#endif
