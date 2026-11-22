"""Pure-stdlib unittest suite for VOISifter and one-line sift_matrix helper."""

from __future__ import annotations

from array import array
import random
import unittest

from streamsieve import EStream, StreamBrief, StreamSieveError, VOIAction, VOISifter, sift_matrix


class TestVOISifter(unittest.TestCase):
    def test_loud_channel_immunity_and_hierarchical_propagation(self) -> None:
        with VOISifter(64, n_sites=8, ticks_per_epoch=8) as sf:
            self.assertEqual(sf.n_channels, 64)
            self.assertEqual(sf.n_sites, 8)
            self.assertEqual(sf.ticks_per_epoch, 8)

            base_probs = sf.posteriors()
            self.assertEqual(len(base_probs), 64)

            # Channel 10 has 5x background noise scale
            scales = array("d", [1.0] * 64)
            scales[10] = 5.0
            sf.set_noise_scales(scales)

            # Loud channel raw mean 1.5 (only 0.3 sigma!) vs normal channel 2 with strong bursts
            sf.observe_mean(10, 1.5)
            burst_ticks = array("d", [0.1, -0.2, 3.8, 0.0, 3.4, -0.1, 0.2, 0.1])
            sf.observe_ticks(2, memoryview(burst_ticks))

            probs = sf.posteriors()
            self.assertGreater(probs[2], 0.5)
            self.assertLess(probs[10], 0.2)

            # Hierarchical hot-site propagation: sibling channel 3 (site 0) boosted over cold site 5 (ch 40)
            self.assertGreater(probs[3], probs[40])

            # Direct site aggregate observation also lifts channels in site 3 (channels 24..31)
            before_ch24 = probs[24]
            sf.observe_site_agg(3, 3.0)
            after_probs = sf.posteriors()
            self.assertGreater(after_probs[24], before_ch24)

            # step_epoch decays posteriors toward prior
            sf.step_epoch(stay_prob=0.80)
            decayed = sf.posteriors()
            self.assertLess(decayed[2], after_probs[2])

    def test_multi_agent_allocation_and_tool_flags(self) -> None:
        with VOISifter(64, n_sites=8, ticks_per_epoch=4) as sf:
            sf.observe_ticks(2, [0.0, 3.9, -0.1, 3.7])

            acts = sf.allocate(budget_per_agent=2, n_agents=4)
            self.assertGreater(len(acts), 0)
            self.assertTrue(all(isinstance(a, VOIAction) for a in acts))

            # Per-agent budget strictly respected
            cost_by_agent: dict[int, int] = {}
            for a in acts:
                self.assertIn(a.agent_slot, range(4))
                self.assertIn(a.tool_kind, ("site_agg", "channel_mean", "channel_ticks"))
                self.assertGreater(a.expected_voi, 0.0)
                cost_by_agent[a.agent_slot] = cost_by_agent.get(a.agent_slot, 0) + a.cost
            for slot, total_cost in cost_by_agent.items():
                self.assertLessEqual(total_cost, 2, f"Agent {slot} exceeded budget")

            # All 8 actions in this 2-unit wave target distinct (tool_kind, target_id) pairs
            targets = [(a.tool_kind, a.target_id) for a in acts]
            self.assertEqual(len(targets), len(set(targets)))

            # Tool restriction flags respected
            only_means = sf.allocate(
                budget_per_agent=3, n_agents=2, allow_site_agg=False, allow_mean=True, allow_ticks=False
            )
            self.assertGreater(len(only_means), 0)
            self.assertTrue(all(a.tool_kind == "channel_mean" for a in only_means))

            only_ticks = sf.allocate(
                budget_per_agent=4, n_agents=2, allow_site_agg=False, allow_mean=False, allow_ticks=True
            )
            self.assertEqual(len(only_ticks), 2)
            self.assertTrue(all(a.tool_kind == "channel_ticks" and a.cost == 4 for a in only_ticks))

            # Zero budget / zero agents returns empty list
            self.assertEqual(sf.allocate(budget_per_agent=0, n_agents=4), [])
            self.assertEqual(sf.allocate(budget_per_agent=4, n_agents=0), [])

    def test_validation_errors(self) -> None:
        with self.assertRaises(StreamSieveError):
            VOISifter(0, 1)
        with VOISifter(8, n_sites=2) as sf:
            with self.assertRaises(StreamSieveError):
                sf.set_noise_scales([1.0, 2.0])
            with self.assertRaises(StreamSieveError):
                sf.observe_site_agg(99, 1.0)
            with self.assertRaises(StreamSieveError):
                sf.observe_mean(99, 1.0)
            with self.assertRaises(StreamSieveError):
                sf.observe_ticks(0, [])

    def test_uneven_site_partition_no_underflow(self) -> None:
        """Uneven (n_channels, n_sites) pairs must partition every site cleanly without underflow."""
        for n_ch, n_sites in ((7, 5), (11, 7), (5, 4), (13, 9)):
            with VOISifter(n_ch, n_sites=n_sites, ticks_per_epoch=4) as sf:
                for s in range(n_sites):
                    sf.observe_site_agg(s, 1.5 if s == n_sites - 1 else 0.0)
                probs = sf.posteriors()
                self.assertEqual(len(probs), n_ch)
                self.assertTrue(all(0.0 <= p <= 1.0 for p in probs))
                # Last channel belongs to last (hot) site and must have elevated posterior
                self.assertGreater(probs[-1], probs[0])
                acts = sf.allocate(budget_per_agent=2, n_agents=2)
                self.assertGreater(len(acts), 0)

    def test_sift_matrix_one_liner_and_lossless_drilldown(self) -> None:
        rng = random.Random(77)
        n_ticks, n_ch = 240, 16
        names = [f"svc_{i}" for i in range(n_ch)]
        mat = [[rng.gauss(0.0, 1.0) for _ in range(n_ch)] for _ in range(n_ticks)]
        for t in range(100, n_ticks):
            mat[t][5] += 2.2

        est, brief = sift_matrix(
            mat, names=names, alpha=0.05, warmup=32, max_channels=5, ring_capacity=256
        )
        try:
            self.assertIsInstance(est, EStream)
            self.assertIsInstance(brief, StreamBrief)
            self.assertIn(5, brief["rejected_channels"])
            top0 = brief["top"][0]
            self.assertEqual(top0["channel"], 5)
            self.assertEqual(top0["name"], "svc_5")

            # Lossless drill-down from handle recovers exact raw ticks
            raw = est.fetch_raw(top0["handle"])
            self.assertGreater(len(raw), 0)
            last_tick, last_val = raw[-1]
            self.assertEqual(last_tick, n_ticks)
            self.assertEqual(last_val, mat[-1][5])
        finally:
            est.close()

        with self.assertRaises(ValueError):
            sift_matrix([])


if __name__ == "__main__":
    unittest.main()
