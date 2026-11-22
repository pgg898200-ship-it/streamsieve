#include "afterkey.hpp"
#include <cstdint>
#include <iostream>

int main() {
    afterkey::Index<int> state(16);
    state.put(7, 10);
    const auto reader_cursor = state.version();
    state.put(3, 20);
    state.put(7, 11);
    state.erase(3);
    state.since(reader_cursor, [](std::uint64_t rev, std::uint32_t key, const int* value) {
        std::cout << rev << ' ' << key << ' ';
        if (value) std::cout << *value;
        else std::cout << "deleted";
        std::cout << '\n';
    });
}
