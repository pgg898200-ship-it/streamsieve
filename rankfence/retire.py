"""Identify open cursors on disjoint shards that cannot return any global winner."""
import heapq

from .core import _UNIT
from .partition import PartitionFence


def _cutoff(fence):
    # Read the heaps without popping from the live index. A document that
    # moved to a larger source mask leaves stale entries in its former heap.
    frontier = [(group.heap[0][0], mask, 0) for mask, group in fence._groups.items()]
    heapq.heapify(frontier)
    found = 0
    while frontier:
        negative, mask, index = heapq.heappop(frontier)
        heap = fence._groups[mask].heap
        if fence._items[heap[index][1]][0] == mask:
            found += 1
            if found == fence._k:
                return -negative
        for child in (2 * index + 1, 2 * index + 2):
            if child < len(heap):
                heapq.heappush(frontier, (heap[child][0], mask, child))
    raise RuntimeError("candidate index inconsistent")


def retirable_sources(fence: PartitionFence) -> frozenset[int]:
    """Return open sources whose whole shard is strictly below the global cutoff.

    A caller can cancel pulls and close these readers without marking them
    done on the fence. The decision remains valid as ranked prefixes grow.
    As with PartitionFence.push, serialize access to the fence. This check
    does not mutate it. Ties and rounding uncertainty keep a source active.
    """
    if not isinstance(fence, PartitionFence):
        raise TypeError("expected a PartitionFence")
    if all(fence._ended) or len(fence._items) < fence._k:
        return frozenset()
    cutoff = _cutoff(fence)
    tails = []
    shard_budget = {}
    for i, shard in enumerate(fence._scopes):
        # Round each rational upper bound upward; observed score terms use
        # the index's downward rounding. Never discard a group on equality.
        denominator = fence._c + len(fence._ranks[i]) + 1
        tail = 0 if fence._ended[i] else (_UNIT + denominator - 1) // denominator
        tails.append(tail)
        shard_budget[shard] = shard_budget.get(shard, 0) + tail

    # An entirely new ID can collect at most its shard's full tail budget.
    losing = {shard for shard, budget in shard_budget.items() if budget < cutoff}
    for mask, group in fence._groups.items():
        shard = fence._scopes[(mask & -mask).bit_length() - 1]
        if shard not in losing:
            continue
        missing = shard_budget[shard]
        seen = mask
        while seen:
            bit = seen & -seen
            missing -= tails[bit.bit_length() - 1]
            seen ^= bit
        # All members of this mask have the same unseen terms. Even a
        # stale heap head bounds every current member's floored score.
        upper = -group.heap[0][0] + mask.bit_count() + missing
        if upper >= cutoff:
            losing.remove(shard)

    return frozenset(i for i, shard in enumerate(fence._scopes)
                     if not fence._ended[i] and shard in losing)
