# When can a search skip opening a shard?

November 25, 2026. RankFence 0.2.0a1 adds an opt-in first-pull delay. Pass `defer_groups=("archive",)` to `fuse_partitioned` to give other sources a chance to settle the result. The coordinator includes the archive in its score bounds from the beginning. If its ranking could still change the answer, the coordinator starts it once the initial sources have each returned a page or retired. Existing calls start every source immediately.

Use independent, lazy sources for this option. A source should open its backend on first pull. A slow initial source can delay an archive that the query needs. The caller supplies the same stable ranking and disjoint ownership guarantees as in v0.1.0. RankSeal supports weighted flat inputs. RankPatch handles source replacements, which require a new snapshot before this coordinator can certify a result.

## SQLite FTS5 on all 300 SciFact development claims

We indexed the actual titles and abstracts of 5,183 papers. A fixed CRC32 split placed 670 in the catalog and 4,513 in the archive. Every query searched both namespaces logically. Two catalog BM25 rankers weighted titles and abstracts differently. The archive received either one or both rankers. Each method within a layout had identical queries, score semantics and tie order.

| Rankers, catalog + archive | Finish in-flight SQL, p95 | Interrupt SQL, p95 | Defer archive, p95 | RankSeal local merge, p95 | Deferred archive never started | Top result in claim’s cited IDs |
|---|---:|---:|---:|---:|---:|---:|
| 2 + 1 | 11.31 ms | 5.09 ms | 3.94 ms | 4.20 ms | 300 / 300 | 21 / 300 |
| 2 + 2 | 23.64 ms | 24.66 ms | 24.44 ms | 16.89 ms | 0 / 300 | 121 / 300 |

In the first layout a paper from the archive can earn only one reciprocal-rank contribution. This makes the archive easy to rule out and heavily favors catalog papers. All four methods returned identical winners within each layout. Removing a ranker to reproduce the faster topology changed the retrieval objective and cut cited-paper hits from 121 to 21. Cited-paper hit rate is a diagnostic for these claims, not a SciFact fact-verification score.

With the asymmetric layout, eager interruption stopped a running archive query on 299 claims. Deferring beat eager interruption on 275 paired claims. With equal ranker counts it beat eager interruption on 153 claims, and RankSeal local merge had the lowest p95. At p50 in the asymmetric layout, RankSeal also finished faster than deferral (2.40 versus 2.52 ms).

**Measurement.** Single host, Python 3.12.14 and SQLite 3.53.1, one timed execution per method per query and layout, randomized case and arm order. Nearest-rank p95 is the 285th time among 300 distinct queries. All 2,400 winners matched exhaustive rational RRF. The oracle ran before each case, warming the filesystem cache. Timing includes SQL, connection setup, fusion, cursor closure and RankFence worker shutdown. RankSeal used two shared-pool workers, one per shard, resolved each local winner and exact score, and used SQL LIMIT 1 for the one-ranker shard. Its reusable pool and different cursor strategy are part of this implementation comparison. The three RankFence arms isolate scheduling and interruption. Measurements cover warm single-query latency, without server load or independent replication.

**Reproduce.** Check out this experiment branch. The runner checks the input SHA-256 before accessing the two JSONL members. Only IDs, counts and timings are redistributed.

```sh
curl -fL https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz -o scifact.tar.gz
python benchmarks/scifact_defer_v1.py scifact.tar.gz > local.tsv
python -m unittest discover -q
```

[Paired observations](scifact-defer-v1.tsv) · [Runner and SQL settings](../benchmarks/scifact_defer_v1.py) · [Prerelease wheel and checksum](../releases/deferred-0.2.0a1) · [SciFact project and authors](https://github.com/allenai/scifact) · [Previous generated-workload control](sqlite-interrupt-v1.md)

## Public evidence checked on November 25

| Owner or publisher | Evidence | Attribution |
|---|---|---|
| [PGG field reports](https://github.com/pgg898200-ship-it/streamsieve/issues/5), [GitHub issue index](https://github.com/search?q=%22rankfence%22&type=issues) | Zero field comments and public forks. Relevant PRs 8 and 11 were first-party. Repository, commit and issue searches produced no independent downstream owner. | First party |
| [grep.app](https://grep.app/api/search?q=rankfence) and [FileForce/files](https://github.com/FileForce/files/issues/4) | Sole indexed code match was an unrelated rudzen/ChessLib identifier. No tool-name hit. FileForce's architecture thread had no comments or RankFence request. | No verified dependency or inbound request |
| [The World of AI](https://theworldofai.org/mcp/site-chatgpt-tancurl3-rankfence-tools-pgg-rankfence/) and [Glama](https://glama.ai/mcp/connectors/site.chatgpt.tancurl3.rankfence-tools-pgg/rankfence) | Directory pages returned successfully. The World of AI page includes the registered RPC URL. AI Models Info and LightNow still appeared in search. | Registry mirrors |
| [MCPub](https://mcpub.dev/mcp) and [official registry](https://registry.modelcontextprotocol.io/v0.1/servers/site.chatgpt.tancurl3.rankfence-tools-pgg%2Frankfence/versions/latest) | Archive search returned one, live search zero. Registered tool returned the expected winner after four of eight replayed IDs. | Listing and first-party verification |
| [MCPServer.cc](https://mcpserver.cc/sitemap/sitemap_servers_1.xml), [MCP Bridge](https://github.com/stormlive-ai/mcp-bridge-docs/issues), [shortformkit](https://github.com/shortformkit/free-remote-mcp-servers/blob/main/data/servers.csv) | No match in inspected sitemap, 29-item Bridge queue, or shortformkit CSV. Yesterday's form receipt establishes intake only. No duplicate submitted. | No verified placement on these surfaces |
| [pgg-engineering Space](https://huggingface.co/spaces/pgg-engineering/rankfence) | Public demo served, runtime RUNNING, zero discussions. Revision f5313a063ce1a90e2f1286eb1ad05bafc15c37e7. No free authorized direct upload available in this environment. | First-party distribution |

Publicly verified downstream repositories 0, change 0. Searches found no independent deployment, substantive research citation or replication. The independent SciFact authors supplied the workload and did not run this benchmark. Registry referrals are discoverability evidence only. Public indexing has incomplete coverage.
