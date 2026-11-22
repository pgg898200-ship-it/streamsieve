---
title: RankFence
emoji: 🪜
colorFrom: green
colorTo: gray
sdk: static
app_file: index.html
license: mit
short_description: Stop fetching ranked pages when the exact winners are proven.
tags:
- reciprocal-rank-fusion
- hybrid-search
- information-retrieval
- retrieval-augmented-generation
- model-context-protocol
---

# RankFence

**Stop fetching search results when the next page cannot change the winners.** RankFence combines stable keyword and vector rankings with unweighted reciprocal rank fusion (RRF). Place the Python coordinator between paged retrievers and a reranker or RAG context builder. With trusted, disjoint shard namespaces it can also retire individual shards.

[Replay two example queries](https://huggingface.co/spaces/pgg-engineering/rankfence) · [Test your own rankings](https://rankfence-pgg.tancurl3.chatgpt.site/workbench.html) · [Python source and integration](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0)

## What does the demo count?

Four cursors cover two shards, with three ranked IDs per cursor. Each round reads one ID from every cursor. When two retrievers agree on the first western result, four IDs suffice to establish the winner. In the split-first-choice example all 12 IDs are required. Both results match exhaustive fusion.

The replay receives the full lists up front. Its read count measures a possible stopping point for live cursors. Latency and avoided backend work depend on how the retriever implements paging.

## Try it from an agent

The public MCP server accepts complete rankings via `rankfence_analyze`. Use the same group label for sources that may return the same document. Omit the groups when ownership is unknown.

```json
{"rankings":[["west/alpha","west/beta","west/gamma"],["west/alpha","west/gamma","west/beta"],["east/delta","east/epsilon","east/zeta"],["east/epsilon","east/zeta","east/delta"]],"source_groups":["west","west","east","east"],"k":1,"page_size":1}
```

Expected output includes winner `west/alpha`, four of 12 ranked IDs read in replay, and agreement with exhaustive fusion. The Python package also connects to live async cursors and supports independently verifiable receipts.

[Public MCP endpoint](https://rankfence-tools-pgg.tancurl3.chatgpt.site/rpc) · [MCP setup and tool contract](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0#let-an-agent-measure-its-own-workload) · [Benchmark methods and results](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0/results)

