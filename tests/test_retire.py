import itertools
import unittest

from rankfence.partition import PartitionFence
from rankfence.retire import retirable_sources
from tests.test_core import exact_order, finite_lists


class RetireTests(unittest.TestCase):
    def test_losing_shard_can_retire_while_winning_order_is_ambiguous(self):
        f = PartitionFence(["hot", "hot", "hot", "cold", "colder"], 2, c=1)
        f.push(0, ["a", "b"], done=True)
        f.push(1, ["b", "a"], done=True)
        self.assertIsNone(f.ordered())
        self.assertEqual(retirable_sources(f), {3, 4})
        self.assertEqual(f.done, (True, True, False, False, False))
        f.push(2, ["b"], done=True)
        self.assertEqual(f.ordered(), ("b", "a"))

    def test_equal_or_close_shard_is_never_retired(self):
        f = PartitionFence(["main", "other"], 1, c=60)
        f.push(0, ["a"], done=True)
        self.assertEqual(retirable_sources(f), set())  # possible new ID ties
        f.push(1, ["z"])
        self.assertEqual(retirable_sources(f), set())  # known ID ties

    def test_bounds_include_missing_sources_for_seen_ids(self):
        f = PartitionFence(["x", "x", "y", "y"], 1, c=1)
        f.push(0, ["a", "top"], done=True)
        f.push(1, ["top"], done=True)
        f.push(2, ["y1"], done=True)
        f.push(3, ["y2"])  # y1 can still gain 1/3, tying top
        self.assertEqual(retirable_sources(f), set())
        f.push(3, ["y3"])  # at most 1/4 now
        self.assertEqual(retirable_sources(f), {3})

    def test_stale_heap_entries_and_large_source_masks_are_read_only(self):
        f = PartitionFence(["hot"] * 3 + [str(i) for i in range(70)], 2, c=1)
        for i, p in enumerate((["a", "b", "ha"], ["b", "a", "hb"])):
            f.push(i, p, done=True)
        for i in range(3, 73):
            f.push(i, [f"p{i}"])
        heap_snapshots = {m: tuple(g.heap) for m, g in f._groups.items()}
        state = f.done, f.depths
        self.assertEqual(retirable_sources(f), set(range(3, 73)))
        self.assertEqual(retirable_sources(f), set(range(3, 73)))
        self.assertEqual((f.done, f.depths), state)
        self.assertEqual({m: tuple(g.heap) for m, g in f._groups.items()}, heap_snapshots)

    def test_exhaustive_disjoint_completions(self):
        # Two rankings in shard x and one in shard y. Allow one additional
        # never-seen ID in each shard when enumerating every legal suffix.
        def universe(items):
            full = finite_lists(items)
            states = list(itertools.product(finite_lists(items[:2]), (False, True)))
            return full, states, {
                (p, closed): [v for v in full if v == p or
                              (not closed and v[:len(p)] == p)]
                for p, closed in states
            }

        x, xs, xc = universe(("x1", "x2", "x0"))
        y, ys, yc = universe(("y1", "y2", "y0"))
        for c in (1, 60):
            oracle = {lists: exact_order(lists, 2, c)
                      for lists in itertools.product(x, x, y)}
            positive = 0
            for states in itertools.product(xs, xs, ys):
                for k in (1, 2):
                    f = PartitionFence(["x", "x", "y"], k, c)
                    for i, (items, closed) in enumerate(states):
                        f.push(i, items, done=closed)
                    retired = retirable_sources(f)
                    if not retired:
                        continue
                    positive += 1
                    self.assertTrue(all(not states[i][1] for i in retired))
                    shards = {"x" if i < 2 else "y" for i in retired}
                    with self.subTest(states=states, c=c, k=k):
                        for lists in itertools.product(xc[states[0]], xc[states[1]], yc[states[2]]):
                            self.assertFalse(any(doc[0] in shards for doc in oracle[lists][:k]))
            self.assertGreater(positive, 0)
