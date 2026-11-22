#!/bin/sh
set -eu
cd "$(dirname "$0")"
# Tested with Zig 0.14.1. The resulting WASI reactor imports nothing.
# In browsers: const {instance} = await WebAssembly.instantiate(bytes, {});
# Call instance.exports._initialize() once before using the bridge.
"${ZIG:-zig}" c++ -std=c++17 -target wasm32-wasi -mexec-model=reactor \
    -Oz -g0 -DNDEBUG -I../include bridge.cpp \
    -Wl,--export=reset_ensemble -Wl,--export=set_member \
    -Wl,--export=erase_member -Wl,--export=ensemble_score -Wl,--export=omission_scores \
    -o recast.wasm
