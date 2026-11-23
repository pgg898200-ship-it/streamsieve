# Replay the Porto geofence labels in SQLite

Does an R-tree point query return the exact zones a taxi entered and left? On the published Porto replay, using SQLite's default R-tree bounds directly produced **10 incorrect updates out of 4,469**. The integer R-tree matched every update with the same SQL. This matters when a storage layer uses a bounding-box index as the final membership test.

| SQLite 3.53.1 index | Updates with exact entry and exit IDs | First mismatch |
| --- | ---: | --- |
| `rtree` | 4,459 / 4,469 | Trip 32, step 8 reported an extra entry into zone 1 |
| `rtree_i32` | 4,469 / 4,469 | None |

Zone 1 starts at latitude **41,153,630** in integer microdegrees. The new fix was **41,153,625**, five microdegrees south of that edge. SQLite rounds floating-point R-tree bounds outward. A query can therefore admit a point outside the original box. For this fixture, all bounds fit in signed 32-bit integers. For data requiring a different representation, use the spatial index to select candidates and test the original bounds before emitting an event. [SQLite coordinate rounding and integer R-trees](https://sqlite.org/rtree.html)

## Run it

From this directory in the StreamSieve checkout, use Python 3 with the SQLite R-tree module enabled. Both inputs are pinned to the same public dataset revision. Once downloaded, the replay runs offline.

```sh
mkdir -p porto-data
base=https://huggingface.co/datasets/pgg-engineering/edgechime-bench/resolve/6279138a1540a8aaa182df33195a39cb98078401
curl -fL "$base/zones.json" -o porto-data/zones.json
curl -fL "$base/moves.jsonl.gz" -o porto-data/moves.jsonl.gz
python3 score_sqlite.py porto-data
```

For each endpoint the query includes the west and south edges and excludes the east and north edges. The scorer subtracts the two membership sets in both directions and compares all IDs to the published labels, including updates with no changes. It prints the first mismatch and returns a failing exit status if the integer index disagrees with any label. This measures correctness on 256 synthetic rectangles and observed taxi positions. For latency, the separate JavaScript benchmark compares EdgeChime, Flatbush and a perimeter grid over several workloads. [Benchmark and losing cases](../../README.md#measured-performance) · [Dataset and attribution](https://huggingface.co/datasets/pgg-engineering/edgechime-bench) · [Map a move](../gis/README.md)
