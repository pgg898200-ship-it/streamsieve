# Pulsefold adoption record

## November 22, 2026 • release baseline

| Signal | Verified at launch | Evidence |
|---|---:|---|
| Independently owned consuming repositories found | 0 | Public GitHub code searches for `"pulsefold.hpp"` and `"pgg-engineering/pulsefold"`, excluding the publisher account, returned no matches. Search coverage and indexing lag limit what this establishes. |
| External benchmark reproductions verified | 0 | The published numbers are first-party measurements on a shared host. |
| Hugging Face Space likes | 0 | The public Space showed Running and zero likes when the replay was verified. |
| First-party integrations tested | 1 | The StreamSieve adapter consumed the real sensor replay through its evidence-lattice API. Three Linux/macOS CI jobs passed. Its pull request was open at this check. |

The standalone header, two CLIs, MIT license, real sensor fixture, challenger implementations, raw measurements and a downloadable source archive are public. [Release source](https://github.com/pgg898200-ship-it/streamsieve/tree/pulsefold-v0.1.0/pulsefold) · [Live Hugging Face demo](https://huggingface.co/spaces/pgg-engineering/pulsefold) · [Benchmark report](https://pulsefold.tancurl3.chatgpt.site/results.html) · [First-party integration](https://github.com/pgg898200-ship-it/streamsieve/pull/1)

A downstream repository counts when an independently maintained project actually uses the implementation. Record owner, public artifact, version and date. Deduplicate forks, mirrors and repeated references. Keep independent performance reproductions, user questions, likes, downloads, and search citations in separate columns. Self-tests and PGG projects stay in the first-party column.

The current product bet is grouped handling of heartbeat expirations in large live fleets. Stronger evidence would be someone adopting the API on their own workload, or a reproducible result from another machine and trace. Compare that evidence with other PGG projects before investing in additional surfaces.
