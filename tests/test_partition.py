import itertools
import random
import unittest
from fractions import Fraction

from rankfence import Fence
from rankfence.partition import PartitionFence


def exhaustive(lists, k, c):
    scores = {}
    for docs in lists:
        for rank, doc in enumerate(docs, 1):
            scores[doc] = scores.get(doc, 0) + Fraction(1, c + rank)
    return tuple(sorted(scores, key=lambda item: (-scores[item], item))[:k])


class PartitionFenceTests(unittest.TestCase):
    def test_scope_enables_early_global_order(self):
        groups = ["west", "east", "west", "east"]
        scoped = PartitionFence(groups, 1)
        generic = Fence(len(groups), 1)
        for state in (scoped, generic):
            for i, page in enumerate((["w"], ["a"], ["w"], ["b"])):
                state.push(i, page)
        self.assertEqual(scoped.ordered(), ("w",))
        self.assertEqual(scoped.members(), {"w"})
        self.assertIsNone(generic.members())

    def test_unseen_competing_shard(self):
        state = PartitionFence(["small", "large", "large"], 1)
        state.push(0, ["small-1"], done=True)
        self.assertIsNone(state.members())
        state.push(1, ["large-1"])
        self.assertIsNone(state.members())
        state.push(2, ["large-1"])
        self.assertEqual(state.ordered(), ("large-1",))

    def test_early_ties_and_sparse_eof(self):
        state = PartitionFence(["a", "b", "c"], 2)
        for i, doc in enumerate(("z", "b", "a")):
            state.push(i, [doc])
        self.assertEqual(state.ordered(), ("a", "b"))
        state = PartitionFence(["a", "b", "a"], 5, c=0)
        state.push(0, [], done=True)
        state.push(1, ["b"])
        state.push(2, ["a"], done=True)
        self.assertIsNone(state.members())
        state.push(1, [], done=True)
        self.assertEqual(state.ordered(), ("a", "b"))
        self.assertEqual(state.done, (True,) * 3)
        self.assertEqual(state.depths, (0, 1, 1))

    def test_inconsistent_page_is_atomic(self):
        state = PartitionFence(["s", "t", "s"], 2)
        state.push(0, ["w"])
        state.push(2, ["w"])
        with self.assertRaisesRegex(ValueError, "different source groups"):
            state.push(1, iter(["next", "w"]), done=True)
        self.assertEqual(state.depths, (1, 0, 1))
        self.assertFalse(any(state.done))
        state.push(1, ["next"], done=True)
        with self.assertRaises(ValueError):
            state.push(0, ["w"])
        self.assertEqual(state.depths, (1, 1, 1))

    def test_names_and_ids(self):
        for bad in ([], "single", [1, 2]):
            with self.assertRaises((ValueError, TypeError)):
                PartitionFence(bad, 2)
        groups = ["a", "b"]
        state = PartitionFence(groups, 1)
        groups[1] = "a"
        for bad_ids in ("string", [1]):
            with self.assertRaises(TypeError):
                state.push(0, bad_ids)
        state.push(0, ["x"])
        with self.assertRaises(ValueError):
            state.push(1, ["x"])

    def test_certificates_during_adversarial_schedules(self):
        rng = random.Random(8)
        for _ in range(300):
            group_count = rng.randint(1, 5)
            groups = [str(rng.randrange(group_count)) for _ in range(rng.randint(1, 10))]
            docs = {group: [f"{group}:{i}" for i in range(rng.randint(1, 8))]
                    for group in sorted(set(groups))}
            lists = [rng.sample(docs[g], rng.randrange(len(docs[g]) + 1)) for g in groups]
            k = rng.randint(1, 8)
            c = rng.choice([0, 2, 60, 2**100])
            expected = exhaustive(lists, k, c)
            state = PartitionFence(groups, k, c)
            active = list(range(len(groups)))
            while active:
                source = rng.choice(active)
                pos = state.depths[source]
                page = lists[source][pos:pos + rng.randint(1, 3)]
                done = pos + len(page) == len(lists[source])
                state.push(source, page, done=done)
                if done:
                    active.remove(source)
                ordered, members = state.ordered(), state.members()
                if members is not None:
                    self.assertEqual(members, frozenset(expected))
                if ordered is not None:
                    self.assertEqual(ordered, expected)
            self.assertEqual(state.ordered(), expected)

    def test_all_tiny_rankings_and_prefixes(self):
        # Distinct completions of the same prefixes must all have the same
        # answer whenever the coordinator issues a certificate on that prefix.
        def lists_for(pool):
            return [p for n in range(len(pool) + 1) for p in itertools.permutations(pool, n)]

        a, b = lists_for("az"), lists_for("by")
        for lists in itertools.product(a, a, b):
            expected = exhaustive(lists, 2, 1)
            for depths in itertools.product(*(range(len(s) + 1) for s in lists)):
                state = PartitionFence(["first", "first", "second"], 2, c=1)
                for i, depth in enumerate(depths):
                    state.push(i, lists[i][:depth])
                if state.ordered() is not None:
                    self.assertEqual(state.ordered(), expected)
                if state.members() is not None:
                    self.assertEqual(state.members(), frozenset(expected))


if __name__ == "__main__":
    unittest.main()
