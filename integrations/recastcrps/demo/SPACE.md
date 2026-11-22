---
title: RecastCRPS
emoji: 🌀
colorFrom: blue
colorTo: green
sdk: static
app_file: index.html
license: mit
tags:
  - ensemble-forecasting
  - forecast-verification
  - crps
  - webassembly
short_description: Score revised forecasts and inspect member withdrawals.
---

# RecastCRPS

Replay 3,844 member updates from the NOAA archive for Hurricane Lee. Select a forecast target, step through the revisions, and withdraw a member to see how much its removal changes the empirical continuous ranked probability score. The browser runs the C++ kernel compiled to WebAssembly.

For equal-weight panels the package also finds both extremal withdrawals by checking at most four forecast values. At 4,096 revising members, the C++ benchmark measured 202 ns per update and query versus 245 ns for a specialized ordered-set/Fenwick implementation. A sorted-prefix competitor was faster for static panels. [Reproduction and results](https://github.com/pgg898200-ship-it/streamsieve/tree/main/integrations/recastcrps/bench).

The trace contains 31 forecasts per complete model cycle. Initialization and target times are historical. Within each cycle the demo applies member updates in identifier order. The eventual best-track value is used for retrospective verification. This is a historical scoring replay.

For a new project, download the [source distribution](https://huggingface.co/spaces/pgg-engineering/recastcrps/resolve/main/recastcrps-0.1.0.tar.gz) and run `python -m pip install recastcrps-0.1.0.tar.gz` using GCC or Clang and Python development headers.

The source integrates with CPython and C++17 and includes an independently checked replay and reproducible benchmarks. [Source and installation](https://github.com/pgg898200-ship-it/streamsieve/tree/main/integrations/recastcrps). [NOAA National Hurricane Center data archive](https://www.nhc.noaa.gov/data/).
