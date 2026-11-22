"""Pure-stdlib unittest suite for TrustCalibrator (Platt, Ville quarantine, delayed AgACI)."""

from __future__ import annotations

import math
import random
import unittest

from streamsieve import AgentTrust, StreamSieveError, TrustCalibrator, TrustDecision


class TestTrustCalibrator(unittest.TestCase):
    def test_binary_prob_fusion_quarantine_and_delayed_out_of_order_resolution(self) -> None:
        rng = random.Random(7)
        pending: list[tuple[int, float]] = []

        with TrustCalibrator(3, conformal_alpha=0.10) as tc:
            self.assertEqual(tc.n_agents, 3)
            for t in range(80):
                y = 1.0 if rng.random() < 0.5 else 0.0
                good = 0.85 if y > 0.5 else 0.15
                bloviator = 0.99 if rng.random() < 0.5 else 0.01
                poisoned = 0.01 if y > 0.5 else 0.99

                dec = tc.predict_prob(t, [good, bloviator, poisoned], underage_cost=4.0, overage_cost=1.0)
                self.assertIsInstance(dec, TrustDecision)
                self.assertEqual(dec.decision_id, t)
                self.assertTrue(0.0 <= dec.fused_prob <= 1.0)
                self.assertTrue(0.0 <= dec.ci_low <= dec.fused_prob <= dec.ci_high <= 1.0)
                self.assertIn(dec.optimal_action, (0.0, 1.0))

                pending.append((t, y))
                # Resolve out of order with a 4-tick delay in reversed chunks
                if len(pending) >= 4:
                    batch = pending[:4]
                    del pending[:4]
                    for did, label in reversed(batch):
                        tc.resolve(did, label)

            for did, label in pending:
                tc.resolve(did, label)

            agents = tc.agents()
            self.assertEqual(len(agents), 3)
            self.assertTrue(all(isinstance(a, AgentTrust) for a in agents))

            # Good agent dominates, poisoned agent is Ville-quarantined with weight 0
            self.assertGreater(agents[0].weight, 0.75)
            self.assertFalse(agents[0].quarantined)
            self.assertTrue(agents[2].quarantined)
            self.assertEqual(agents[2].weight, 0.0)
            self.assertEqual(agents[0].n_resolved, 80)

            # Overconfident random agent gets Platt-shrunk (slope pulled down from 1.0)
            self.assertLess(agents[1].platt_slope, 0.7)

    def test_abstaining_agents_string_ids_and_cost_asymmetry(self) -> None:
        with TrustCalibrator(3) as tc:
            # None and NaN abstentions handled cleanly
            dec_high_underage = tc.predict_prob(
                "evt:alpha", [0.40, None, math.nan], underage_cost=9.0, overage_cost=1.0
            )
            # Bayes threshold = co / (cu + co) = 0.10 -> 0.40 >= 0.10 => action 1.0
            self.assertEqual(dec_high_underage.active_agents, 1)
            self.assertEqual(dec_high_underage.optimal_action, 1.0)

            dec_high_overage = tc.predict_prob(
                "evt:beta", [0.40, None, math.nan], underage_cost=1.0, overage_cost=9.0
            )
            # Bayes threshold = 0.90 -> 0.40 < 0.90 => action 0.0
            self.assertEqual(dec_high_overage.optimal_action, 0.0)

            tc.resolve("evt:alpha", 1.0)
            tc.resolve("evt:beta", 0.0)

            # Double-resolving or unknown ID raises StreamSieveError (SS_ERR_BOUNDS)
            with self.assertRaises(StreamSieveError):
                tc.resolve("evt:alpha", 1.0)
            with self.assertRaises(StreamSieveError):
                tc.resolve(999999, 1.0)

    def test_scalar_fusion_agaci_and_newsvendor_asymmetry(self) -> None:
        rng = random.Random(21)
        with TrustCalibrator(3, conformal_alpha=0.10) as tc:
            covered = 0
            n_eval = 0
            for t in range(100):
                truth = 10.0 + rng.gauss(0.0, 1.0)
                good_m = truth + rng.gauss(0.0, 0.4)
                noisy_m = truth + rng.gauss(0.0, 2.5)
                bad_m = truth + 18.0  # Strongly biased Byzantine forecaster

                stds = None if (t % 2 == 0) else [0.5, 2.5, 1.0]
                dec = tc.predict_scalar(
                    t, [good_m, noisy_m, bad_m], agent_stds=stds, underage_cost=4.0, overage_cost=1.0
                )
                self.assertLessEqual(dec.ci_low, dec.fused_point)
                self.assertLessEqual(dec.fused_point, dec.ci_high)
                # Underage > overage => newsvendor optimal order > fused mean
                self.assertGreater(dec.optimal_action, dec.fused_point)

                if t >= 20:
                    n_eval += 1
                    if dec.ci_low <= truth <= dec.ci_high:
                        covered += 1

                tc.resolve(t, truth)

            # Verify symmetric/reversed cost direction
            dec_low = tc.predict_scalar(
                500, [10.0, 10.0, 28.0], agent_stds=[1.0, 1.0, 1.0], underage_cost=1.0, overage_cost=4.0
            )
            self.assertLess(dec_low.optimal_action, dec_low.fused_point)

            agents = tc.agents()
            self.assertGreater(agents[0].weight, agents[1].weight)
            self.assertTrue(agents[2].quarantined)
            self.assertEqual(agents[2].weight, 0.0)
            # Online AgACI coverage should be sensible (well above 70% for 90% target)
            self.assertGreater(covered / n_eval, 0.70)

    def test_regime_flip_recovery(self) -> None:
        rng = random.Random(99)
        with TrustCalibrator(2, fixed_share_alpha=0.06, quarantine_e_thresh=500.0) as tc:
            # Phase 1: agent 0 good, agent 1 poor
            for t in range(35):
                y = 1.0 if rng.random() < 0.5 else 0.0
                p0 = 0.88 if y > 0.5 else 0.12
                p1 = 0.35 if y > 0.5 else 0.65
                tc.predict_prob(t, [p0, p1])
                tc.resolve(t, y)
            w_phase1 = [a.weight for a in tc.agents()]
            self.assertGreater(w_phase1[0], w_phase1[1])

            # Phase 2: roles flip! Fixed-share mixing lets agent 1 recover swiftly
            for t in range(35, 95):
                y = 1.0 if rng.random() < 0.5 else 0.0
                p0 = 0.35 if y > 0.5 else 0.65
                p1 = 0.88 if y > 0.5 else 0.12
                tc.predict_prob(t, [p0, p1])
                tc.resolve(t, y)
            w_phase2 = [a.weight for a in tc.agents()]
            self.assertGreater(w_phase2[1], w_phase2[0])

    def test_validation_errors(self) -> None:
        with self.assertRaises(StreamSieveError):
            TrustCalibrator(0)
        with TrustCalibrator(2) as tc:
            with self.assertRaises(StreamSieveError):
                tc.predict_prob(1, [0.5])
            with self.assertRaises(StreamSieveError):
                tc.predict_scalar(1, [1.0, 2.0], agent_stds=[1.0])
            tc.predict_prob(1, [0.5, 0.5])
            with self.assertRaises(StreamSieveError):
                tc.resolve(1, math.nan)


if __name__ == "__main__":
    unittest.main()
