# Comparison with scoringrules 0.12.0

This benchmark asks how long a Python caller waits for a new empirical CRPS after replacing one member and changing the observation. Both libraries process the same deterministic revisions. Initial setup, steady-state loops, and complete offline batches are timed separately.

## Reproduce

Run from the project root in a Python 3.12 virtual environment with GCC or Clang and C++17 support.

```sh
python -m pip install .
python -m pip install 'scoringrules[numba]==0.12.0' 'numpy==2.3.5' 'scipy==1.17.0' 'numba==0.68.1' 'llvmlite==0.50.0' 'array-api-compat==1.16.0'
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMBA_NUM_THREADS=1 python comparison/scoringrules_bench.py > results.json
```

The recorded run also used Linux `taskset -c 8` to pin the process to one available logical CPU. The script uses the released `scoringrules.crps_ensemble` API with the quantile decomposition estimator (`qd`), unsorted float64 arrays, and either default equal weights or explicit member weights. It warms each NumPy and Numba mode before timing. [scoringrules API](https://scoringrules.readthedocs.io/en/latest/generated/scoringrules.crps_ensemble.html)

## Workload and interpretation

- Forecasts are integer ticks drawn from a predeclared grid of 4,097 values, from −2,048 through 2,048. Observations may lie outside it. A fixed PRNG seed generates the initial ensemble, replacement slot, new value, positive integer weight (one or 1–8), and observation. Each timed online loop scores every replacement immediately.
- For RecastCRPS the setup timer includes grid construction and all initial member insertions. For the online scoringrules paths it includes allocating the necessary arrays from the same input lists. Raw times include Python argument handling and output collection for online scorers. Import, JIT compilation and input generation are excluded.
- The batched baseline scores all revised snapshots at once with one vectorized call. Its score time excludes constructing the snapshot matrix. Its preparation time is recorded separately. A batch of 2,048 snapshots for eight members represents offline bulk scoring. The larger workloads each have 128 snapshots.
- The table reports median wall-clock microseconds per revision across five trials. Each trial executes the five methods in a reproducibly shuffled order. CPU, runtime versions, score differences, setup times, and all five elapsed times in nanoseconds are in [results.json](results.json).

| Members | Weights | Recast online | NumPy online | Numba online | NumPy batch | Numba batch |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 8 | equal | 0.196 | 31.476 | 19.019 | 0.193 | 0.067 |
| 32 | equal | 0.210 | 23.287 | 12.733 | 0.557 | 0.297 |
| 512 | equal | 0.236 | 31.430 | 18.170 | 10.117 | 5.272 |
| 8192 | equal | 0.347 | 367.635 | 288.253 | 363.439 | 274.583 |
| 8 | 1–8 | 0.227 | 61.995 | 55.218 | 0.366 | 0.399 |
| 32 | 1–8 | 0.372 | 77.232 | 69.283 | 2.140 | 1.962 |
| 512 | 1–8 | 0.278 | 73.812 | 64.189 | 18.077 | 12.498 |
| 8192 | 1–8 | 0.361 | 448.351 | 435.144 | 604.270 | 494.638 |

At 8,192 weighted members, RecastCRPS setup took 1.395 ms and online scoringrules with Numba took 0.635 ms to prepare its arrays. A prebuilt offline batch of eight-member equal-weight ensembles scored faster with Numba at 0.067 µs per snapshot.

Across 4,864 revisions, all equal-weight scores agreed with the float64 NumPy batch reference exactly. For weighted ensembles, the largest absolute difference was 1.84 × 10⁻¹⁰ tick, with signed relative differences from −2.20 × 10⁻¹³ to +1.53 × 10⁻¹³. The project tests separately check the pairwise CRPS definition using arbitrary-precision rational arithmetic.

Measurements are warm process timings on one shared Linux host using one logical CPU of an AMD EPYC 9V74. This protocol measures tick-valued forecast revisions on a declared support. Continuous coordinates, fair estimators, weighted scores targeting specific outcomes, GPU workloads, and distributed network latency require separate measurements.

## Tail of individual calls

After 5,000 warm-up edits, a separate trace timed 20,000 individual Python update-and-score calls on 8,192 members with weights 1–8. The timer starts after unpacking the revision and stops when the score returns. A matching empty block measured timer overhead between alternating 500-record blocks. Unadjusted nearest-rank measurements were **211 ns p50, 301 ns p95, and 361 ns p99**. The empty bracket measured 50, 50, and 51 ns respectively. The longest timed call was 45,417 ns.

Run `python comparison/tail.py` with the locally built package. [tail.json](tail.json) records both duration histograms, affinity, source digests, timer resolution, and Python version. These are in-process call latencies on one pinned CPU. The main table reports medians of whole-workload means.
