# RankFence benchmark results

## Ranked retrieval replays

Four TREC Robust 2003 submissions supplied 100 topics with 1,000 IDs per source per topic. The NFCorpus video-query ablation supplied 102 topics. For each topic it includes title, description and combined-text retrieval. Each BM25 run contributes 1,000 results and its cross-encoder rerank contributes 100. These six sources are correlated by construction.

All runs used RRF constant 60, top 5, pages of 32 IDs and one page per active source per round. Ordered mode certifies the final rank order. Membership mode certifies the set a downstream reranker would receive. On both collections, each mode produced the same result as exhaustive exact fusion for every topic. RankFence and the independent no-random-access (NRA) implementation also stopped at the same page on every topic.

| Collection | Mode | Topics stopping early | Entries read / available | Skipped | RankFence p50 / p95, ms | Integer NRA p50 / p95, ms | Exact integer batch p50 / p95, ms |
|---|---|---:|---:|---:|---:|---:|---:|
| Robust03 | Ordered | 93/100 | 83,168 / 400,000 | 79.21% | 0.262 / 4.166 | 0.179 / 14.191 | 0.914 / 1.395 |
| Robust03 | Membership | 95/100 | 70,560 / 400,000 | 82.36% | 0.257 / 3.587 | 0.167 / 13.916 | 0.847 / 1.237 |
| NFCorpus | Ordered | 93/102 | 88,512 / 336,600 | 73.70% | 0.469 / 3.701 | 0.323 / 10.594 | 0.645 / 0.994 |
| NFCorpus | Membership | 98/102 | 78,408 / 336,600 | 76.71% | 0.490 / 2.867 | 0.313 / 6.887 | 0.661 / 0.937 |

The coordinator used fewer input entries than batch fusion. Integer NRA had lower median CPU-side elapsed time on these traces. RankFence had lower p95 CPU-side time than integer NRA in every row. The in-memory integer batch implementation had the lowest p95 among those three implementations.

## TCP loopback

Four local TCP producers replayed the same 12 evenly sampled Robust03 topics in each scenario. A client opened one connection per source. The paged exhaustive client requested all 32-ID pages, the one-shot client requested each entire window in one message, and RankFence stopped requesting pages at certification. Both exhaustive clients used the exact integer batch implementation. Every method returned the same ordered top five.

| Producer cost model | RankFence p50 / p95, ms | Paged exhaustive p50 / p95, ms | One-shot exhaustive p50 / p95, ms |
|---|---:|---:|---:|
| No delay added | 3.03 / 16.39 | 6.07 / 8.93 | 2.78 / 4.13 |
| 1 ms per request + 10 µs per hit | 21.89 / 117.05 | 82.84 / 104.12 | 15.79 / 18.76 |
| 1 ms per request + 100 µs per hit | 45.50 / 196.40 | 180.88 / 188.14 | 106.36 / 109.71 |

RankFence received a mean 21,268 application response bytes across the sampled topics, compared with 60,654 for paged exhaustive and 59,414 for one-shot exhaustive. For the most expensive producer model, RankFence beat both alternatives at the median. The one-shot client retained the best p95 in every scenario. The delays are simulated with asynchronous timers. The socket traffic, decoding, connection setup, fusion and latency are measured. These are local experiments on precomputed rankings. A production backend can save retrieval work only if it can serve ranked prefixes incrementally and avoid or cancel work for the suffix.

## Scale and counterexamples

| Workload | Result |
|---|---|
| 64 identical ranked lists × 16,384 candidates | Top five certified at 2,048 of 1,048,576 entries. This single deliberately favorable query took a median 1.10 ms over three timed repeats. |
| 32 partially overlapping lists × 2,048 candidates | All three queries required their entire input. RankFence took 126.75 ms at the median compared with 27.08 ms for exact integer batch and 429.51 ms for integer NRA. |
| 4 disjoint lists × 512 candidates | Full reads on all five queries. The top documents have ties and each open source can still promote a competitor. |
| 4 sources with short windows | Every source fits in a single page. A coordinator cannot reduce transfer within that page. |

The synthetic sweep also covers partially shared shuffled prefixes and rotating ranks that produce exact ties. See the per-query results for ordering differences from the floating-point batch, which follows the usual sum-and-sort approach.

## Disjoint index partitions

A separate replay assigns each Robust03 document to one of eight shards by CRC32 of its ID. Each of the four real rankings is filtered to its shard and numbered again, giving 32 shard-local ranked streams. This models independent retrieval on disjoint indexes. RRF scores here use the new local ranks. The exhaustive reference uses exactly those same ranks.

| Exact method | Entries read, 100 topics | Topics stopping early | p50 / p95, ms |
|---|---:|---:|---:|
| Global Fence with unknown source overlap | 400,000 | 0 | 4.582 / 8.543 |
| PartitionFence with shard declarations | 82,637 | 99 | 0.708 / 4.105 |
| Independent local top-five, then score merge | 206,934 | 99 | 2.415 / 6.667 |
| Direct partition-aware integer NRA | 82,637 | 99 | 0.338 / 4.772 |
| Exhaustive integer batch | 400,000 | 0 | 0.746 / 1.288 |

The local-merge baseline stops each shard independently once it can identify its five possible winners and prove their final scores. For example, a candidate seen in every local source already has its exact score. A global decision can often establish that a shard contains no competitive item before that shard can resolve its own five winners. Across five synthetic single-hot-shard queries, PartitionFence read 1,920 entries and local merge read 39,984. On five queries with perfectly agreeing rankers inside each shard, both read 1,920.

The direct partition-aware NRA baseline uses the same shard information and a warm exact integer table. Its stopping rounds matched PartitionFence on all 100 transformed topics and all 15 synthetic queries. It won the median CPU timing on the transformed Robust03 topics. Results compare ordered top-five decisions over pull-based pages of 16. Latencies in this section are single-process coordinator experiments.

## RankSeal comparison

The actual [RankSeal](https://huggingface.co/spaces/pgg-engineering/rankseal) Fusion API was replayed against the same 100 Robust03 topics. Every ordered top five matched exhaustive integer RRF. Both projects received the same ascending ID tie break, unit weights, rank constant 60 and 16-ID pages. This page size differs from the 32-ID headline replay.

| Input | Method | Entries / 400,000 | p50 / p95, ms |
|---|---|---:|---:|
| Four original rankings | RankSeal | 79,904 | 0.348 / 9.543 |
| Four original rankings | RankFence | 79,904 | 0.242 / 7.107 |
| 32 shard-local rankings | RankSeal | 400,000 | 8.825 / 18.451 |
| 32 shard-local rankings | RankFence | 400,000 | 5.389 / 13.962 |
| 32 shard-local rankings | One RankSeal per shard, then merge exact scores | 206,934 | 3.865 / 9.334 |
| 32 shard-local rankings | PartitionFence | 82,637 | 0.813 / 3.148 |
| 32 shard-local rankings | PartitionFence with retirement of losing shards | 78,956 | 0.770 / 2.713 |

RankSeal and RankFence consumed identical aggregate input on the original rankings. In the sharded replay, both generic coordinators had to allow for unseen overlap across every stream. The local RankSeal wrapper gave each group its own coordinator, then waited for exact scores of local winners. PartitionFence could certify globally with 60.07% fewer entries than that wrapper. Retiring each losing shard as soon as its score ceiling fell strictly below the global cutoff reduced total reads to 78,956, or 61.84% fewer than the local RankSeal wrapper. RankSeal also exposes weighted RRF and a two-source hindsight page frontier. This comparison exercises its equal-weight streaming core.

The comparison imports the MIT-licensed RankSeal core from the bundled benchmark reference or a supplied checkout and verifies SHA-256 `6b0603d10ef7aa4524558e06799e3e3c541eb52d94fbc1e82317f2de08804e6f` for its core implementation. The identical core bytes are in RankSeal revision `c27b8af7a9f982382a206d7a110e161a5ce4f4ca`. The top IDs, per-topic measurements and exact implementation hashes are in [rankseal.json](rankseal.json).

## Timing and baselines

The coordinator benchmark uses a single CPython 3.12.14 process on a Linux x86-64 host reporting an AMD EPYC 9V74. It preloads the rankings, warms all methods once per query and then shuffles method execution order across timed repeats. The tables report distributions of per-topic median elapsed times measured with `perf_counter_ns`. The p95 takes the nearest-rank 95th percentile. Each real query had three timed repeats. CPU scheduling was uncontrolled. All counts are exact for the specified inputs.

| Baseline in the JSON | Implementation |
|---|---|
| `exact_batch` | Exhaustive rational arithmetic using Python Fraction scores and heap selection. |
| `float_batch` | Exhaustive floating-point accumulation and heap selection. Rounding can change a tie. |
| `integer_batch` | Exhaustive exact integer arithmetic and heap selection. A warm table uses the least common multiple of all denominators in the known maximum rank window. |
| `fraction_nra` | Independent exact interval evaluation on every page, using Fraction scores. |
| `integer_nra` | The same streaming certificate with exact integers and a warm known-depth denominator table. |

Each report contains per-topic timings, input counts, machine information and hashes of the source and trace bytes used. Input counts and top IDs can be checked on another machine. Absolute timings vary with hardware and system load.

## Reproduce

Run from the repository root with Python 3.10 or later. Prepare the two traces with the repository's hash-verifying fetch script, saving them locally as `robust03.json` and `nfcorpus-video.json`. Generated trace files contain document IDs in rank order.

```sh
python benchmarks/fetch_replay.py robust03 robust03.json
python benchmarks/fetch_replay.py nfcorpus-video nfcorpus-video.json
python bench.py --trace robust03.json --page 32 --repeats 3
python bench.py --trace robust03.json --page 32 --repeats 3 --mode members
python bench.py --trace nfcorpus-video.json --page 32 --repeats 3
python bench.py --trace nfcorpus-video.json --page 32 --repeats 3 --mode members
python bench.py --queries 5 --repeats 5 --depth 512
python bench.py --workloads identical --sources 64 --depth 16384 --queries 1 --page 32 --repeats 3
python bench.py --workloads partial_overlap --sources 32 --depth 2048 --queries 3 --page 64 --repeats 3
python examples/rankseal_replay.py --rankseal benchmarks/reference --trace robust03.json --json rankseal-local.json
python examples/partition_replay.py --trace robust03.json --shards 8 --queries 100 --page 16 --repeats 3 --json partition-trace-local.json
python examples/partition_replay.py --shards 8 --rankers 3 --queries 5 --depth 512 --page 16 --repeats 3 --json partition-local.json
python examples/loopback.py --trace robust03.json --queries 12 --repeats 3 --json loopback-local.json
python examples/loopback.py --trace robust03.json --queries 12 --repeats 3 --page-ms 1 --item-us 10 --json loopback-paced-local.json
python examples/loopback.py --trace robust03.json --queries 12 --repeats 3 --page-ms 1 --item-us 100 --json loopback-expensive-local.json
```

`bench.py --json PATH` writes a raw report. The optional `--trace-limit N` uses the first N topics in file order for a quick check.

## Live SQLite and cursor retirement

The SQLite example uses four independent FTS5 tables, three BM25 configurations per table, deterministic row-ID tie breaks and one consistent database transaction. Seven illustrative searches fetched 612 rows with the shard map, 900 by solving each shard and merging its exact scores, and 4,281 using either unrestricted fusion or exhaustive retrieval. The corresponding cursor fetch counts were 90, 126 and 579. All results and receipts were checked against full rational fusion. The tests measure rows delivered to the consumer; SQLite may perform its ranking before the first fetch. A service offering scored shard-local top k directly has different network costs. [Raw SQLite observations](sqlite-fts.json)

In the asyncio cursor-retirement test, the losing readers occupied a median of 8.85 aggregate cursor-ms with retirement enabled and 1,193 cursor-ms otherwise. All 29 losing cursors closed before the deciding page in the pruning arm. This artificial slow-winner case measures resource release. [Raw cancellation observations](retirement.json)

```sh
python examples/sqlite_fts.py --shards 4 --query file --k 3 --page-size 8
python examples/retirement.py --help
```
