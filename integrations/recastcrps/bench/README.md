# Benchmarking live ensemble revisions

From the repository root, build with GCC or Clang and run the benchmark:

```sh
c++ -std=c++17 -O3 -DNDEBUG -Iinclude bench/benchmark.cpp -o /tmp/recastcrps-bench
/tmp/recastcrps-bench 5 > results.csv
```

The optional argument selects repetitions per engine and scenario. Each CSV row records one complete timed run. Divide `elapsed_ns` by `revisions` to get the average nanoseconds per revision, including the requested scoring frequency. These are throughput measurements. A percentile across repetitions describes repetition variability, not individual revision latency.

## Workload

All engines start with the same populated slots and see the same ordered corrections and observations. The fixed seed is 726381. Each revision picks one member uniformly. About one in 23 updates erases it. Later updates can restore that member. Slot zero stays populated to keep the score defined. Fresh weights are uniformly drawn positive integers from one through four except in the equal-weight case.

| Case | Slots | Support points | Revision | Scores |
| --- | ---: | ---: | --- | --- |
| tiny | 8 | 128 | within three ranks | every revision |
| small | 64 | 256 | within three ranks | every revision |
| large_local | 4,096 | 4,096 | within three ranks | every revision |
| equal_local | 4,096 | 4,096 | within three ranks, unit weights | every revision |
| large_global | 4,096 | 4,096 | uniform new rank | every revision |
| tied_lattice | 8,192 | 32 | within two ranks | every revision |
| dense_global | 4,096 | 4,096 | uniform new rank | every revision |
| score_per_64 | 4,096 | 4,096 | within three ranks | every 64 revisions |
| score_per_4096 | 4,096 | 4,096 | uniform new rank | every 4,096 revisions |

The tied and dense cases use consecutive integers. The remaining supports have increments drawn uniformly from one through eight. Local moves clamp to the supported range. The observation is independently sampled from the support with an added integer offset from minus four through four, so observations can fall between or outside supported values. These are synthetic traces.

## Comparators

| CSV label | Implementation |
| --- | --- |
| `resort` | Copy active slots into a reusable buffer, sort, and calculate the weighted empirical CRPS in one scan. Slot mutation takes constant time. |
| `sorted_update` | Maintain a sorted vector of active weighted members. A correction erases and inserts by binary search. Score the sorted vector in one scan. |
| `histogram` | Adjust a flat array of per-value weights using a dense inverse index. Compute the score in one ordered pass over bins. |
| `fenwick` | Independently implemented Fenwick tree of mass and first moments plus a running pairwise spread. Use arithmetic ranks on regular grids and binary search elsewhere. |
| `recastcrps` | The public C++ API in this repository, including its input checks. |

The comparator arithmetic is specialized to these input bounds and fits in signed 64-bit integers. A run begins by checking all five engines on 3,000 revisions against a direct quadratic weighted-pair oracle. Every timed output is subsequently checked against the per-revision full-sort reference, with relative-plus-absolute tolerance of `1e-10 * (1 + abs(reference))`. Index construction, population of the initial ensemble, random generation, validation, and CSV output are outside the timed region. The timer includes mutations, score computations, and writing scores into a preallocated output buffer. Engine order is shuffled each repetition with a fixed seed.

This measures an in-process scoring kernel. End-to-end event delivery, network latency, windowing, Python dispatch and simultaneous writers are outside this driver.

## All-member withdrawal attribution

```sh
c++ -std=c++17 -O3 -DNDEBUG -Iinclude bench/attribution.cpp -o /tmp/recast-attribution
/tmp/recast-attribution 5 > attribution-results.csv
```

Each timed operation changes one member and returns an entire vector of leave-one-out CRPS values, including NaN for any inactive slot. The `small`, `many_ties`, `large`, and `sparse_active` scenarios use 16/64, 4,096/32, 4,096/4,096, and 64/65,536 members/support points respectively. All values come from a deterministically generated irregular support with weights one to four. The support, observations and revisions use seed 82631.

`sort_once` sorts the member indices once per query and uses two prefix passes. `rank_scan` maintains weights in a histogram, scans the support twice per query, and computes one value per slot. `erase_restore` removes each member, requests its score using the public API and reinserts it. Every vector is validated outside timing against the sort-once batch formula. A separate 11-member case checks every value against direct weighted pair sums after 200 edits.

## Best and worst unit-weight withdrawals

```sh
c++ -std=c++17 -O3 -DNDEBUG -Iinclude bench/extremes.cpp -o /tmp/recast-extremes
/tmp/recast-extremes 5 > extremes-results.csv
```

The result is the minimizing and maximizing removed forecast value and its post-withdrawal CRPS, breaking score ties by smaller forecast value. Dynamic cases replace one member immediately before every query. The static case queries a single ensemble with varying observations. Each driver run excludes construction and first-time preparation of the static prefix index.

- `all_loo` calls the public optimized all-member leave-one-out method, then scans those returned scores for both extrema.
- `static_prefix` sorts members and builds prefix moments. It reuses the prepared state until a member changes. One binary search per observation locates the neighbors, and the two endpoints are in the sorted vector.
- `ordered_fenwick` maintains an ordered set of occupied ranks plus a Fenwick tree of count and first moment. It updates pair spread on every revision, uses a dense inverse rank table for the bounded benchmark coordinates, and uses a single prefix query at the observation.
- `recastcrps` calls the public withdrawal-extremes method.

Both specialized comparators implement the same four-candidate mathematical shortcut. To see why it works, write the candidate-dependent term in the held-out numerator as `H(x) = A(x) - (n-1)|x-y|`, with `A(x) = sum_i |x_i-x|`. In an open gap between distinct active members, let `L` be the number of forecasts below the gap. The slope of `H` left of the observation is `2L-1 > 0`. On the right it is `2L-2n+1 < 0`. Therefore the minimum is attained at one of the two outermost values, and the maximum at an occupied neighbor of the observation. Ties are compared exactly in the native implementation.

Before measuring, the benchmark exhausts every ordered ensemble of two to five members on four distinct coordinates and every observation from -7 to 8. All 21,760 ensembles/observations are checked against a direct, quadratic-per-withdrawal oracle. The complete dynamic traces are then checked against full enumeration using the public all-member method.

## Measured results

The tables report median nanoseconds per operation across seven repetitions of each deterministic trace. The benchmark ran in one shared Linux x86-64 environment reporting an AMD EPYC 9V74, with GCC 14.2.0, C++17, and `-O3 -DNDEBUG`. Each driver was pinned to logical CPU 4 and the three drivers ran sequentially. Durations include CPU scheduling effects. Initial ensemble preparation and verification are excluded. The measured header has SHA-256 `5e5945696e21ab9eda8fa0d99a59520566f169ef70d0a069dafec52117f56c34`.

### Minimum and maximum withdrawals

[Raw repetitions](extremes-results.csv)

| Case | `recastcrps` | `ordered_fenwick` | `static_prefix` | `all_loo` |
| --- | ---: | ---: | ---: | ---: |
| small_dynamic | **120.0** | 161.0 | 932.0 | 665.6 |
| large_dynamic | **201.5** | 244.5 | 137,065.2 | 37,875.2 |
| many_ties_dynamic | 79.3 | **63.7** | 170,450.1 | 55,650.1 |
| large_static | 185.7 | 106.9 | **63.2** | 31,449.8 |

### All-member withdrawals

[Raw repetitions](attribution-results.csv)

| Case | `recastcrps` | `rank_scan` | `sort_once` | `erase_restore` |
| --- | ---: | ---: | ---: | ---: |
| small | **191.2** | 216.7 | 211.1 | 472.4 |
| many_ties | 20,478.1 | **8,843.3** | 125,075.6 | 112,105.2 |
| large | 25,799.8 | **19,180.9** | 203,025.6 | 188,803.8 |
| sparse_active | **1,175.1** | 158,139.9 | 1,236.1 | 7,624.2 |

### Score after revisions

[Raw repetitions](revision-results.csv)

| Case | `recastcrps` | `fenwick` | `histogram` | `sorted_update` | `resort` |
| --- | ---: | ---: | ---: | ---: | ---: |
| tiny | **44.5** | 101.9 | 230.1 | 55.3 | 51.8 |
| small | **56.6** | 121.2 | 504.0 | 224.0 | 806.0 |
| large_local | **57.1** | 157.0 | 6,900.3 | 8,745.5 | 97,041.7 |
| equal_local | **54.2** | 147.0 | 6,561.4 | 8,102.6 | 93,637.0 |
| large_global | **71.2** | 155.7 | 6,553.4 | 9,610.7 | 198,739.2 |
| tied_lattice | **38.3** | 44.5 | 67.8 | 17,832.0 | 406,427.6 |
| dense_global | 64.3 | **55.7** | 7,283.1 | 8,075.2 | 187,033.3 |
| score_per_64 | **38.7** | 87.1 | 108.8 | 1,808.3 | 3,065.2 |
| score_per_4096 | 46.4 | 94.7 | **6.3** | 1,878.2 | 52.3 |

On this run the fastest alternative for 4,096 changing members and two withdrawal extrema was the dedicated ordered-set/Fenwick implementation. It took 244.51 ns per revision and query against 201.51 ns for RecastCRPS. Enumerating the full leave-one-out vector took 37,875.15 ns. For a static ensemble the prebuilt sorted prefix index took 63.24 ns against 185.66 ns, and the ordered-set/Fenwick index led on the 32-point tie-heavy domain.

For a scalar score after every revision to the 4,096-member irregular ensemble, RecastCRPS took 57.12 ns for local changes and 71.15 ns for global changes. The independent Fenwick baseline took 156.96 ns and 155.70 ns respectively. That comparator led on the globally revised consecutive-integer support. Scoring only once per 4,096 changes favored the flat histogram. For all-member withdrawal scores, the specialized rank scan led on both dense 4,096-member workloads. The margins on small and sparsely occupied ensembles were closer. These rankings describe the benchmark inputs and host.
