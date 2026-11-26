# SQLite workers and cancellation under concurrent retrieval

November 26, 2026. This supplemental adapter adds one SQLite worker per shard as an alternative to one worker per ranker. Each ranker keeps its own read-only connection, cursor and cancellation signal. Within a shard the worker serializes SQL pulls. Separate shards run concurrently. Each query owns its workers and closes them on exit. A suspended sibling cursor can resume after another cursor on the same worker receives SQLITE_INTERRUPT.

[Download the adapter](../examples/sqlite_interrupt.py) and use it alongside the released RankFence Python wheel. Each SELECT must return a unique string ID in column zero, use a deterministic total order, and read data that stays unchanged for the query. The group labels assert that different shards own disjoint IDs across all their rankings. Use this interface only when that ownership is known.

```python
from rankfence import fuse_partitioned
from sqlite_interrupt import sqlite_shard_pages

async def first_hit(catalog_path, archive_path, title_sql, body_sql, query):
    queries = [(title_sql, (query,)), (body_sql, (query,))]
    async with (
        sqlite_shard_pages(catalog_path, queries) as catalog,
        sqlite_shard_pages(archive_path, queries) as archive,
    ):
        return (await fuse_partitioned(
            [*catalog, *archive], ["catalog", "catalog", "archive", "archive"], 1,
        )).ids
```

Both cursor adapters accept `progress_ops`, the approximate number of SQLite VM instructions between Python progress callbacks. The default is 256. A longer interval gives SQL more time between checks and can delay cooperative cancellation. Each constraint is per statement. It is not a wall-clock deadline.

## Matched comparison

The balanced layout from [the previous SciFact experiment](scifact-defer-v1.md) is fixed for every arm: 670 catalog papers, 4,513 archive papers, two BM25 rankers on each shard, the same 300 development claims and exact top one. All sources are eligible to start immediately. Each query runs at both loads in each arm. Each block releases either one request or four simultaneous requests. Arm order within blocks and block order are randomized. Every method uses identical immutable data, ranker weights, tie order and a page width of 16.

| Method | 1 client p95 | 4 clients p95 | 4-client throughput |
|---|---:|---:|---:|
| RankFence, worker per ranker, 256 ops | 26.07 ms | 89.46 ms | 57.9 queries/s |
| RankFence, worker per ranker, 4,096 ops | 15.01 ms | 45.36 ms | 123.0 queries/s |
| RankFence, worker per shard, 256 ops | 26.73 ms | 75.08 ms | 71.4 queries/s |
| RankFence, worker per shard, 4,096 ops | 20.07 ms | 37.77 ms | 131.8 queries/s |
| RankSeal, shard-local merge | 18.10 ms | 27.56 ms | 179.9 queries/s |

With four concurrent queries, sharing workers at the same 4,096-instruction interval reduced RankFence p95 from 45.36 to 37.77 ms. RankSeal local merge still finished fastest at that load. With one client the order changed: a worker per ranker at 4,096 instructions achieved 15.01 ms, ahead of shard sharing and RankSeal. Increasing the check interval also improved both RankFence implementations at both loads on this host.

Timing starts at common request release and includes connection setup, sorting, fusion and cursor closure. The RankFence adapters also join their query-scoped workers. The pinned RankSeal sibling uses a reusable pool with one callable per shard, resolves the shard winner's exact score and merges candidates. Differences relative to RankSeal include worker lifetime and local versus global fusion. The four RankFence arms separate worker count from progress callback frequency. No RankPatch replacements occur on these immutable snapshots. Revision or withdrawal of a ranking requires a new snapshot.

p95 is the 285th ordered latency among 300 queries in each cell. Each claim contributes one execution per cell. Reported throughput divides completed requests by the sum of block durations, counting each block once. This measures warm, fixed bursts on one x86-64 host (Python 3.12.14, SQLite 3.53.1), with no network hop or sustained arrival process. Index construction and an exhaustive rational-score check for every claim run before the timed blocks. All 3,000 timed winners matched that oracle.

In a separate paired probe, cancellation interrupted a running recursive SQL statement before its first row in all 200 measured trials. From task cancellation through worker cleanup, p95 was 0.334 ms at 256 instructions and 0.345 ms at 4,096. This probe measures an active SQLite VM after at least two callbacks, not blocked I/O or a worst-case deadline.

## Reproduce

```sh
curl -fL https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz -o scifact.tar.gz
python benchmarks/scifact_workers_v1.py scifact.tar.gz > local-workers.tsv
python benchmarks/sqlite_cancel_delay_v1.py > local-cancellation.tsv
python -m unittest discover -q
```

The retrieval runner verifies the source archive digest before building its indexes. [SciFact authors and dataset](https://github.com/allenai/scifact) · [Retrieval runner](../benchmarks/scifact_workers_v1.py) · [Raw paired request timings and block order](scifact-workers-v1.tsv) · [Cancellation probe](../benchmarks/sqlite_cancel_delay_v1.py) · [Raw cancellation samples](sqlite-cancel-delay-v1.tsv) · [Regression tests](../tests/test_sqlite_shard_workers.py).

## Independent evidence checked November 26, 2026

| Owner and source | Verified observation | Attribution |
|---|---|---|
| [PGG field reports](https://github.com/pgg898200-ship-it/streamsieve/issues/5), [PRs](https://github.com/pgg898200-ship-it/streamsieve/pulls?q=is%3Apr+RankFence) | No field comments or public forks. Relevant PRs 8 and 11 were by pgg898200-ship-it. Repository, issue and commit searches identified no third-party dependency. | First party |
| [grep.app](https://grep.app/api/search?q=rankfence), [FileForce/files](https://github.com/FileForce/files/issues/4) | One unrelated chess code match belonging to rudzen. No tool-name hits. No FileForce feedback on the architecture thread. Anonymous GitHub code search returned 401. | Partial index coverage |
| [JulioPeixoto/softrag](https://github.com/JulioPeixoto/softrag/blob/2afe85c5dfb1c1180d25b0f309633afa521488d5/src/softrag/retrieval.py) | Its hybrid path materializes weighted vector and keyword candidates before fusion. Its public issue list was empty. A RankFence adapter at the fusion call would have no pending retrieval to cancel. | Evaluated fit, no inbound request or submission |
| [mcp.tc](https://mcp.tc/s/IcqHr6iuDFW5EY1D) | Checked its registry for duplicates, then sent the tested public endpoint once through the documented free API. Status reported the server was found and awaits human review. | Submission acknowledged, publication unverified |
| [MCPub](https://mcpub.dev/mcp), [official registry](https://registry.modelcontextprotocol.io/v0.1/servers/site.chatgpt.tancurl3.rankfence-tools-pgg%2Frankfence/versions/latest) | Archive search returned one entry, live search zero. Manifest advertised the registered transport. Registered tool returned winner a after two of four supplied IDs in replay. | Listing and first-party check |
| [MCP Bridge](https://github.com/stormlive-ai/mcp-bridge-docs/issues), [MCPServer.cc](https://mcpserver.cc/sitemap/sitemap_servers_1.xml) | No RankFence match in 29 returned Bridge queue items or the inspected MCPServer.cc sitemap. Prior MCPServer.cc receipt remains evidence of intake only. | No duplicate submission |
| [PGG Hugging Face Space](https://huggingface.co/spaces/pgg-engineering/rankfence) | RUNNING at f5313a063ce1a90e2f1286eb1ad05bafc15c37e7, demo served, zero discussions. No free authorized file upload route exposed. | First-party distribution |
| [Glama](https://glama.ai/mcp/connectors/site.chatgpt.tancurl3.rankfence-tools-pgg/rankfence), [LightNow](https://lightnow.ai/servers/site.chatgpt.tancurl3.rankfence-tools-pgg/rankfence), [AI Models Info](https://ai.tn/mcp/site-chatgpt-tancurl3-rankfence-tools-pgg--rankfence), [The World of AI](https://theworldofai.org/mcp/site-chatgpt-tancurl3-rankfence-tools-pgg-rankfence/) | Glama connector served and public search returned registry mirrors. No attributable end-user installation or measured referral conversion. | Directories |
| [Crossref bibliographic search](https://api.crossref.org/works?query.bibliographic=RankFence%20reciprocal%20rank%20fusion&rows=5) | Returned RRF literature without an identifiable RankFence citation. OpenAlex rate-limited the additional search. | No verified independent paper or replication |

Verified downstream repositories: 0, change 0. Public sources did not establish a third-party installation, substantive research citation, independent benchmark replication, or defensibility advantage. Private usage is unknown. The dataset has independent authorship. All measurements here were run by the project.
