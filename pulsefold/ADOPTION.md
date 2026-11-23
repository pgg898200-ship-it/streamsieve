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

## November 23, 2026 • Pulsefold 0.1.0

| Public evidence checked | Verified in this audit |
|---|---|
| Independent consuming repositories / distinct owners | 0 / 0 verified. Exact default-branch code searches for [the header](https://github.com/search?q=%22pulsefold.hpp%22&type=code), [the monitor type](https://github.com/search?q=%22pulsefold%3A%3AMonitor%22&type=code), the live executable and the Space identifier returned no matches. Unindexed code, private use and other branches remain outside this count. |
| Independent reproductions / external issues | 0 / 0 verified. The [Space discussions](https://huggingface.co/spaces/pgg-engineering/pulsefold/discussions) were empty. The [first-party adapter PR](https://github.com/pgg898200-ship-it/streamsieve/pull/1) was open with no public review or comments. |
| Forks and namesakes | The [source repository fork list](https://api.github.com/repos/pgg898200-ship-it/streamsieve/forks) was empty. Two separately owned GitHub namesakes predate the November 22 release. [amahon78/pulsefold](https://github.com/amahon78/pulsefold) last pushed October 14 and analyzes OpenTelemetry logs. [MaherRayes/PulseFold](https://github.com/MaherRayes/PulseFold) last pushed September 18 and compresses TIFF sequences. Neither qualifies. |
| First-party evidence | One StreamSieve adapter carried forward. The [Space](https://huggingface.co/spaces/pgg-engineering/pulsefold) showed zero likes. Counts exclude publisher tests, replays, downloads, stars, likes and search citations. |

The [publisher's ten public GitHub repositories](https://api.github.com/users/pgg898200-ship-it/repos?per_page=100) each showed zero stars and forks. A [portfolio issue and PR search excluding the publisher](https://github.com/search?q=user%3Apgg898200-ship-it+-author%3Apgg898200-ship-it+is%3Apublic&type=issues) returned no records. Prior portfolio audits also had no verified consuming repository for EdgeChime, EventFold, Afterkey, CachePin or RankFence. No stronger independently verified demand surfaced to justify moving this work into another active product program.

**Installation.** The [0.1.0 manifest](https://pulsefold.tancurl3.chatgpt.site/release.json) matched the downloaded ZIP (SHA-256 `c39cf9b88c47e39bc879bee13bbe92e321ac36eab73cb506c556908bdcd3cf11`). The archive lacked a Make install target. The release branch now supports prefix and staged installation of the header, replay CLI and license, plus an optional POSIX live CLI. A first-party Linux check compiled a separate C++17 consumer against the installed header, matched the installed replay output to the example, and passed the installed live CLI smoke test. The existing monitor and baseline suites passed. See the [install commands](README.md#install-from-the-release-branch). The [published measurements](results/REPORT.md) remain the November 22 first-party results.
