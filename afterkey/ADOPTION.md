# Adoption evidence

## November 22, 2026 · 0.1.0 launch

| Signal | Verified at launch |
| --- | ---: |
| Independent repositories integrating Afterkey | 0 found |
| Independent benchmark reproductions | 0 found |
| Likes on the Afterkey Hugging Face Space | 0 |

The GitHub search checked `"afterkey.hpp"`, `"afterkey_journal.hpp"` and `"afterkey::Index"` outside the publishing account. Additional searches checked issues and commit messages for the header. These searches can miss private repositories and newly indexed code. StreamSieve stars and forks apply to the parent repository. Neither PGG integrations nor our anonymous release smoke test count as third-party use.

The ZeroMQ example implements the multi-topic slow-reader case described in [libzmq issue 1688](https://github.com/zeromq/libzmq/issues/1688). The issue predates this release. The native StreamSieve integration and ZeroMQ example both run from the published source. The [public replay](bench/replay.md) uses recorded data and simulated consumers.

A downstream entry should identify its owner, public URL, pinned release or vendored code, what the consumer uses and the date checked. Keep reproductions, citations, requests, likes and downloads distinct. Useful independent measurements include hot-set size, reader-lag distribution, poll-time distribution, callback work, memory and the uniform-churn crossover.

[GitHub source](https://github.com/pgg898200-ship-it/streamsieve/tree/main/afterkey) · [Public demo and source mirror](https://huggingface.co/spaces/pgg-engineering/afterkey) · [Verified release ZIP](https://raw.githubusercontent.com/pgg898200-ship-it/streamsieve/6e0530a1afe27efda145fdcbbd94198b79c99b81/afterkey/releases/afterkey-0.1.0.zip)
