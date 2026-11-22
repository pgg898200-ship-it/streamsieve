# RankFence for GitHub Copilot CLI

Ask Copilot how far two ranked lists must be read before their combined top result is certain. The plugin runs the RankFence replay and receipt verifier as a local MCP server. It reads the complete lists you supply and simulates page delivery. Use the [Python coordinator](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0) to stop live retrieval cursors in your application.

## Install

With GitHub Copilot CLI, Python 3.10 or newer and [uv](https://docs.astral.sh/uv/) available, run these commands.

```sh
copilot plugin marketplace add pgg898200-ship-it/streamsieve
copilot plugin install rankfence@pgg-engineering
```

Start a new Copilot CLI session and enter `/mcp` to inspect the tools. On first use, uv fetches the 25 KB wheel from a fixed GitHub revision and checks its SHA-256 digest. The [server configuration](mcp.json) contains the exact command and arguments.

## Try a replay

> Use the RankFence analyze tool with rankings [["a","b"],["a","c"]], k=1 and page_size=1. Show the winning ID, how many entries the replay read, and whether exhaustive fusion agreed.

Both retrievers ranked **a** first. The replay reads **2 of 4** entries and returns `matches_exhaustive: true`. The result also includes a receipt that `rankfence_verify` can check. Try changing the second list to `["c","a"]` to see why a different first page can delay the decision.

Run `copilot plugin uninstall rankfence` to remove the plugin. [Source and benchmark](https://github.com/pgg898200-ship-it/streamsieve/tree/rankfence-v0.1.0) · [GitHub Copilot plugin reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)
