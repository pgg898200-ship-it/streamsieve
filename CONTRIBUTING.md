# Contributing a retrieval integration

A backend adapter only needs to supply an asynchronous stream of `Page` objects per ranking. Start with a single fixed query, a stable snapshot, deterministic ties, and one known finite result window. Record whether stopping a cursor cancels producer work, skips the next remote fetch, or just avoids consuming rows already materialized by the backend.

For a sharded adapter, describe how the service enforces disjoint document ownership, including replicas and migrations. A changing snapshot or two shards returning the same global ID can invalidate the proof. Use the flat adapter if ownership is uncertain.

A reproducible integration can be small. Supply a public fixture or a self-contained local generator, an exhaustive reference using the same scoring semantics, a positive case, and a case where the adapter reads the whole window. Include the counts from the underlying provider when available. Label modeled costs and report the median and tail if the claim concerns latency.

A benchmark submission may use anonymized, consistently remapped IDs. Include the same mapping across sources, page sizes, local score tie order and true EOF markers. The browser workbench performs its analysis locally. Upload rankings to a public repository only if you have the rights to publish them.

Run the package tests with `python -m unittest discover -v`. Reports in `results/` contain machine and configuration details for comparison.
