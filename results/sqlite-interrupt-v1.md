# Cancelling an active SQLite search on a disjoint shard

November 24, 2026. The [Python adapter](../examples/sqlite_interrupt.py) runs each ordered SELECT on a dedicated worker and read-only SQLite connection. If the RankFence coordinator cancels a page request during sqlite3_step, a progress callback stops the SQLite VM. The adapter waits for connection closure and worker shutdown before returning. With a cached file or index that keeps changing, acquire stable snapshots covering the same query generation before starting any sources. Supply globally unique IDs and a deterministic sort tie-break.

Install the pinned v0.1.0 wheel and copy the adapter into your application, or run directly in this checkout. For example, with two stable SELECT statements that return a string ID first:

```python
from examples.sqlite_interrupt import sqlite_pages
from rankfence import fuse_partitioned

async def first_hit(catalog, archive, lexical_sql, title_sql, query):
    sources = [
        sqlite_pages(catalog, lexical_sql, (query,)),
        sqlite_pages(catalog, title_sql, (query,)),
        sqlite_pages(archive, lexical_sql, (query,)),
    ]
    return (await fuse_partitioned(sources, ["catalog", "catalog", "archive"], 1)).ids
```

Each catalog ID can score in two lists. The best possible archive ID can score in only one. Two catalog rankers agreeing on their first ID can therefore certify the winner before the archive supplies its first page. RankSeal supports flat weighted fusion. Comparisons with its local merge are in the [previous FTS5 trial](sqlite-tail-v1.md). Use RankPatch for mutable source rankings. A replacement invalidates a proof based on prior stable prefixes.

## Matched interruption experiment

Two catalog BM25 queries (70 generated documents) and one archive BM25 query (30,000 generated documents) search read-only FTS5 indexes. Both arms use the same coordinator, pruning rule, page width of 16, real SELECTs, worker model and instrumented progress handler. Only the handler's response to cancellation changes. The control allows the current SQL step to finish, then closes. The timed region includes connection and thread startup, SQL, fusion, closure and worker join. Corpus construction, the independent exact Fraction oracle and winner checks are outside it.

| Case | Interrupt p95 ms | Finish step p95 ms | Archive SQL interruptions | Median archive progress callbacks, interrupt / control |
|---|---:|---:|---:|---:|
| Two catalog rankers agree | 2.09 | 30.76 | 40 / 40 | 1 / 3,048 |
| Archive result required | 29.76 | 27.78 | 0 / 40 | 3,048 / 3,048 |
| All sources empty | 1.02 | 0.99 | 0 / 40 | 0 / 0 |

SQLITE_INTERRUPT on the worker confirms actual backend interruption. Progress callbacks are scheduled every 256 VM instructions and give a coarse work measure. All 240 measured results matched exhaustive ordered fusion. Forty paired repetitions per workload used randomized arm order after two warmup pairs. Reported p95 is the 38th sorted elapsed time per 40 runs. This synthetic corpus deliberately aligns the two catalog rankings. These measurements establish an integration mechanism on one host. The proportion of queries exhibiting this pattern in production and cross-host tail performance remain unknown. SQLite interruptibility also depends on the operation reaching a progress callback.

```sh
python -m unittest discover -q
python benchmarks/sqlite_interrupt_v1.py > my-sqlite-interrupt-v1.json
```

[Runner](../benchmarks/sqlite_interrupt_v1.py) · [All observations, runtime versions and winners](sqlite-interrupt-v1.json) · [Finite cursor, error and in-flight cancellation tests](../tests/test_sqlite_interrupt.py)

## Public evidence checked November 24, 2026

| Owner and source | Verified observation | Attribution |
|---|---|---|
| [PGG field reports](https://github.com/pgg898200-ship-it/streamsieve/issues/5) and [GitHub PRs](https://github.com/pgg898200-ship-it/streamsieve/pulls?q=is%3Apr+RankFence) | Zero field comments, relevant PRs #8 and #11 by pgg898200-ship-it, zero public streamsieve forks. Repository and commit searches found no outside downstream owner. | First party |
| [grep.app](https://grep.app/api/search?q=rankfence) | No RankFence dependency declaration observed. Name search found only a chess identifier owned by rudzen, tool-name search found no indexed code. | Partial index, unrelated hit excluded |
| [FileForce/files](https://github.com/FileForce/files/issues/4) | No feedback in the thread on vitalics's retrieval architecture issue | No RankFence integration request |
| [Glama](https://glama.ai/mcp/connectors/site.chatgpt.tancurl3.rankfence-tools-pgg/rankfence), [LightNow](https://lightnow.ai/servers/site.chatgpt.tancurl3.rankfence-tools-pgg/rankfence), [AI Models Info](https://ai.tn/mcp/site-chatgpt-tancurl3-rankfence-tools-pgg--rankfence) | Dedicated Glama connector references the registered endpoint. Other two registry mirrors remain discoverable. | Third-party listings |
| [MCPub](https://mcpub.dev/mcp) | search("RankFence") returned one /rpc entry, search_live returned zero. Public manifest advertised Streamable HTTP and registered tool returned the expected winner. | Directory placement and first-party functional check |
| [MCPServer.cc](https://mcpserver.cc/submit) | Checked directory sitemap and indexed search for duplicates. Review form returned HTTP 200, code 0 and receipt 3306727c-5dd1-4031-8456-cf00f109e030. | Submitted, publication unverified |
| [MCP Bridge / stormlive-ai](https://github.com/stormlive-ai/mcp-bridge-docs/issues) | No RankFence entry among 28 returned queue items. No repeat submission. | No verified placement |
| [Hugging Face / pgg-engineering](https://huggingface.co/spaces/pgg-engineering/rankfence) | RUNNING at f5313a063ce1a90e2f1286eb1ad05bafc15c37e7, static demo served, zero public discussions. No free direct file-upload action exposed through the connected account. | First-party distribution |

Verified independent downstream repositories 0, change 0. Public searches found no independent deployment, substantive citation, benchmark replication, or RankFence-specific integration request. Private usage and unindexed code are unknown. The adapter and timings are first-party evidence only.
