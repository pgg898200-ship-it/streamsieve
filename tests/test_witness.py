import itertools
import random
import unittest

from rankfence import Completion, counterexample
from tests.test_core import exact_order, finite_lists


class WitnessTests(unittest.TestCase):
    def check_pair(self, pair, prefixes, done, k, c, mode):
        self.assertIsInstance(pair, tuple)
        self.assertEqual(len(pair), 2)
        self.assertTrue(all(isinstance(case, Completion) for case in pair))
        self.assertNotEqual(pair[0].ids, pair[1].ids)
        self.assertEqual(pair[0].suffixes, tuple(() for _ in prefixes))
        self.assertLessEqual(len(set(itertools.chain.from_iterable(pair[1].suffixes))), 1)
        for case in pair:
            self.assertEqual(len(case.suffixes), len(prefixes))
            completed = []
            for prefix, closed, suffix in zip(prefixes, done, case.suffixes):
                self.assertLessEqual(len(suffix), int(not closed))
                items = tuple(prefix) + suffix
                self.assertEqual(len(items), len(set(items)))
                completed.append(items)
            exact = exact_order(completed, k, c)
            expected = tuple(sorted(exact)) if mode == "members" else exact
            self.assertEqual(case.ids, expected)

    def test_order_can_change_while_membership_stays_fixed(self):
        prefixes, done = [["a", "b"], ["b"]], [True, False]
        pair = counterexample(prefixes, done, 2, 1)
        self.check_pair(pair, prefixes, done, 2, 1, "ordered")
        self.assertEqual(pair[1].suffixes, ((), ("a",)))
        self.assertEqual(pair[1].ids, ("a", "b"))
        self.assertIsNone(counterexample(prefixes, done, 2, 1, mode="members"))

    def test_fresh_ids_break_ties_and_respect_existing_empty_id(self):
        pair = counterexample([["a"], []], [True, False], 1)
        self.assertEqual(pair[0].ids, ("a",))
        self.assertEqual(pair[1].ids, ("",))
        self.assertIsNone(counterexample([[""], []], [True, False], 1))
        sparse = [["", "\0"], []]
        pair = counterexample(sparse, [True, False], 4)
        self.check_pair(pair, sparse, [True, False], 4, 60, "ordered")
        self.assertEqual(pair[1].suffixes, ((), ("\0\0",)))

    def test_exact_witness_against_all_completions_in_small_universe(self):
        # All possible prefix states drawn from a,b; the oracle additionally
        # knows one fresh ID, smaller than every existing ID. Enumerate every
        # possible ordering and length of their remaining suffixes.
        prefix_lists = finite_lists(("a", "b"))
        full_lists = finite_lists(("", "a", "b"))
        states = list(itertools.product(prefix_lists, (False, True)))
        completions = {(p, closed): [q for q in full_lists if q == p or
                                     (not closed and q[:len(p)] == p)]
                       for p, closed in states}
        for c in (0, 60):
            oracle = {(l, r): exact_order((l, r), 3, c)
                      for l, r in itertools.product(full_lists, repeat=2)}
            for (left, left_done), (right, right_done) in itertools.product(states, repeat=2):
                p, flags = [left, right], [left_done, right_done]
                results = [oracle[key] for key in itertools.product(
                    completions[left, left_done], completions[right, right_done])]
                for k, mode in itertools.product((1, 2, 3, 4), ("ordered", "members")):
                    with self.subTest(p=p, flags=flags, k=k, c=c, mode=mode):
                        orders = {r[:k] for r in results}
                        if mode == "members":
                            orders = {tuple(sorted(r)) for r in orders}
                        pair = counterexample(p, flags, k, c, mode=mode)
                        self.assertEqual(pair is not None, len(orders) > 1)
                        if pair is not None:
                            self.check_pair(pair, p, flags, k, c, mode)

    def test_randomized_compact_witnesses(self):
        rng = random.Random(8052)
        for i in range(300):
            n, k, c = rng.randint(1, 8), rng.randint(1, 7), rng.choice((0, 1, 60, 2**110))
            p = [rng.sample(list("abcdefg"), rng.randint(0, 7)) for _ in range(n)]
            flags = [rng.choice((True, False)) for _ in p]
            for mode in ("ordered", "members"):
                with self.subTest(i=i, mode=mode):
                    pair = counterexample(p, flags, k, c, mode=mode)
                    if pair is not None:
                        self.check_pair(pair, p, flags, k, c, mode)

    def test_invalid_inputs(self):
        for p, flags, k, c, mode in [
            ([], [], 1, 60, "ordered"), ([[]], [], 1, 60, "ordered"),
            ([["a", "a"]], [True], 1, 60, "ordered"), ([["a"]], [1], 1, 60, "ordered"),
            ([["a"]], [False], 0, 60, "ordered"), ([["a"]], [False], 1, -1, "ordered"),
            ([["a"]], [False], 1, 60, "other"), ([["a", 1]], [True], 1, 60, "ordered"),
            (["abc"], [False], 1, 60, "ordered"),
        ]:
            with self.subTest(p=p, done=flags, k=k, c=c, mode=mode):
                with self.assertRaises((ValueError, TypeError)):
                    counterexample(p, flags, k, c, mode=mode)


if __name__ == "__main__":
    unittest.main()
