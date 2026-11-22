import itertools
import random
import unittest
from fractions import Fraction

from rankfence import Fence


def exact_order(lists, k, c):
    scores = {}
    for items in lists:
        for rank, item in enumerate(items, 1):
            scores[item] = scores.get(item, 0) + Fraction(1, c + rank)
    return tuple(sorted(scores, key=lambda item: (-scores[item], item))[:k])


def finite_lists(items):
    return [order for n in range(len(items) + 1)
            for order in itertools.permutations(items, n)]


class RankFenceTests(unittest.TestCase):
    def assert_certificate(self, ordered, members, expected):
        if ordered is not None:
            self.assertEqual(ordered, expected)
        if members is not None:
            self.assertEqual(members, frozenset(expected))

    def test_complete_short_empty_and_tie(self):
        cases = [
            ([[], [], []], 4),
            ([["only"], [], ["only"]], 7),
            ([["z", "a"], ["a", "z"], []], 1),
            ([["z", "a"], ["a", "z"], []], 2),
            ([["x", "z"], ["a", "z"], ["b", "z"]], 3),
        ]
        for lists, k in cases:
            with self.subTest(lists=lists, k=k):
                fence = Fence(len(lists), k, c=60)
                for i, items in enumerate(lists):
                    fence.push(i, items, done=True)
                expected = exact_order(lists, k, 60)
                self.assertEqual(fence.ordered(), expected)
                self.assertEqual(fence.members(), frozenset(expected))
                self.assertEqual(fence.depths, tuple(map(len, lists)))
                self.assertEqual(fence.done, (True,) * len(lists))

    def test_no_evidence_or_unresolved_late_winner(self):
        fence = Fence(3, 1)
        self.assertIsNone(fence.ordered())
        self.assertIsNone(fence.members())
        for i, item in enumerate(["apple", "orange", "kiwi"]):
            fence.push(i, [item])
        self.assertIsNone(fence.ordered())
        self.assertIsNone(fence.members())
        for i in range(3):
            fence.push(i, ["late"])
        self.assertEqual(fence.ordered(), ("late",))
        self.assertEqual(fence.members(), frozenset(["late"]))

    def test_rational_tie_with_different_accumulation_orders(self):
        # Ordinary left-to-right binary64 addition rounds the contribution
        # sequence 1/2 + 1/3 + 1/6 differently from 1/6 + 1/2 + 1/3.
        lists = []
        for i, (a, z) in enumerate([(1, 5), (2, 1), (5, 2)]):
            items = [f"fill-{i}-{rank}" for rank in range(5)]
            items[a - 1], items[z - 1] = "a", "z"
            lists.append(items)
        self.assertEqual(exact_order(lists, 1, 1), ("a",))
        fence = Fence(3, 1, c=1)
        for i, items in enumerate(lists):
            fence.push(i, items, done=True)
        self.assertEqual(fence.ordered(), ("a",))

    def test_can_certify_before_any_source_closes(self):
        fence = Fence(3, 2, c=1)
        for i in range(3):
            fence.push(i, ["first", "second"])
        self.assertEqual(fence.done, (False, False, False))
        self.assertEqual(fence.ordered(), ("first", "second"))
        self.assertEqual(fence.members(), frozenset(["first", "second"]))

    def test_members_settles_before_order(self):
        fence = Fence(2, 2, c=1)
        fence.push(0, ["a", "b"], done=True)
        fence.push(1, ["b"])
        # Second list can finish here (b wins) or add a (a wins the tie).
        self.assertIsNone(fence.ordered())
        self.assertEqual(fence.members(), frozenset(["a", "b"]))

    def test_duplicates_are_local_to_one_source_and_atomic(self):
        fence = Fence(2, 1)
        fence.push(0, ["a"])
        fence.push(1, ["a"])
        for page in (["b", "b"], ["b", "a"]):
            with self.subTest(page=page):
                with self.assertRaises(ValueError):
                    fence.push(0, page, done=True)
                self.assertEqual(fence.depths, (1, 1))
                self.assertEqual(fence.done, (False, False))
        fence.push(0, ["b"], done=True)
        fence.push(1, [], done=True)
        self.assertEqual(fence.ordered(), ("a",))

    def test_exhaustive_two_source_prefixes(self):
        # The oracle uses only complete rankings. It has no knowledge of
        # the implementation's intervals, rounding mode, or stopping rule.
        lists = finite_lists(("a", "b"))
        states = [(p, closed) for p in lists for closed in (False, True)]
        continuations = {
            (p, closed): [q for q in lists if q == p or
                          (not closed and q[:len(p)] == p)]
            for p, closed in states
        }
        for c, k in itertools.product((1, 60), (1, 2, 3)):
            answers = {(left, right): exact_order((left, right), k, c)
                       for left, right in itertools.product(lists, repeat=2)}
            for left, right in itertools.product(states, repeat=2):
                with self.subTest(left=left, right=right, c=c, k=k):
                    fence = Fence(2, k, c=c)
                    for i, (p, closed) in enumerate((left, right)):
                        fence.push(i, p, done=closed)
                    ordered, members = fence.ordered(), fence.members()
                    for completion in itertools.product(continuations[left],
                                                        continuations[right]):
                        self.assert_certificate(ordered, members, answers[completion])

    def test_random_prefixes_admit_multiple_completions(self):
        rng = random.Random(193847)
        universe = tuple("abcdefgh")
        for trial in range(200):
            n = rng.randint(2, 5)
            k = rng.randint(1, 6)
            c = rng.choice([1, 4, 60, 1000])
            fence = Fence(n, k, c=c)
            prefixes = [rng.sample(universe, rng.randint(0, 5)) for _ in range(n)]
            closed = [rng.choice([False, False, True]) for _ in range(n)]
            # Deliver arbitrary interleaved chunks without changing source ranks.
            pending = list(range(n))
            offsets = [0] * n
            while pending:
                i = rng.choice(pending)
                start = offsets[i]
                stop = min(len(prefixes[i]), start + rng.randint(1, 3))
                offsets[i] = stop
                final = stop == len(prefixes[i])
                fence.push(i, prefixes[i][start:stop], done=final and closed[i])
                if final:
                    pending.remove(i)
            ordered, members = fence.ordered(), fence.members()
            with self.subTest(trial=trial, prefixes=prefixes, closed=closed, c=c, k=k):
                for draw in range(12):
                    completion = []
                    for p, finished in zip(prefixes, closed):
                        pool = [x for x in (*universe, "!new", "zz/new") if x not in p]
                        extra = [] if finished or draw == 0 else rng.sample(
                            pool, rng.randint(0, len(pool)))
                        completion.append(p + extra)
                    expected = exact_order(completion, k, c)
                    self.assert_certificate(ordered, members, expected)
                for i in range(n):
                    if not closed[i]:
                        fence.push(i, completion[i][len(prefixes[i]):], done=True)
                self.assertEqual(fence.ordered(), expected)
                self.assertEqual(fence.members(), frozenset(expected))


if __name__ == "__main__":
    unittest.main()
