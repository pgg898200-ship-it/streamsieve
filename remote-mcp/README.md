# RankFence for hybrid search

How many ranked results does your search service need to read before the top result is certain? RankFence answers that question for reciprocal rank fusion (RRF). Give it complete, stable lists of document IDs from your retrievers. It replays those lists one page at a time, reports the first round that certifies the winners, and checks them against exhaustive fusion.

**Try it in the [browser workbench](https://rankfence-pgg.tancurl3.chatgpt.site/workbench.html)** or connect a Streamable HTTP MCP client to `https://rankfence-tools-pgg.tancurl3.chatgpt.site/rpc`. Call `rankfence_analyze` with these arguments:

```json
{"rankings":[["a","b"],["a","c"]],"k":1,"page_size":1}
```

Both retrievers rank `a` first. The replay certifies `a` after reading two of the four entries. The hosted tool receives all four IDs. Use the browser workbench or local stdio server to keep your IDs on your machine.

| What you need | Where to find it |
|---|---|
| Hosted replay implementation | [Version 0.1.0 source](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-mcp-0.1.0/remote-mcp) · [Protocol and exactness tests](https://github.com/pgg898200-ship-it/streamsieve/blob/05e1c34faeb120c962b535e7b264d1dd063ef3c2/remote-mcp/test.mjs) · [Official MCP Registry record](https://registry.modelcontextprotocol.io/v0.1/servers/site.chatgpt.tancurl3.rankfence-tools-pgg%2Frankfence/versions/0.1.0) |
| Local stdio replay and receipt verification | [Python package and MCP client configuration](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0#let-an-agent-measure-its-own-workload) |
| Live retrieval | [Progressive file-search example](https://github.com/pgg898200-ship-it/streamsieve/blob/c7e35ccb5563ca7b115b4f34da74c8b4019be6bd/examples/progressive_file_search.py) · [Async cursor adapter](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0#use-live-retrieval-cursors) |

For live queries, run the Python coordinator beside your retrievers. It can close unfinished cursors once it proves the requested result. That saves backend work when a producer fetches lazily or honors cancellation. The hosted replay measures simulated reads on the lists you supply.
