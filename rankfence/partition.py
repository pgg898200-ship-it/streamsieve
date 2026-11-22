"""Exact reciprocal-rank fusion when groups of sources have disjoint ID spaces."""
from fractions import Fraction
import heapq

from .core import Fence, _UNIT


class PartitionFence(Fence):
    """Certify global winners over immutable, pairwise-disjoint source groups.

    Every occurrence of the same ID, including future occurrences, must belong
    to a single group. A common example is a globally qualified document ID in
    a partitioned index, with lexical and dense rankings per shard. A replica
    returning the same ID must share the original's group. Observed violations
    raise ValueError and the entire incoming page is rejected.
    """

    def __init__(self, source_groups: list[str], k: int, c: int = 60):
        if isinstance(source_groups, (str, bytes)):
            raise TypeError("source_groups must be a sequence of group names")
        names = tuple(source_groups)
        if any(type(name) is not str for name in names):
            raise TypeError("each group name must be a string")
        super().__init__(len(names), k, c)
        masks = {}
        for source, name in enumerate(names):
            masks[name] = masks.get(name, 0) | (1 << source)
        self._scopes = tuple(masks[name] for name in names)

    def push(self, source, ids, *, done=False):
        if type(source) is not int or not 0 <= source < len(self._scopes):
            raise ValueError("source index out of range")
        if isinstance(ids, (str, bytes)):
            raise TypeError("ids must be an iterable of string IDs")
        page = tuple(ids)
        scope = self._scopes[source]
        for doc in page:
            if type(doc) is not str:
                raise TypeError("each ID must be a string")
            if doc in self._items and not (self._items[doc][0] & scope):
                raise ValueError(f"ID appears in different source groups: {doc!r}")
        super().push(source, page, done=done)

    def _partial(self):
        # A seen candidate's unknown terms come only from its source group.
        # A never-seen candidate could be in any one group, so take the maximum.
        ceilings = []
        budgets = {}
        for ranks, ended, scope in zip(self._ranks, self._ended, self._scopes):
            cap = 0 if ended else 1 + (_UNIT - 1) // (self._c + len(ranks) + 1)
            ceilings.append(cap)
            budgets[scope] = budgets.get(scope, 0) + cap

        def scope_for(mask):
            return self._scopes[(mask & -mask).bit_length() - 1]

        extra_by_mask = {}

        def extra(mask):
            if mask not in extra_by_mask:
                allowance = budgets[scope_for(mask)]
                seen = mask
                while seen:
                    bit = seen & -seen
                    allowance -= ceilings[bit.bit_length() - 1]
                    seen ^= bit
                extra_by_mask[mask] = allowance
            return extra_by_mask[mask]

        # Lower-bound leaders can be found by merging just the live heap heads.
        heads = [(*self._head(group, mask), mask) for mask, group in self._groups.items()]
        heapq.heapify(heads)
        popped = []
        leaders = []
        try:
            for _ in range(self._k):
                _, doc, mask = heapq.heappop(heads)
                leaders.append(doc)
                group = self._groups[mask]
                popped.append((group.heap, heapq.heappop(group.heap)))
                head = self._head(group, mask)
                if head is not None:
                    heapq.heappush(heads, (*head, mask))
            cutoff = self._items[leaders[-1]][1]
            if cutoff <= max(budgets.values()):
                return None, None
            near = []
            for neg, _, mask in heads:
                allowance = mask.bit_count() + extra(mask)
                gap = -neg + allowance - cutoff
                if gap > 3 * len(ceilings):
                    return None, None
                if gap > 0:
                    near.append((mask, allowance))

            lower_order, exact_lower = self._lower_order()
            leaders.sort(key=lower_order)
            rational_tails = {}

            def ahead_of(a, b, mask):
                if mask not in rational_tails:
                    missing = scope_for(mask) & ~mask
                    total = Fraction()
                    while missing:
                        bit = missing & -missing
                        i = bit.bit_length() - 1
                        if not self._ended[i]:
                            total += Fraction(1, self._c + len(self._ranks[i]) + 1)
                        missing ^= bit
                    rational_tails[mask] = total
                return (-exact_lower(a), a) < (-(exact_lower(b) + rational_tails[mask]), b)

            # Check only heap branches within rounding distance of the boundary.
            for mask, allowance in near:
                heap = self._groups[mask].heap
                stack = [0]
                while stack:
                    index = stack.pop()
                    if index >= len(heap):
                        continue
                    neg, doc = heap[index]
                    if -neg + allowance <= cutoff:
                        continue
                    if self._items[doc][0] == mask and not ahead_of(leaders[-1], doc, mask):
                        return None, None
                    stack.extend((2 * index + 1, 2 * index + 2))
            members = frozenset(leaders)
            for a, b in zip(leaders, leaders[1:]):
                mask, score = self._items[b]
                tail = extra(mask)
                if tail and score + mask.bit_count() + tail > self._items[a][1]:
                    if not ahead_of(a, b, mask):
                        return members, None
            return members, tuple(leaders)
        finally:
            for heap, entry in popped:
                heapq.heappush(heap, entry)
