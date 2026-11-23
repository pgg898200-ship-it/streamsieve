# SQLite FTS5 tail latency, November 23, 2026

Four disjoint in-memory indexes contain 4,926 Python documentation records, with three BM25 field weightings per shard. Each query uses the same databases, SQL, rowid tie-break and read snapshot across methods. RankFence and shard-local RankSeal use unit weights, eight-ID pages and c=60. RankSeal resolves each shard's top k and the winners' scores before merging. Batch scans all rows using Python Fraction scores. RankPatch targets replacements and withdrawals during ranking updates, which these immutable snapshots do not exercise.

| Query | k | RankFence p95 ms | RankSeal local p95 ms | Exact batch p95 ms | IDs fetched, Fence / Seal / batch |
|---|---:|---:|---:|---:|---|
| file | 1 | 5.087 | 5.044 | 10.947 | 96 / 96 / 1,605 |
| file | 10 | 6.351 | 4.983 | 10.110 | 192 / 480 / 1,605 |
| exception | 1 | 1.823 | 2.552 | 3.559 | 96 / 120 / 594 |
| exception | 10 | 2.269 | 2.662 | 4.398 | 192 / 192 / 594 |
| absent term | 1 | 0.558 | 0.303 | 0.119 | 0 / 0 / 0 |
| absent term | 10 | 0.855 | 0.426 | 0.265 | 0 / 0 / 0 |

Times include SQL execution, Python fusion and cursor closure. SQLite may score and sort all matches before delivering a page. The experiment measures rows crossing the cursor and elapsed client time. It does not measure interrupted SQLite computation. Forty warm observations per cell, after two warmups, use randomized method order and nearest-rank percentiles. All 720 measured runs matched exhaustive ordered IDs. These repeated fixed queries characterize one host with synchronous SQLite calls. Production concurrency, independent backend cancellation and generalization across query distributions remain open.

Run from this checkout with Python and SQLite FTS5:

```sh
python benchmarks/sqlite_tail_v1.py > sqlite-tail-v1-local.json
```

Compare the corpus digest and software versions before comparing timings. The installed Python standard library supplies the corpus. The raw samples, winning IDs and method-level row counts are in [sqlite-tail-v1.json](sqlite-tail-v1.json). The runner is [sqlite_tail_v1.py](../benchmarks/sqlite_tail_v1.py).

## Adoption evidence checked November 23, 2026

| Public owner or publisher | Observation | Classification |
|---|---|---|
| [LightNow](https://lightnow.ai/servers/site.chatgpt.tancurl3.rankfence-tools-pgg/rankfence) and [AI Models Info](https://ai.tn/mcp/site-chatgpt-tancurl3-rankfence-tools-pgg--rankfence) | Each reproduces the registered identity and remote configuration | Registry mirrors |
| [MCPub](https://mcpub.dev/mcp) | RankFence search returned one archive record and zero live records | Directory placement |
| [pgg-engineering](https://huggingface.co/spaces/pgg-engineering/rankfence) | Public Space running, demo served, checksum manifest matches release | First-party distribution |
| [FileForce/files, issue by vitalics](https://github.com/FileForce/files/issues/4) | Hybrid search architecture discussion, zero comments and no RankFence request identified | Adjacent use case |
| [stormlive-ai](https://github.com/stormlive-ai/mcp-bridge-docs/issues) | No RankFence issue found in the queue. Submission through documented route returned GitHub 403 | No placement |
| [pgg898200-ship-it](https://github.com/pgg898200-ship-it/streamsieve/issues/5) | Zero field-report comments. GitHub repository, issue and commit searches identified no independent use | First-party evidence only |
| [rudzen/ChessLib](https://grep.app/api/search?q=rankfence) | Sole indexed code match was a chess identifier | Unrelated |

Verified independent downstream repositories: 0 (change 0). No verified independent dependency declaration, integration request, PR, research citation or benchmark replication. Anonymous GitHub code search returned 401. Indexed code and web searches have partial coverage. No independent defensibility result.
