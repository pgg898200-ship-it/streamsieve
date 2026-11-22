# When could any exact coordinator have stopped?

On a simulated eight-shard replay of four TREC Robust03 retrieval submissions, RankFence with losing-shard retirement finished at the earliest certifiable synchronous round on **100 of 100 topics**. Seventy-seven topics finished after the first page from each participating source. The other 23 required two to nine rounds. On all 23, an already observed document could still change the ordered top five after the previous round.

| Stop round | Topics |
|---:|---:|
| 1 | 77 |
| 2 | 11 |
| 3 | 4 |
| 4 | 1 |
| 5 | 1 |
| 6 | 3 |
| 9 | 3 |

## What is measured

Each topic has 32 stable, duplicate-free ranked lists. Each of the four genuine submissions is partitioned into eight lists by CRC32 of the original document ID, modulo eight. Positions in these **shard-local lists** determine reciprocal rank fusion scores, using constant 60, unit weights and ties resolved by ascending qualified ID. This changes ranking semantics from the original unsharded track.

In one synchronous round, a coordinator can pull at most one consecutive page of up to 16 IDs from each source. The final page carries an EOF flag. Namespace routing is known. A coordinator has no random access, advance list lengths, complete candidate vocabulary, raw scores, cross-topic prior about hidden inputs, or independent source of suffix information. The correctness target is the ordered five across every completion consistent with the returned data.

## Why the stopping round is minimal on these inputs

For each topic, take the round in which RankFence with retirement certified the result. The audit independently rescored the entire actual input using exact rational arithmetic and checked the coordinator's stopping receipt against its accepted prefixes. This gives an achievable round count.

The lower bound gives a hypothetical competitor more data than a selective scheduler could have collected by the previous round. The audit reveals every source's maximum prefix, including the prefixes of sources RankFence stopped polling. It then builds two complete worlds with identical revealed pages and EOF flags. In the first, every open source appends one unique, namespace-valid neutral ID and closes. In the second, selected sources in one shard append the same challenger ID instead. The validator checks uniqueness within each source, recomputes the CRC32 of every document, preserves EOF status, and scores both complete worlds with exact fractions. Different ordered top fives force every universally correct coordinator to query another round. For round-one stops the shared history is empty.

The output includes each topic number, terminating round, and SHA-256 digests of both distinct answers, suffixes, and shared history. The original rankings stay at their source. Across topics, 77 witnesses use a fresh ID and 23 promote an existing candidate. The observed 78,956 consumed IDs are an independent workload count. The lower bound addresses round count only. It assigns no optimality to per-source reads, individual pages, computation, wall time, ranking quality, or another access model.

## Reproduce from a release checkout

Use Python and the standard library. Substitute the checkout and audit directory paths.

```sh
python /path/to/rankfence/benchmarks/fetch_replay.py robust03 /tmp/robust03.json
python /path/to/round_audit.py --repo /path/to/rankfence --trace /tmp/robust03.json --json /tmp/round_audit.json
```

The audit rejects a trace whose SHA-256 differs from the pinned one. The report records SHA-256 hashes of the verifier and scorer. The original runs are available from [trectools at the pinned revision](https://github.com/joaopalotti/trectools/tree/8a896def007e3d657eb29f820ee3de98e2f32691/examples/robust03). The [official TREC overview](https://trec.nist.gov/pubs/trec12/papers/ROBUST.OVERVIEW.pdf) describes the retrieval task.
