# Reproducing the comparison

Run from the repository root with Node.js 18 or newer. The script writes JSON to standard output and uses the vendored comparison code and track data. These commands require no network access or package installation.

```sh
node bench/run.js > results.json
node bench/run.js --scenario porto --steps 4469 > porto.json
node bench/run.js --scenario wide-local --boxes 100000 --steps 1024 --repeats 5 --seed 270114 > scale.json
```

Defaults are 8,192 static rectangles, at most 2,048 interleaved moves per scenario, five timed passes, and seed 270113. Use the same Node version, CPU, and command to compare changes. On Linux, prefix the command with `taskset -c CPU` using a CPU available to the process. Use an idle machine for timing.

## Workloads

A move emits the IDs of boxes whose endpoint membership changed and the corresponding enter or exit direction. Each rectangle includes its minimum edges and excludes its maximum edges. Rectangle centers are uniform over the coordinate extent. Width and height are independent uniform draws from the listed range, scaled separately by each axis span. Rectangles may extend beyond the coordinate extent.

| Scenario | Positions | Box width and height |
| --- | --- | --- |
| `wide-local` | 16 agents, each axis moves by a uniform random step of up to ±0.0006 in the unit square | 15–55% |
| `sparse-local` | Same local walks | 0.1–1% |
| `wide-teleport` | Each new position is independent and uniform in the unit square | 15–55% |
| `stationary` | Repeated coordinates, with an exact no-op fast path in all three implementations | 15–55% |
| `porto` | Up to 4,469 recorded movements, 99 taxi trip prefixes, interleaved one observation per active trip per round | 2–12% of the trace's longitude and latitude spans |

Synthetic agents start in the central 60% of each axis. Local walks clamp to the unit square. Porto trajectories preserve the order and stationary updates within trips. Its subscription geometry is synthetic. The trace ships with [selection, coordinate units, checksums, and attribution](trace.md).

## Methods

| JSON method | Work per move | Retained state |
| --- | --- | --- |
| `edgechime` | Enumerate candidate axis-aligned edges crossed between endpoints and verify membership. A packed bounding-box hierarchy handles large candidate sets on larger indexes. Small indexes may use a scan. | Box coordinates, two sorted edge indexes, coordinate directories, a packed bounding hierarchy and optional bounded strip indexes. Receives both endpoints per call. |
| `flatbush` | Query upstream Flatbush 4.7.0 once at the new point and diff against the agent’s cached previous hits. Upper bounds are converted at build time to their immediate Float64 predecessor for exact half-open lookup. | Packed Hilbert R-tree, one previous hit array and one membership bit per box per agent, and one shared 32-bit stamp per box. |
| `perimeterGrid` | Follow a horizontal and vertical path between endpoints through a uniform grid of box perimeters. Verify candidate boxes on the original endpoints, with a scan fallback. | Box coordinates, cell offsets, four bytes per posted box ID, and reusable deduplication stamps when needed. Grid resolution is reduced to fit the posting budget. |

The two delta methods can operate directly on the movement records. Each Flatbush runner initializes membership for every agent before replay. JSON stores this in `runnerSetupMs` for each timed pass. `buildMs` is the elapsed time of one index construction per scenario. The source releases and licenses are listed in [vendor/README.md](vendor/README.md).

## Verification and timing

Before timing, the harness sorts all emitted pairs of box ID and direction and compares the full arrays for **every move across all three methods**. It also checks approximately 16 evenly spaced moves and the last move against a direct scan of all boxes. Timed runs use identical callbacks that count events and accumulate signed IDs. Every pass verifies these totals.

Each method gets one additional full warm-up pass. Timed passes rotate the execution order and start each trajectory from its original coordinates. Results include each pass’s mean microseconds per update, the median of those means, and the 95th percentile of complete 64-update block means pooled across passes. The block statistic measures short-window throughput variation. Individual-request latency, startup membership delivery, NDJSON parsing, networking, and downstream notification delivery lie outside the query timing. `meanStartMembers` is the average number of containing rectangles at the initial positions. `events` counts all enter and exit callbacks for one full replay.

## Published runs

- [Default scenarios](results.json), [full Porto replay](porto-full.json) and [100,000-zone run](wide-100k.json).
- Three additional broad-zone seeds each used 8,192 boxes and 8,192 updates. [514229](holdout-514229.json), [2718281](holdout-2718281.json), [8675309](holdout-8675309.json).
- [Fleet replay](fleet-results.json). Run `node bench/fleet.mjs` to interleave 10,000 agents over 100,000 boxes. This measures only EdgeChime's in-process move loop, starting from known positions. Initial snapshots, I/O, simulation and downstream delivery are excluded. The JSON reports every pass and its event checksum.
- Each published local run used Node 24.22.3 and `taskset -c 2`. Omit the sample scale command's seed override to reproduce the default-seed 100,000-zone result, and use `--steps 2048`. Full Porto uses `--repeats 7`.
- [Second-host replay](hf-cpu.json). Four targeted workloads ran in a Hugging Face CPU-basic job on Intel Xeon Platinum 8375C, Node 22.23.5. Each run records its exact arguments and every sample.
