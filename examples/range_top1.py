"""Try an exact top-one shortcut before starting a disjoint retrieval shard.

All ranked sources must be stable, duplicate-free and use the same RRF rank
constant. IDs use ascending Python string order for ties. Metadata must
describe every active ranker and the same immutable snapshot as the query.
"""
from contextlib import AsyncExitStack
from .sqlite_interrupt import sqlite_shard_pages
from rankfence import fuse_partitioned


def certified_first(heads, other_shards):
    """heads contains (first ID or None, positive integer weight) on one shard.

    other_shards contains (sum of positive weights, inclusive ID lower bound
    or None) for EACH remaining shard. The caller guarantees disjoint IDs.
    None means that the shortcut cannot certify a winner. Empty first results
    must mean genuine EOF. A timeout must propagate as an error.
    """
    heads = tuple(heads)
    others = tuple(other_shards)
    for doc, weight in heads:
        if doc is not None and not isinstance(doc, str):
            raise TypeError("expected string ID or EOF")
        if type(weight) is not int or weight < 1:
            raise ValueError("weights must be positive integers")
    for mass, floor in others:
        if type(mass) is not int or mass < 1:
            raise ValueError("each other shard needs its positive total weight")
        if floor is not None and not isinstance(floor, str):
            raise TypeError("expected string ID floor or None")
    present = [(doc, weight) for doc, weight in heads if doc is not None]
    if not present:
        return None
    first = present[0][0]
    if any(doc != first for doc, _ in present):
        return None
    mass = sum(weight for _, weight in present)
    for other_mass, floor in others:
        if other_mass > mass or (other_mass == mass and (floor is None or first >= floor)):
            return None
    return first


async def probe_catalog(path, queries):
    """Run each ranked SELECT with LIMIT 1; supply SELECTs without LIMIT or ';'."""
    limited = [(sql + " LIMIT 1", params) for sql, params in queries]
    async with sqlite_shard_pages(path, limited, width=1, progress_ops=4096) as readers:
        heads = []
        for reader in readers:
            page = await anext(reader)
            heads.append(page.ids[0] if page.ids else None)
    return heads


def probe_possible(catalog_floor, archive_floor):
    """For equal total weight, could a catalog ID be strictly below the archive floor?"""
    for bound in (catalog_floor, archive_floor):
        if bound is not None and not isinstance(bound, str):
            raise TypeError("expected string ID floor or None")
    return archive_floor is not None and (catalog_floor is None or catalog_floor < archive_floor)


async def sqlite_first(paths, queries, *, catalog_floor=None, archive_floor=None, probe=True):
    """Run the same unit-weight rankers on two disjoint, read-only SQLite files.

    Returns (ordered IDs, archive skipped). Cache a conservative archive ID
    floor with the index generation. If the probe is inconclusive, this
    example repeats its catalog SQL using the regular paginated coordinator.
    """
    queries = tuple(queries)
    if len(paths) != 2 or not queries:
        raise ValueError("expected two shard paths and at least one ranker")
    if probe and probe_possible(catalog_floor, archive_floor):
        heads = await probe_catalog(paths[0], queries)
        first = certified_first([(doc, 1) for doc in heads], [(len(queries), archive_floor)])
        if first is not None:
            return [first], True
    async with AsyncExitStack() as stack:
        sources = []
        for path in paths:
            readers = await stack.enter_async_context(sqlite_shard_pages(path, queries, progress_ops=4096))
            sources.extend(readers)
        groups = ["catalog"] * len(queries) + ["archive"] * len(queries)
        result = await fuse_partitioned(sources, groups, 1)
    return list(result.ids), False
