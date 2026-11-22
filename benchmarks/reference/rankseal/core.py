"""Certified, demand-driven fusion of append-only ranked lists."""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Sequence
from fractions import Fraction


_SCALE = 1 << 64


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class _Document:
    __slots__ = ("mask", "lower", "upper", "ranks")

    def __init__(self) -> None:
        self.mask = 0
        self.lower = 0
        self.upper = 0
        self.ranks: list[tuple[int, int]] = []


class _Bucket:
    __slots__ = ("heap", "live")

    def __init__(self) -> None:
        self.heap: list[tuple[int, str]] = []
        self.live = 0


class Fusion:
    """Fuse stable ranked prefixes, with rank 1 first and lexicographic ID ties.

    Push one page per call. Close a source only when its ranked window ends.
    A non-None certificate gives the final ordered IDs for every valid suffix.
    Instances support serialized calls, including from a single event loop.
    """

    def __init__(
        self,
        sources: int,
        k: int = 10,
        constant: int = 60,
        weights: Sequence[int] | None = None,
    ) -> None:
        if not all(_positive_int(value) for value in (sources, k, constant)):
            raise ValueError("sources, k and constant must be positive integers")
        try:
            values = (1,) * sources if weights is None else tuple(weights)
        except TypeError as exc:
            raise ValueError("weights must be a sequence of positive integers") from exc
        if len(values) != sources or not all(_positive_int(w) for w in values):
            raise ValueError("give one positive integer weight per source")
        self._k = k
        self._constant = constant
        self._weights = values
        self._numerators = tuple(_SCALE * weight for weight in values)
        self._depth = [0] * sources
        self._closed = [False] * sources
        self._open = sources
        self._docs: dict[str, _Document] = {}
        self._leaders: list[tuple[int, str, int]] = []
        self._buckets: dict[int, _Bucket] = {}
        self._last: tuple[str, ...] | None = None
        self._checked = False

    def _check_source(self, source: int) -> None:
        if (isinstance(source, bool) or not isinstance(source, int)
                or not 0 <= source < len(self._weights)):
            raise ValueError("source must be a valid zero-based integer index")

    def push(self, source: int, ids: Iterable[str]) -> None:
        """Append one ranked page atomically. Reject repeated IDs per source."""
        self._check_source(source)
        if self._closed[source]:
            raise ValueError("source is closed")
        if isinstance(ids, (str, bytes)):
            raise ValueError("pass an iterable of IDs, not a single string")
        try:
            page = list(ids)
        except TypeError as exc:
            raise ValueError("page must be iterable") from exc
        bit = 1 << source
        seen: set[str] = set()
        for doc_id in page:
            if not isinstance(doc_id, str) or not doc_id:
                raise ValueError("each ID must be a nonempty string")
            doc = self._docs.get(doc_id)
            if doc_id in seen or (doc is not None and doc.mask & bit):
                raise ValueError("duplicate ID in this source")
            seen.add(doc_id)
        if not page:
            return

        touched = set()
        depth = self._depth[source]
        numerator = self._numerators[source]
        for rank, doc_id in enumerate(page, depth + 1):
            doc = self._docs.get(doc_id)
            if doc is None:
                doc = self._docs[doc_id] = _Document()
            else:
                self._buckets[doc.mask].live -= 1
                touched.add(doc.mask)
            term, remainder = divmod(numerator, self._constant + rank)
            doc.lower += term
            doc.upper += term + bool(remainder)
            doc.ranks.append((source, rank))
            doc.mask |= bit
            bucket = self._buckets.get(doc.mask)
            if bucket is None:
                bucket = self._buckets[doc.mask] = _Bucket()
            bucket.live += 1
            heapq.heappush(bucket.heap, (-doc.upper, doc_id))
            heapq.heappush(self._leaders, (-doc.lower, doc_id, doc.mask))
        self._depth[source] += len(page)
        for mask in touched:
            bucket = self._buckets[mask]
            if bucket.live == 0:
                del self._buckets[mask]
            elif len(bucket.heap) > 2 * bucket.live + 16:
                bucket.heap = [row for row in bucket.heap if self._docs[row[1]].mask == mask]
                heapq.heapify(bucket.heap)
        if len(self._leaders) > 2 * len(self._docs) + 16:
            self._leaders = [(-doc.lower, key, doc.mask) for key, doc in self._docs.items()]
            heapq.heapify(self._leaders)
        self._checked = self._last is not None

    def close(self, source: int) -> None:
        """Mark a ranked window exhausted. Repeated close calls are harmless."""
        self._check_source(source)
        if not self._closed[source]:
            self._closed[source] = True
            self._open -= 1
            self._checked = self._last is not None

    def _current_leaders(self, count: int) -> list[str]:
        leaders = []
        while len(leaders) < count:
            item = heapq.heappop(self._leaders)
            if self._docs[item[1]].mask == item[2]:
                leaders.append(item)
        for item in leaders:
            heapq.heappush(self._leaders, item)
        return [item[1] for item in leaders]

    def _outside(self, mask: int, bucket: _Bucket, chosen: set[str]) -> tuple[int, str] | None:
        # Drop obsolete memberships. Temporarily lift leaders to see the best rival.
        lifted = []
        best = None
        while bucket.heap:
            entry = bucket.heap[0]
            doc_id = entry[1]
            if self._docs[doc_id].mask != mask:
                heapq.heappop(bucket.heap)
            elif doc_id in chosen:
                lifted.append(heapq.heappop(bucket.heap))
            else:
                best = entry
                break
        for entry in lifted:
            heapq.heappush(bucket.heap, entry)
        return best

    def _exact_score(self, doc: _Document) -> Fraction:
        return sum((Fraction(self._weights[s], self._constant + r)
                    for s, r in doc.ranks), Fraction())

    def _smallest_unseen(self) -> str:
        key = "\x00"
        while key in self._docs:
            key += "\x00"
        return key

    def _exact_certificate(
        self, floor: int, caps: dict[int, int], count: int
    ) -> tuple[str, ...] | None:
        # A directed bound reaching the kth known lower bound is necessary to compete.
        candidates = []
        for doc_id, doc in self._docs.items():
            if doc.upper + caps[doc.mask] >= floor:
                score = self._exact_score(doc)
                candidates.append((-score, doc_id, doc.mask))
        candidates.sort()
        winners = candidates[:count]
        if not self._open:
            return tuple(key for _, key, _ in winners)
        future = [Fraction() if closed else Fraction(weight, self._constant + depth + 1)
                  for closed, weight, depth in zip(self._closed, self._weights, self._depth)]
        unseen = sum(future)
        weakest_score, weakest_key, _ = winners[-1]
        if -weakest_score < unseen or (-weakest_score == unseen
                                       and weakest_key > self._smallest_unseen()):
            return None
        exact_caps: dict[int, Fraction] = {}
        rival = None
        # Other outside documents omitted above have best score strictly below floor.
        for index in range(len(candidates) - 1, -1, -1):
            negative_score, key, mask = candidates[index]
            if index < count and rival is not None and (negative_score, key) >= rival:
                return None
            if mask not in exact_caps:
                exact_caps[mask] = sum((value for i, value in enumerate(future)
                                        if not mask & (1 << i)), Fraction())
            bound = (negative_score - exact_caps[mask], key)
            if rival is None or bound < rival:
                rival = bound
        return tuple(key for _, key, _ in winners)

    def certified(self) -> tuple[str, ...] | None:
        """Return the certified ordered result, or None while the answer is open."""
        if self._checked:
            return self._last
        self._checked = True
        count = min(self._k, len(self._docs))
        if self._open and count < self._k:
            return None
        if count == 0:
            self._last = ()
            return self._last
        ordered = self._current_leaders(count)
        floor = self._docs[ordered[-1]].lower
        future = [0 if closed else -(-num // (self._constant + depth + 1))
                  for closed, num, depth in zip(self._closed, self._numerators, self._depth)]
        unseen = sum(future)
        # Each directed score differs from the exact scaled score by less than m.
        slack = 2 * len(future)
        if self._open and unseen - floor > slack:
            return None
        ambiguous = self._open and unseen >= floor
        chosen = set(ordered)
        caps = {}
        rival = None
        outside_observed = None
        for mask, bucket in self._buckets.items():
            cap = unseen
            bits = mask
            while bits:
                bit = bits & -bits
                cap -= future[bit.bit_length() - 1]
                bits ^= bit
            caps[mask] = cap
            entry = self._outside(mask, bucket, chosen)
            if entry is not None:
                if outside_observed is None or entry < outside_observed:
                    outside_observed = entry
                upper = (entry[0] - cap, entry[1])
                if rival is None or upper < rival:
                    rival = upper
        for key in reversed(ordered):
            doc = self._docs[key]
            lower = (-doc.lower, key)
            if rival is not None and lower >= rival:
                if self._open and lower[0] - rival[0] > slack:
                    # A large gap proves instability if this pair also has the
                    # proposed order when all sources stop now. Near-tied lower
                    # scores can otherwise put a genuine leader in the wrong slot.
                    other_key = rival[1]
                    other = self._docs[other_key]
                    other_known = (-other.upper, other_key)
                    if other_key not in chosen and (-floor, ordered[-1]) < other_known:
                        return None  # This outsider can enter the exact top k.
                    if outside_observed is None or lower < outside_observed:
                        if lower < other_known:
                            return None
                        if (-self._exact_score(doc), key) < (-self._exact_score(other), other_key):
                            return None
                ambiguous = True
            upper = (-(doc.upper + caps[doc.mask]), key)
            if rival is None or upper < rival:
                rival = upper
        self._last = self._exact_certificate(floor, caps, count) if ambiguous else tuple(ordered)
        return self._last
