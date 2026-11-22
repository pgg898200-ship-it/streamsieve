"""Certify standard reciprocal-rank fusion over append-only, unique-ID prefixes."""

from __future__ import annotations

import heapq
from fractions import Fraction
from functools import cmp_to_key
from typing import Iterable


# Each term's floor has absolute error less than one unit. Python integers
# retain this interval guarantee for arbitrary depths and numbers of sources.
_UNIT = 1 << 96


class _Group:
    """Objects seen at exactly the same sources share the same unknown tail."""

    __slots__ = ("count", "heap")

    def __init__(self):
        self.count = 0
        self.heap = []


class Fence:
    """Fuse ranked sources with score sum(1 / (c + one_based_rank)).

    Each source emits stable, duplicate-free string IDs, in rank order, and
    contributes zero for absent IDs. A closed source cannot receive more IDs.
    Break exact score ties by increasing ID. Results describe IDs only.
    Serialize calls to a Fence within one coordinator.
    """

    def __init__(self, sources: int, k: int, c: int = 60):
        for name, value, minimum in (("sources", sources, 1), ("k", k, 1), ("c", c, 0)):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        self._k = k
        self._c = c
        self._ranks = [{} for _ in range(sources)]
        self._ended = [False] * sources
        self._items = {}  # ID -> (source mask, sum of individually floored terms)
        self._groups = {}
        self._discounts = [0]  # indexed by one-based rank
        self._cached = None

    @property
    def depths(self) -> tuple[int, ...]:
        """Number of IDs accepted from each source."""
        return tuple(map(len, self._ranks))

    @property
    def done(self) -> tuple[bool, ...]:
        """Whether each source has explicitly closed."""
        return tuple(self._ended)

    def push(self, source: int, ids: Iterable[str], *, done: bool = False) -> None:
        """Atomically append a page and optionally close its source."""
        if type(source) is not int or not 0 <= source < len(self._ranks):
            raise ValueError("source index out of range")
        if type(done) is not bool:
            raise TypeError("done must be a bool")
        if isinstance(ids, (str, bytes)):
            raise TypeError("ids must be an iterable of string IDs")
        page = tuple(ids)
        if self._ended[source]:
            raise ValueError("source has already closed")
        if any(type(doc) is not str for doc in page):
            raise TypeError("each ID must be a string")
        ranks = self._ranks[source]
        if len(set(page)) != len(page) or any(doc in ranks for doc in page):
            raise ValueError("duplicate ID in source")

        end = len(ranks) + len(page)
        self._discounts.extend(
            _UNIT // (self._c + rank) for rank in range(len(self._discounts), end + 1)
        )
        bit = 1 << source
        for rank, doc in enumerate(page, len(ranks) + 1):
            ranks[doc] = rank
            previous, score = self._items.get(doc, (0, 0))
            if previous:
                group = self._groups[previous]
                group.count -= 1
                if not group.count:
                    del self._groups[previous]
            mask = previous | bit
            score += self._discounts[rank]
            self._items[doc] = mask, score
            if mask not in self._groups:
                self._groups[mask] = _Group()
            group = self._groups[mask]
            group.count += 1
            heapq.heappush(group.heap, (-score, doc))
        self._ended[source] = done
        self._cached = None

    def _lower_order(self):
        # A comparison needs rational arithmetic only when the fixed-point
        # intervals overlap. Cache those fractions for this snapshot alone.
        exact = {}

        def score(doc):
            if doc not in exact:
                mask = self._items[doc][0]
                total = Fraction(0)
                while mask:
                    bit = mask & -mask
                    rank = self._ranks[bit.bit_length() - 1][doc]
                    total += Fraction(1, self._c + rank)
                    mask ^= bit
                exact[doc] = total
            return exact[doc]

        def compare(a, b):
            mask_a, lo_a = self._items[a]
            mask_b, lo_b = self._items[b]
            # Each seen term has STRICTLY less than one unit of error.
            if lo_a >= lo_b + mask_b.bit_count():
                return -1
            if lo_b >= lo_a + mask_a.bit_count():
                return 1
            x, y = score(a), score(b)
            if x != y:
                return -1 if x > y else 1
            return (a > b) - (a < b)

        return cmp_to_key(compare), score

    def _head(self, group, mask):
        heap = group.heap
        while heap and self._items[heap[0][1]][0] != mask:
            heapq.heappop(heap)
        return heap[0] if heap else None

    def _snapshot(self):
        if self._cached is not None:
            return self._cached
        if all(self._ended):
            lower_key, _ = self._lower_order()
            ordered = tuple(heapq.nsmallest(self._k, self._items, key=lower_key))
            self._cached = frozenset(ordered), ordered
        elif len(self._items) < self._k:
            self._cached = None, None
        else:
            self._cached = self._partial()
        return self._cached

    def _partial(self):
        tails = [
            0 if ended else (_UNIT - 1) // (self._c + len(ranks) + 1) + 1
            for ranks, ended in zip(self._ranks, self._ended)
        ]
        total_tail = sum(tails)
        tail_by_mask = {}

        def remaining(mask):
            if mask not in tail_by_mask:
                result = total_tail
                seen = mask
                while seen:
                    bit = seen & -seen
                    result -= tails[bit.bit_length() - 1]
                    seen ^= bit
                tail_by_mask[mask] = result
            return tail_by_mask[mask]

        # Merge group heaps until k leaders have been removed. The first
        # remaining member in each group bounds all its other challengers.
        heads = [(*self._head(g, m), m) for m, g in self._groups.items()]
        heapq.heapify(heads)
        leaders = []
        removed = []
        try:
            for _ in range(self._k):
                neg, doc, mask = heapq.heappop(heads)
                group = self._groups[mask]
                removed.append((group, heapq.heappop(group.heap)))
                leaders.append(doc)
                head = self._head(group, mask)
                if head is not None:
                    heapq.heappush(heads, (*head, mask))

            cutoff = self._items[leaders[-1]][1]
            # An unseen ID may win a tie via its ID, so require a strict
            # separation from its (closed) upper bound.
            if cutoff <= total_tail:
                return None, None
            # Most failures have a wide score gap. Only candidates that can
            # differ by a few rounding units need an exact-rational comparison.
            rounding_window = 3 * len(tails)
            ambiguous = []
            for neg, _, mask in heads:
                allowance = mask.bit_count() + remaining(mask)
                gap = -neg + allowance - cutoff
                if gap > rounding_window:
                    return None, None
                if gap > 0:
                    ambiguous.append((mask, allowance))

            lower_key, exact_lower = self._lower_order()
            leaders.sort(key=lower_key)
            exact_tails = {}

            def outranks(a, b, mask_b):
                if mask_b not in exact_tails:
                    exact_tails[mask_b] = sum(
                        Fraction(1, self._c + len(ranks) + 1)
                        for i, ranks in enumerate(self._ranks)
                        if tails[i] and not mask_b & (1 << i)
                    )
                return (-exact_lower(a), a) < (-(exact_lower(b) + exact_tails[mask_b]), b)

            for mask, allowance in ambiguous:
                # Heap children have no higher floor than their parent. Walk
                # just the tied boundary; stale entries can still have children.
                heap = self._groups[mask].heap
                pending = [0]
                while pending:
                    pos = pending.pop()
                    if pos >= len(heap):
                        continue
                    neg, rival = heap[pos]
                    if -neg + allowance <= cutoff:
                        continue
                    if self._items[rival][0] == mask and not outranks(leaders[-1], rival, mask):
                        return None, None
                    pending.extend((2 * pos + 1, 2 * pos + 2))

            members = frozenset(leaders)
            for a, b in zip(leaders, leaders[1:]):
                mask_b, lower_b = self._items[b]
                extra = remaining(mask_b)
                if not extra:
                    continue
                gap = lower_b + mask_b.bit_count() + extra - self._items[a][1]
                if gap > rounding_window or (gap > 0 and not outranks(a, b, mask_b)):
                    return members, None
            return members, tuple(leaders)
        finally:
            for group, entry in removed:
                heapq.heappush(group.heap, entry)

    def members(self) -> frozenset[str] | None:
        """Return a certified winner set, or None while membership is uncertain."""
        return self._snapshot()[0]

    def ordered(self) -> tuple[str, ...] | None:
        """Return a certified ordered prefix of up to k IDs, or None."""
        return self._snapshot()[1]
