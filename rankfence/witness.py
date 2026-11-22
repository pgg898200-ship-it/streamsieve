"""Construct concrete reasons that an unfinished RRF result can still change."""
from dataclasses import dataclass
from fractions import Fraction
from heapq import nsmallest
from itertools import chain
from typing import Sequence


@dataclass(frozen=True)
class Completion:
    """Append suffixes[i] to source i, then close every open source.

    ids is the resulting ordered top-k, or the winning IDs sorted by string
    when the caller requested membership.
    """

    suffixes: tuple[tuple[str, ...], ...]
    ids: tuple[str, ...]


def counterexample(prefixes: Sequence[Sequence[str]], done: Sequence[bool], k: int,
                   c: int = 60, *, mode: str = "ordered") -> tuple[Completion, Completion] | None:
    """Return two legal completions with different answers, or None if none was produced.

    This offline diagnostic uses exact fractions and the same duplicate-free,
    immutable rankings and string tie-break as Fence. A missing example is
    not a certificate. Use Fence or verify for certification.

    >>> first, second = counterexample([["a", "b"], ["b"]], [True, False], 2, c=1)
    >>> first.suffixes, first.ids
    (((), ()), ('b', 'a'))
    >>> second.suffixes, second.ids
    (((), ('a',)), ('a', 'b'))
    """
    if type(k) is not int or k < 1 or type(c) is not int or c < 0:
        raise ValueError("k must be a positive integer and c a nonnegative integer")
    if mode not in ("ordered", "members"):
        raise ValueError("mode must be ordered or members")
    if not prefixes or len(prefixes) != len(done) or any(type(v) is not bool for v in done):
        raise ValueError("supply one boolean done flag per source")

    lower = {}
    covered = {}
    seen_by_source = []
    unseen = Fraction(0)
    discounts = [Fraction(0)]
    for items, closed in zip(prefixes, done):
        if isinstance(items, (str, bytes)):
            raise TypeError("each source must supply a sequence of string IDs")
        next_rank = len(items) + 1
        discounts.extend(Fraction(1, c + rank) for rank in range(len(discounts), next_rank + 1))
        tail = 0 if closed else discounts[next_rank]
        unseen += tail
        seen = set()
        for rank, item in enumerate(items, 1):
            if type(item) is not str:
                raise TypeError("each ID must be a string")
            if item in seen:
                raise ValueError("duplicate ID in source")
            seen.add(item)
            lower[item] = lower.get(item, 0) + discounts[rank]
            covered[item] = covered.get(item, 0) + tail
        seen_by_source.append(seen)
    if all(done):
        return None

    def lower_key(item):
        return -lower[item], item

    def upper_key(item):
        return -(lower.get(item, 0) + unseen - covered.get(item, 0)), item

    baseline = nsmallest(k, lower, key=lower_key)
    winners = set(baseline)
    # A string precedes every proper extension. The first absent all-NUL
    # string (including "") is the smallest ID the prefixes have never seen.
    fresh = ""
    while fresh in lower:
        fresh += "\0"
    outsiders = chain((fresh,), (item for item in lower if item not in winners))
    challenger = min(outsiders, key=upper_key)
    if len(baseline) == k and upper_key(challenger) >= lower_key(baseline[-1]):
        if mode == "members":
            return None
        challenger = next((b for a, b in zip(baseline, baseline[1:])
                           if upper_key(b) < lower_key(a)), None)
        if challenger is None:
            return None

    # Only the challenger's score changes. Give it the next rank everywhere
    # it can still appear, then stop; this attains its optimistic score.
    suffixes = tuple((challenger,) if not closed and challenger not in seen else ()
                     for seen, closed in zip(seen_by_source, done))
    # Every baseline outsider except the challenger keeps its old score.
    # Thus only the old top-k and this ID can enter the new top-k.
    bumped_key = upper_key(challenger)
    changed = nsmallest(k, winners | {challenger},
                        key=lambda item: bumped_key if item == challenger else lower_key(item))
    if mode == "members":
        baseline.sort()
        changed.sort()
    return (Completion(tuple(() for _ in prefixes), tuple(baseline)),
            Completion(suffixes, tuple(changed)))
