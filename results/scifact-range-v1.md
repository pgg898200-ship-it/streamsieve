# When can two balanced search shards skip a query?

November 27, 2026. The [Python adapter](../examples/range_top1.py) uses a shard's smallest possible document ID to decide an exact top-one RRF tie before opening that shard. The same decision works with RankFence or RankSeal. It needs stable, duplicate-free lists, disjoint document ownership, positive integer weights, a common rank constant and ascending string-ID tie order.

If every nonempty catalog ranker puts the same document first, its score is the sum of those rankers' weights divided by (c + 1). Every other catalog document ranks lower. The highest possible score in another shard is its total ranker weight divided by (c + 1). When that ceiling equals the catalog winner's score, a trusted ID lower bound can settle the tie: the catalog ID must sort strictly before the bound. The helper checks every rival shard. When evidence is insufficient, the SQLite adapter runs normal RankFence fusion.

For equally weighted shards, if the catalog lower bound already sorts at or after the archive lower bound, probing the catalog cannot establish this certificate. Passing both bounds bypasses the probe in that case. Bounds must cover *all* possible IDs in the same index generation. For example, a stale archive bound of z would incorrectly exclude a newly inserted document a that could beat b on a score tie. Pin the snapshot across the probe and fallback. In a RankPatch pipeline, a change of generation invalidates this evidence and requires fresh bounds and rankings.

## Use with SQLite

The supplemental files work with the v0.1.0 wheel. Run from the experiment checkout or copy both adapters into the same Python package. Give the two BM25 rankers to both shards.

```python
from examples.range_top1 import sqlite_first

ids, archive_skipped = await sqlite_first(
    [catalog_db, archive_db],
    [(title_sql, (term,)), (body_sql, (term,))],
    catalog_floor=catalog_min_id,
    archive_floor=archive_min_id,
)
```

Each SELECT returns a unique string ID first and defines its full deterministic ordering. The adapter appends LIMIT 1 for the probe, so pass SELECTs without a trailing semicolon or an existing LIMIT. If the probe fails, the fallback repeats the catalog query. With no trusted archive bound the adapter uses normal fusion. The benchmark caches both database-wide binary ID minima while building the immutable indexes. An application can use conservative lower bounds from its routing metadata.

## SciFact results

Both layouts have 670 catalog papers, 4,513 archive papers and two BM25 rankers per shard. Hash placement follows the prior experiment. Range placement assigns the lexicographically smallest 670 IDs to the catalog. Placement changes local BM25 statistics and ranking positions. Interpret speed and quality comparisons within each layout.

| Layout | Method | Catalog probes | Archive queries skipped | Median | p95 |
|---|---|---:|---:|---:|---:|
| range | RankFence eager | 0 | 0 | 9.14 ms | 17.83 ms |
| range | RankFence, archive bound | 300 | 200 | 1.86 ms | 19.34 ms |
| range | RankFence, both bounds | 300 | 200 | 1.88 ms | 19.10 ms |
| range | RankSeal eager | 0 | 0 | 9.68 ms | 16.83 ms |
| range | RankSeal, archive bound | 300 | 200 | 1.85 ms | 16.05 ms |
| range | RankSeal, both bounds | 300 | 200 | 1.92 ms | 16.31 ms |
| hash | RankFence eager | 0 | 0 | 9.88 ms | 19.49 ms |
| hash | RankFence, archive bound | 300 | 0 | 11.81 ms | 22.22 ms |
| hash | RankFence, both bounds | 0 | 0 | 9.89 ms | 19.86 ms |
| hash | RankSeal eager | 0 | 0 | 9.73 ms | 16.37 ms |
| hash | RankSeal, archive bound | 300 | 0 | 11.32 ms | 19.71 ms |
| hash | RankSeal, both bounds | 0 | 0 | 9.54 ms | 16.18 ms |

Range routing let both coordinators leave the archive unopened for 200 of 300 queries without removing a ranker. RankFence median latency fell from 9.14 to 1.88 ms, but p95 rose from 17.83 to 19.10 ms because inconclusive probes add work. Hash routing gave no eligible catalog ID: the smallest catalog ID was `10029891` and the smallest archive ID was `10009203`. Supplying both bounds avoided all 300 unproductive probes. Timing differences between the hash eager and two-bound arms should be read as run variation plus the small metadata check.

All 3,600 timed answers matched exhaustive rational RRF for its own layout. All methods returned a cited paper first on 97 of 300 range-partitioned claims and 121 of 300 hash-partitioned claims. This is a diagnostic for this retrieval objective. Partitioning by ID solely to enable skipping would change the rankings.

The experiment times connection and worker startup, SQL, fusion and cleanup for RankFence. RankSeal uses the same SQLite top-one probe followed, when needed, by its existing two-worker shard-local merge. Both implementations receive the same metadata and shortcut. The native RankFence baseline uses the balanced 4,096-instruction, per-shard worker configuration. Each of 300 distinct SciFact claims ran once per method and layout. Method and case order were shuffled; the exhaustive oracle ran immediately before each case, warming the filesystem cache. Nearest-rank p95 is the 285th sorted time. These are first-party, single-host, single-client results (Python 3.12.14, SQLite 3.53.1, x86-64). RankSeal can use the identical check, so the shortcut alone establishes no exclusive advantage.

## Independent reproduction

Run these commands from this checkout with Python 3.10+ and SQLite FTS5. The runner checks the archive SHA-256. The comparison script checks coverage, each winner, citation-hit flags and deterministic skip decisions before printing your p50 and p95.

```sh
curl -fL https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz -o scifact.tar.gz
python benchmarks/scifact_range_v1.py scifact.tar.gz > local.tsv
python benchmarks/compare_range_runs.py local.tsv
python -m unittest discover -q
```

An independent replication can be attributed when its owner publishes their run, environment, exact revision and raw observations. The existing [field-report thread](https://github.com/pgg898200-ship-it/streamsieve/issues/5) accepts that link, including negative results. [Benchmark runner](../benchmarks/scifact_range_v1.py) · [Paired observations](scifact-range-v1.tsv) · [Comparison script](../benchmarks/compare_range_runs.py) · [Correctness tests](../tests/test_range_top1.py) · [SciFact authors and dataset](https://github.com/allenai/scifact).

## Adoption evidence checked November 27, 2026

| Owner and public source | Observation | Classification |
|---|---|---|
| [PGG field reports](https://github.com/pgg898200-ship-it/streamsieve/issues/5), [GitHub PRs](https://github.com/pgg898200-ship-it/streamsieve/pulls?q=is%3Apr+RankFence) | No field comments or public forks. The two relevant PRs, 8 and 11, are by pgg898200-ship-it. Repository, issue and commit searches yielded no independent downstream owner. | First party |
| [grep.app](https://grep.app/api/search?q=rankfence) and [FileForce/files](https://github.com/FileForce/files/issues/4) | Indexed code matched only an unrelated rudzen/ChessLib identifier. No tool-name match or FileForce thread comments. Anonymous GitHub code search returned 401. | Partial coverage; no verified dependency or integration request |
| [Crossref](https://api.crossref.org/works?query.bibliographic=RankFence%20reciprocal%20rank%20fusion&rows=5) | Returned RRF literature without an identifiable RankFence research citation. Semantic Scholar returned 429. No independent replication found. | No verified citation |
| [PGG Hugging Face Space](https://huggingface.co/spaces/pgg-engineering/rankfence/tree/main) | RUNNING, demo served, zero discussions. Revision f5313a063ce1a90e2f1286eb1ad05bafc15c37e7. The browser showed Log In and public file access. No authorized free file-upload action was available. Hub name search found only the first-party Space. | First-party distribution |
| [MCPub](https://mcpub.dev/mcp), [official registry](https://registry.modelcontextprotocol.io/v0.1/servers/site.chatgpt.tancurl3.rankfence-tools-pgg%2Frankfence/versions/latest) | RankFence archive search returned one and live search returned zero. Manifest advertised the registered /rpc URL. The tool reproduced the expected two-of-four replay. | Listing and first-party trial |
| [Glama](https://glama.ai/mcp/connectors/site.chatgpt.tancurl3.rankfence-tools-pgg/rankfence), [LightNow](https://lightnow.ai/servers/site.chatgpt.tancurl3.rankfence-tools-pgg/rankfence), [AI Models Info](https://ai.tn/mcp/site-chatgpt-tancurl3-rankfence-tools-pgg--rankfence), [The World of AI](https://theworldofai.org/mcp/site-chatgpt-tancurl3-rankfence-tools-pgg-rankfence/) | Glama page contained the registered RPC endpoint. Other mirrors appeared in public search. | Directory coverage; no attributable installation |
| [mcp.tc](https://mcp.tc/s/IcqHr6iuDFW5EY1D), [MCP Bridge](https://github.com/stormlive-ai/mcp-bridge-docs/issues), [MCPServer.cc](https://mcpserver.cc/sitemap/sitemap_servers_1.xml) | mcp.tc remains pending human review; its registry search returned zero. No RankFence entry in 30 returned Bridge items or in the inspected MCPServer.cc sitemap. | No new verified placement or duplicate submission |
| [MCP Harbor](https://ai.mcpharbor.dev/submit) and [AgentNDX](https://agentndx.ai/submit/) | MCP Harbor timed out before submission. AgentNDX’s live keyword search returned zero. Its documented free intake accepted the tested endpoint information with HTTP 201, submission 573, status received. | AgentNDX review intake only |

Verified independent downstream repositories: 0 (delta 0). Private usage and unindexed dependencies are unknown. No independently attributable target-workload performance or defensibility evidence met the stopping criteria.
