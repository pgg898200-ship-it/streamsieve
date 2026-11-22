#include "recastcrps.hpp"

#include <cstdint>
#include <limits>
#include <numeric>
#include <optional>
#include <vector>

// Zig's wasm32-wasi C++ runtime has no exception unwinder. Public calls check
// every kernel precondition below. An internal invariant failure or exhausted
// memory traps instead of attempting to unwind across the JS/Wasm boundary.
#ifdef __wasm__
#include <wasi/api.h>
extern "C" void* __cxa_allocate_exception(std::size_t) noexcept { __builtin_trap(); }
extern "C" void __cxa_throw(void*, void*, void (*)(void*)) { __builtin_trap(); }

// This browser module owns no file descriptors. libc's optional diagnostic
// output receives the corresponding WASI error locally.
extern "C" int __imported_wasi_snapshot_preview1_fd_close(int) { return __WASI_ERRNO_BADF; }
extern "C" int __imported_wasi_snapshot_preview1_fd_write(int, int, int, int) {
    return __WASI_ERRNO_BADF;
}
extern "C" int __imported_wasi_snapshot_preview1_fd_seek(int, std::int64_t, int, int) {
    return __WASI_ERRNO_BADF;
}
#endif

namespace {
// These bounds apply only to the browser demo and bound its linear memory.
constexpr int max_coordinate = 65535;
constexpr int max_slots = 65536;
std::optional<recastcrps::Ensemble> ensemble;
std::vector<std::uint32_t> member_weights;
std::vector<double> omissions;
int last_coordinate = -1;

bool valid_slot(int slot) {
    return ensemble && slot >= 0 && static_cast<std::size_t>(slot) < member_weights.size();
}
}  // namespace

// Mutators return 0 on success or -1 for invalid input; failures leave the
// current ensemble intact. JS callers must supply signed 32-bit integers.
extern "C" int reset_ensemble(int max_value, int slots) {
    if (max_value < 0 || max_value > max_coordinate || slots < 1 || slots > max_slots)
        return -1;
    std::vector<std::int32_t> support(static_cast<std::size_t>(max_value) + 1);
    std::iota(support.begin(), support.end(), 0);
    ensemble.emplace(std::move(support), static_cast<std::size_t>(slots));
    member_weights.assign(static_cast<std::size_t>(slots), 0);
    last_coordinate = max_value;
    return 0;
}

extern "C" int set_member(int slot, int value, int weight) {
    if (!valid_slot(slot) || value < 0 || value > last_coordinate || weight < 1) return -1;
    const auto amount = static_cast<std::uint32_t>(weight);
    if (ensemble->total_weight() - member_weights[slot] + amount >
        static_cast<std::uint64_t>(std::numeric_limits<std::int32_t>::max())) return -1;
    ensemble->set(static_cast<std::size_t>(slot), value, amount);
    member_weights[slot] = amount;
    return 0;
}

extern "C" int erase_member(int slot) {
    if (!valid_slot(slot)) return -1;
    ensemble->erase(static_cast<std::size_t>(slot));
    member_weights[slot] = 0;
    return 0;
}

extern "C" double ensemble_score(int observation) {
    if (!ensemble || ensemble->count() == 0) return std::numeric_limits<double>::quiet_NaN();
    return static_cast<double>(ensemble->score(observation));
}

// The JS view has the configured slot count. Read it before the next call
// into this module, which may reallocate the vector or grow linear memory.
extern "C" std::uintptr_t omission_scores(int observation) {
    if (!ensemble) return 0;
    omissions = ensemble->leave_one_out(observation);
    return reinterpret_cast<std::uintptr_t>(omissions.data());
}
