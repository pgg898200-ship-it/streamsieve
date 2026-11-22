import math
import random
import unittest
from fractions import Fraction

from recastcrps import Ensemble


def exact_crps(members, observation):
    """Pairwise definition evaluated with arbitrary precision."""
    total = sum(weight for _, weight in members.values())
    first = sum(w * abs(x - observation) for x, w in members.values())
    pairs = sum(w * v * abs(x - z) for x, w in members.values()
                for z, v in members.values())
    return Fraction(first, total) - Fraction(pairs, 2 * total**2)


class EnsembleTest(unittest.TestCase):
    def assert_withdrawals_match(self, ensemble, members, slots, observation):
        got = ensemble.leave_one_out(observation=observation)
        self.assertIsInstance(got, list)
        self.assertEqual(len(got), slots)
        for slot, score in enumerate(got):
            if slot not in members or len(members) == 1:
                self.assertIsNone(score)
            else:
                remaining = {i: member for i, member in members.items() if i != slot}
                expected = float(exact_crps(remaining, observation))
                self.assertIsInstance(score, float)
                self.assertAlmostEqual(score, expected, delta=math.ulp(expected))

    def test_leave_one_out_empty_and_singleton(self):
        self.assertEqual(Ensemble([], 0).leave_one_out(0), [])
        ensemble = Ensemble([0, 9], 4)
        self.assert_withdrawals_match(ensemble, {}, 4, 3)
        ensemble.set(2, 9, 11)
        self.assert_withdrawals_match(ensemble, {2: (9, 11)}, 4, 3)
        self.assertEqual((ensemble.count, ensemble.total_weight, ensemble.score(3)), (1, 11, 6))
        with self.assertRaises(OverflowError):
            ensemble.leave_one_out(2**80)
        with self.assertRaises(TypeError):
            ensemble.leave_one_out("3")

    def test_leave_one_out_weighted_ties_and_boundaries(self):
        low, high = -2**31, 2**31 - 1
        ensemble = Ensemble([low, 0, high], 4)
        members = {0: (low, 1), 1: (high, high - 7), 2: (low, 2), 3: (0, 4)}
        for slot, (value, weight) in members.items():
            ensemble.set(slot, value, weight)
        for observation in (low, -1, 0, high):
            before = ensemble.score(observation)
            self.assert_withdrawals_match(ensemble, members, 4, observation)
            self.assertEqual(ensemble.score(observation), before)
            self.assertEqual((ensemble.count, ensemble.total_weight), (4, high))
        del members[2]
        ensemble.erase(2)
        self.assert_withdrawals_match(ensemble, members, 4, -1)

    def test_withdrawal_extremes_ties_and_exterior_observations(self):
        ensemble = Ensemble([-10, 0, 10], 4)
        ensemble.set(0, -10)
        ensemble.set(2, 10)
        self.assertEqual(ensemble.withdrawal_extremes(observation=0),
                         ((-10, 10.0), (-10, 10.0)))
        self.assertEqual(ensemble.withdrawal_extremes(-15), ((10, 5.0), (-10, 25.0)))
        self.assertEqual(ensemble.withdrawal_extremes(50), ((-10, 40.0), (10, 60.0)))
        ensemble.set(2, -10)
        self.assertEqual(ensemble.withdrawal_extremes(50), ((-10, 60.0), (-10, 60.0)))

    def test_withdrawal_extremes_rejects_undefined_cases(self):
        ensemble = Ensemble([2, 3, 5], 3)
        with self.assertRaises(ValueError):
            ensemble.withdrawal_extremes(2)
        ensemble.set(0, 2)
        with self.assertRaises(ValueError):
            ensemble.withdrawal_extremes(2)
        ensemble.set(1, 3, weight=2)
        before = ensemble.score(2)
        with self.assertRaises(ValueError):
            ensemble.withdrawal_extremes(2)
        self.assertEqual((ensemble.count, ensemble.total_weight, ensemble.score(2)), (2, 3, before))
        ensemble.set(1, 3)
        self.assertEqual(ensemble.withdrawal_extremes(2), ((3, 0.0), (2, 1.0)))
        with self.assertRaises(TypeError):
            ensemble.withdrawal_extremes(2.5)
        with self.assertRaises(OverflowError):
            ensemble.withdrawal_extremes(2**31)

    def test_withdrawal_extremes_match_exhaustive(self):
        rng = random.Random(8842)
        support = [-2**31, -13, -3, 0, 1, 2, 3, 14, 2**31 - 1]
        slots = 14
        ensemble = Ensemble(support, slots)
        members = {0: (0, 1), 2: (1, 1)}
        ensemble.set(0, 0)
        ensemble.set(2, 1)
        for _ in range(150):
            slot = rng.randrange(slots)
            if slot in members and len(members) > 2 and rng.randrange(4) == 0:
                del members[slot]
                ensemble.erase(slot)
            else:
                value = rng.choice(support)
                members[slot] = (value, 1)
                ensemble.set(slot, value)
            observation = rng.choice(support) if rng.randrange(2) else rng.randrange(-20, 21)
            before = ensemble.leave_one_out(observation)
            full_score = ensemble.score(observation)
            exhaustive = []
            for i, (value, _) in members.items():
                remaining = {j: member for j, member in members.items() if i != j}
                score = exact_crps(remaining, observation)
                exhaustive.append((value, score))
            lowest = min(exhaustive, key=lambda pair: (pair[1], pair[0]))
            highest = max(exhaustive, key=lambda pair: (pair[1], -pair[0]))
            extremes = ensemble.withdrawal_extremes(observation)
            self.assertIsInstance(extremes, tuple)
            self.assertEqual(len(extremes), 2)
            for (value, score), (expected_value, exact) in zip(extremes, [lowest, highest]):
                expected = float(exact)
                self.assertEqual(value, expected_value)
                self.assertAlmostEqual(score, expected, delta=math.ulp(expected))
            defined = [s for s in before if s is not None]
            self.assertEqual((extremes[0][1], extremes[1][1]), (min(defined), max(defined)))
            self.assertEqual(ensemble.leave_one_out(observation), before)
            self.assertEqual((ensemble.count, ensemble.total_weight, ensemble.score(observation)),
                             (len(members), len(members), full_score))

    def test_replacement_and_erasure(self):
        ensemble = Ensemble(support=(x for x in [10, 0, 0]), slots=2)
        ensemble.set(slot=0, value=0)
        ensemble.set(1, 10, weight=3)
        self.assertEqual(ensemble.score(observation=5), 3.125)
        ensemble.set(1, 0, 2)
        self.assertEqual((ensemble.count, ensemble.total_weight), (2, 3))
        self.assertEqual(ensemble.score(5), 5)
        ensemble.erase(slot=1)
        ensemble.erase(1)
        self.assertEqual((ensemble.count, ensemble.total_weight), (1, 1))
        ensemble.erase(0)
        with self.assertRaises(ValueError):
            ensemble.score(5)

    def test_rejected_input_preserves_prior_state(self):
        ensemble = Ensemble([-2**31, 2**31 - 1], 2)
        ensemble.set(0, -2**31, 2**31 - 1)
        for args, expected in [
            ((0, 0), ValueError),
            ((0, 2**31), OverflowError),
            ((0, -2**31 - 1), OverflowError),
            ((0, 2**31 - 1, 0), ValueError),
            ((0, 2**31 - 1, 2**32), OverflowError),
            ((0, 2**31 - 1, -1), OverflowError),
            ((1, 2**31 - 1), OverflowError),
            ((2, -2**31), IndexError),
            ((2**200, -2**31), OverflowError),
            ((0, "-123"), TypeError),
            ((0, -2**31, 1.5), TypeError),
        ]:
            with self.subTest(args=args):
                with self.assertRaises(expected):
                    ensemble.set(*args)
                self.assertEqual((ensemble.count, ensemble.total_weight), (1, 2**31 - 1))
                self.assertEqual(ensemble.score(2**31 - 1), 2**32 - 1)

    def test_conversion_and_construction_errors(self):
        with self.assertRaises(TypeError):
            Ensemble(3, 2)
        with self.assertRaises(OverflowError):
            Ensemble([2**80], 2)
        with self.assertRaises(OverflowError):
            Ensemble([], -1)
        with self.assertRaises(OverflowError):
            Ensemble([], 2**200)

        def broken():
            yield 1
            raise LookupError("iteration failed")

        with self.assertRaisesRegex(LookupError, "iteration failed"):
            Ensemble(broken(), 2)

        class IntegerLike:
            def __index__(self):
                return 1

        one = IntegerLike()
        ensemble = Ensemble([one], one)
        ensemble.set(0, one, one)
        self.assertEqual(ensemble.score(one), 0)
        with self.assertRaises(TypeError):
            ensemble.score(1.0)
        with self.assertRaises(OverflowError):
            ensemble.score(2**31)
        with self.assertRaises(IndexError):
            ensemble.erase(1)

    def test_edits_match_pairwise_oracle(self):
        rng = random.Random(31)
        support = [-2**31, -160, 3, 45, 2**31 - 1]
        ensemble = Ensemble(support, 10)
        members = {}
        for step in range(400):
            slot = rng.randrange(10)
            if rng.randrange(4) == 0:
                ensemble.erase(slot)
                members.pop(slot, None)
            else:
                value = rng.choice(support)
                weight = rng.randint(1, 10**6)
                ensemble.set(slot, value, weight)
                members[slot] = (value, weight)
            self.assertEqual(ensemble.count, len(members))
            self.assertEqual(ensemble.total_weight, sum(w for _, w in members.values()))
            if members:
                y = rng.randrange(-2**31, 2**31)
                expected = float(exact_crps(members, y))
                self.assertAlmostEqual(ensemble.score(y), expected,
                                       delta=max(1e-15, abs(expected) * 2e-16))
                if step % 13 == 0:
                    self.assert_withdrawals_match(ensemble, members, 10, y)


if __name__ == "__main__":
    unittest.main()
