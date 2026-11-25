# RankFence for hybrid search

How many ranked results does your search service need to read before the top result is certain? RankFence answers that question for reciprocal rank fusion (RRF). Give it complete, stable lists of document IDs from your retrievers. It replays those lists one page at a time, reports the first round that certifies the winners, and checks them against exhaustive fusion.

[Run two public examples](https://rankfence-tools-pgg.tancurl3.chatgpt.site/) to see both stopping points. One query settles after 2 of 8 IDs. The other needs all 6. Each button calls the public MCP service with the displayed inputs.

## Replay one search with and without a shard map

Connect a Streamable HTTP MCP client to `https://rankfence-tools-pgg.tancurl3.chatgpt.site/rpc` and call `rankfence_analyze` with the JSON below. You can also paste it into the [browser workbench](https://rankfence-pgg.tancurl3.chatgpt.site/workbench.html).

A billing index and a documentation index each return a keyword ranking and a vector ranking. The two indexes own different documents. The source groups tell RankFence which lists can contain the same ID.

```json
{
  "rankings": [
    ["invoice", "receipt"],
    ["invoice", "receipt"],
    ["guide", "manual"],
    ["manual", "guide"]
  ],
  "source_groups": ["billing", "billing", "docs", "docs"],
  "k": 1,
  "page_size": 1
}
```

Run it once as shown, then remove `source_groups` and run it again. Both calls receive all eight entries. They report how far a consumer of paged results would have to read.

| Input | Winning ID | Ranked IDs read in replay | Matches exhaustive RRF |
| --- | --- | ---: | --- |
| With the shard map | `invoice` | 4 of 8 | Yes |
| Same rankings, no map | `invoice` | 8 of 8 | Yes |

Both billing retrievers rank `invoice` first. Once the first page of each list arrives, the map rules out any additional billing points for `guide` and `manual`. Without that guarantee, either could still appear on the billing lists. Omit the map whenever document ownership is uncertain.

The hosted tool accepts up to 32 lists, 512 IDs per list, and 2,048 ID occurrences in total. Use the browser workbench or local stdio server to keep document IDs on your machine.

| What you need | Where to find it |
|---|---|
| Hosted replay implementation | [Version 0.1.0 source](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-mcp-0.1.0/remote-mcp) · [Protocol and exactness tests](https://github.com/pgg898200-ship-it/streamsieve/blob/05e1c34faeb120c962b535e7b264d1dd063ef3c2/remote-mcp/test.mjs) · [Official MCP Registry record](https://registry.modelcontextprotocol.io/v0.1/servers/site.chatgpt.tancurl3.rankfence-tools-pgg%2Frankfence/versions/0.1.0) |
| Local stdio replay and receipt verification | [Python package and MCP client configuration](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0#let-an-agent-measure-its-own-workload) |
| Live retrieval | [Progressive file-search example](https://github.com/pgg898200-ship-it/streamsieve/blob/c7e35ccb5563ca7b115b4f34da74c8b4019be6bd/examples/progressive_file_search.py) · [Async cursor adapter](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0#use-live-retrieval-cursors) |

For live queries, run the Python coordinator beside your retrievers. It can close unfinished cursors once it proves the requested result. That saves backend work when a producer fetches lazily or honors cancellation. The hosted replay measures simulated reads on the lists you supply.

[Compare SQLite FTS5 query latency and reproduce the measurements](https://github.com/pgg898200-ship-it/streamsieve/blob/1957c64233a949027e28a08105d59758cdadc2f8/results/sqlite-tail-v1.md). The benchmark times RankFence, shard-local RankSeal and exhaustive fusion through cursor cleanup, including queries where early stopping costs more time.

[Connect cancellable SQLite shard cursors](https://github.com/pgg898200-ship-it/streamsieve/blob/5d0d61157c7047aac41598f8337ed1ad3da5da51/results/sqlite-interrupt-v1.md) with the Python adapter. The paired benchmark includes interruption of a running FTS5 query, a required archive read and an empty result.

[Try deferred shard startup in the Python 0.2.0a1 preview](https://github.com/pgg898200-ship-it/streamsieve/tree/dcf5a22e9c00870a5edd255757df7d3571061f58). The SciFact reproduction includes 300 queries, a shard-local RankSeal comparison, and cited-paper hit counts for both ranking layouts.
