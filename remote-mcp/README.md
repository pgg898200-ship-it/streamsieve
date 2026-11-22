# RankFence remote MCP replay

This stateless HTTP worker exposes one read-only tool, `rankfence_analyze`. Give it complete, stable ranked ID lists and a target k. It returns the ordered RRF winners, the number of entries a paged consumer would read before certification, and an exhaustive cross-check. The caller uploads every ID for this simulation. To stop live producers in your own service, use the Python coordinator in the repository root.

**Endpoint** [RankFence MCP](https://rankfence-tools-pgg.tancurl3.chatgpt.site/rpc)  
**Project** [RankFence guide and measured results](https://rankfence-pgg.tancurl3.chatgpt.site/guide.html)

| Tool input | Contract |
|---|---|
| `rankings` | One to 32 ranked lists of string IDs, with unique IDs within each list. Up to 512 IDs per list and 2,048 occurrences per request. |
| `k` | Integer from 1 through 100. |
| `page_size` | Simulated page length from 1 through 256. Defaults to 8. |
| `c` | RRF constant from 0 through 1,024. Defaults to 60. |
| `source_groups` | Optional, one string per list. IDs may overlap only among lists in the same group. |

For example, use these arguments to test two retrievers whose first result agrees.

```json
{"rankings":[["a","b"],["a","c"]],"k":1,"page_size":1}
```

Both rankings put `a` first. The replay certifies the winner after reading two of four entries. Every tied final score is resolved by ascending Unicode code-point order.

The public endpoint accepts MCP Streamable HTTP POST requests. A compatible MCP client can use the URL in [server.json](server.json). The worker accepts request bodies up to 256 KiB. Use the local Python API or browser workbench when the IDs should stay on your machine.

## Run the checks

The source at `dist/server/index.js` is an ES module exporting `{ fetch }` for a runtime that implements the Web Fetch API and BigInt. From this directory, use Node.js 24 to run the protocol and exactness checks.

```sh
node --test test.mjs
```

The timing and entry counts reported by the replay describe simulated CPU-side page rounds. The public worker accepts no source URLs and makes no outbound retrieval calls.

## Python coordinator evidence

[Independent synchronous-round audit](evidence/README.md) checks the local Python coordinator on 100 sharded Robust03 topics. The hosted JavaScript replay has separate tests in this directory.
