---
title: RankFence
short_description: Stop paging search shards when the top documents are known
emoji: 🪜
colorFrom: green
colorTo: gray
sdk: static
app_file: index.html
license: mit
tags:
  - reciprocal-rank-fusion
  - hybrid-search
  - information-retrieval
  - rag
  - mcp
---

# RankFence

**How many search results do you need to read before the winners are certain?** RankFence sits between paged keyword or vector retrievers and your reranker or RAG context builder. It uses reciprocal rank fusion (RRF) to return the exact ordered top documents. A trusted map of disjoint document namespaces lets it close the cursors on losing shards.

[Replay a query](https://pgg-engineering-rankfence.hf.space/workbench.html) · [Integration guide and Python source](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0) · [Benchmark results](https://pgg-engineering-rankfence.hf.space/benchmarks.html)

The Space runs in your browser. Paste complete ranked lists or use the example to compare the IDs read with and without a shard map. The replay checks the answer against exhaustive fusion. In a live pipeline, use the Python adapter with stable paged cursors. Remote savings depend on the backend supporting lazy reads or cancellation.

On 100 historical TREC Robust03 topics split into eight simulated shards, RankFence read 78,956 of 400,000 ranked IDs. Its ordered top five matched exhaustive fusion on every query. Independent shard fusion read 206,934 IDs for the same answers. [Read the raw measurements](https://raw.githubusercontent.com/pgg898200-ship-it/streamsieve/9107326cf0993e842c281d412cb276e83eb1c6a4/results/rankseal.json)

Agents can call the local stdio MCP tool `rankfence_analyze` with an array of ranked ID lists, `k`, and an optional map of disjoint source groups. It reports ordered winners, simulated reads and agreement with exhaustive RRF. [Example tool input and output](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0#let-an-agent-measure-its-own-workload)
