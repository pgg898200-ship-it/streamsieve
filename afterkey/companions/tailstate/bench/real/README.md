# Google Borg machine-capacity replay

This replay uses the complete machine-events shard of Google’s May 2011 Borg
trace v2.1. It exercises a fleet-capacity mirror. Applications that need every
historical capacity transition should consume the original event log.

- Source: [Google ClusterData2011_2](https://github.com/google/cluster-data/blob/master/ClusterData2011_2.md), John Wilkes and Charles Reiss; [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- File: `gs://clusterdata-2011-2/machine_events/part-00000-of-00001.csv.gz`.
  This run used the [pinned mirror](https://github.com/mCiccone23/spark-project/blob/0e6c5f36f33921240e8d3870cd98b4bce21ad212/machine_events/part-00000-of-00001.csv.gz).
- SHA-256 of the gzip archive: `fc44b8d2b33a96a261382488789855be7a0ce338888c99930ff45cb592249664`.

The 37,780 source rows include a 12,477-row snapshot at timestamp zero.
The adapter preserves file order, including timestamp ties, maps observed
machine IDs to dense integers, and preserves each add/update and removal.
It encodes CPU and memory exactly at five decimal places into one 64-bit value,
reserves all-ones 32-bit fields for missing capacities, and omits platform IDs.

The consumer schedule is hypothetical. Read the initial snapshot, then poll at
hourly or daily boundaries through the final event. Each poll emits all changed
machines, in revision order, with their latest state at that boundary.
All three implementations get identical timestamp windows. They must match an
independent per-window latest-by-key dictionary and revision sort, field by
field. The ordered baseline updates a `std::map` via dense key-to-iterator
handles, reuses extracted nodes, inserts at the monotonic end, and resumes with
`upper_bound`. The indexed and compact stores resume with `ts_begin`.

Run these commands from the project root with Python 3 and a C++17 compiler.
The Python command first tries Google Cloud Storage, then the pinned mirror,
and requires the same SHA-256 from either. To work offline, pass the path to
the downloaded archive as its argument.

```sh
cc -std=c11 -O3 -DNDEBUG -Iinclude -c src/tailstate.c -o /tmp/tailstate-borg-engine.o
c++ -std=c++17 -O3 -DNDEBUG -Iinclude bench/real/replay.cpp /tmp/tailstate-borg-engine.o -o /tmp/tailstate-borg
python3 bench/real/borg.py > /tmp/tailstate-borg-input
/tmp/tailstate-borg < /tmp/tailstate-borg-input
```

Nine replays run per polling interval, with deterministic randomized method
order. Timed work includes state mutations and full output materialization into
a preallocated vector. Parsing, initial store construction, oracle calculation,
verification, waiting, and network transport are excluded. Event processing
runs at CPU speed, independent of the original wall clock. All three stores
achieve identical row reduction. These measurements describe one shard and
one host. See [results.txt](results.txt).
