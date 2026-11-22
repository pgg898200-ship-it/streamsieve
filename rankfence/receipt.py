"""Independent, exact-rational verifier for global and partitioned prefix receipts."""
from fractions import Fraction


def verify(receipt: object, *, source_groups=None) -> bool:
    """Check a JSON-compatible ordered or membership proof against its supplied prefixes.

    Upstream prefix order, uniqueness beyond the supplied prefixes, and EOF markers
    are source assertions. A partition receipt also asserts disjoint ID spaces.
    Pass source_groups from a trusted index layout to pin that assertion.
    Verification makes no network requests.
    """
    if not isinstance(receipt, dict) or receipt.get("format") not in ("rankfence/1", "rankfence-partition/1"):
        return False
    c, k, mode = (receipt.get(key) for key in ("c", "k", "mode"))
    if type(c) is not int or c < 0 or type(k) is not int or k < 1 or mode not in ("ordered", "members"):
        return False
    prefixes, done, winners = (receipt.get(key) for key in ("prefixes", "done", "ids"))
    if not isinstance(prefixes, list) or not prefixes or not isinstance(done, list) or len(done) != len(prefixes):
        return False
    if not all(type(flag) is bool for flag in done):
        return False
    if not isinstance(winners, list) or not all(isinstance(item, str) for item in winners):
        return False
    chosen = set(winners)
    if len(chosen) != len(winners):
        return False

    if receipt["format"] == "rankfence-partition/1":
        groups = receipt.get("source_groups")
        if not isinstance(groups, list) or len(groups) != len(prefixes):
            return False
        if any(type(group) is not str for group in groups):
            return False
        if source_groups is not None:
            if not isinstance(source_groups, (list, tuple)) or tuple(source_groups) != tuple(groups):
                return False
    else:
        if source_groups is not None or "source_groups" in receipt:
            return False
        groups = [None] * len(prefixes)

    lower, covered, owners, budgets = {}, {}, {}, {}
    for prefix, closed, group in zip(prefixes, done, groups):
        if not isinstance(prefix, list) or not all(isinstance(item, str) for item in prefix):
            return False
        if len(prefix) != len(set(prefix)):
            return False
        ceiling = Fraction(0) if closed else Fraction(1, c + len(prefix) + 1)
        budgets[group] = budgets.get(group, 0) + ceiling
        for rank, item in enumerate(prefix, 1):
            if owners.setdefault(item, group) != group:
                return False
            lower[item] = lower.get(item, 0) + Fraction(1, c + rank)
            covered[item] = covered.get(item, 0) + ceiling

    if not chosen.issubset(lower) or len(chosen) != min(k, len(lower)):
        return False
    if len(chosen) < k and not all(done):
        return False
    if not chosen:
        return all(done)

    def lower_key(item):
        return -lower[item], item

    def upper_key(item):
        return -(lower[item] + budgets[owners[item]] - covered[item]), item

    weakest = max(chosen, key=lower_key)
    if not all(done) and lower[weakest] <= max(budgets.values()):
        return False
    if any(upper_key(item) <= lower_key(weakest) for item in lower if item not in chosen):
        return False
    return mode == "members" or all(
        lower_key(a) < upper_key(b) for a, b in zip(winners, winners[1:])
    )
