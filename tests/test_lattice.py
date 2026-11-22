"""Pure-stdlib unittest suite for EvidenceLattice CRDT axioms, echo immunity & wire sync."""

from __future__ import annotations

import math
import unittest

from streamsieve import EvidenceLattice, HypSummary, StreamSieveError, hash_provenance


class TestEvidenceLattice(unittest.TestCase):
    def test_string_and_int_ids_and_echo_suppression(self) -> None:
        with EvidenceLattice("strict_origin") as lat:
            self.assertEqual(lat.merge_mode, "strict_origin")
            lat.set_prior("db_outage", -1.0)
            lat.set_agent_weight(0, 1.0)
            lat.set_agent_weight(1, 1.2)

            # First observation: NOT an echo
            was_echo = lat.observe(
                "db_outage",
                "log_line:sha256:aaa",
                llr=2.0,
                agent_id=0,
                reliability=0.9,
                timestamp_ns=1000,
            )
            self.assertFalse(was_echo)

            s1 = lat.query("db_outage")
            self.assertIsInstance(s1, HypSummary)
            self.assertEqual(s1.hyp_label, "db_outage")
            self.assertEqual(s1.hyp_id, hash_provenance("db_outage"))
            self.assertEqual(s1.unique_evidence, 1)
            self.assertEqual(s1.echoes_suppressed, 0)
            self.assertAlmostEqual(s1.total_log_odds, 1.0, places=12)

            # 100x echo storm of the SAME provenance_id across different agents!
            for k in range(100):
                echo = lat.observe(
                    "db_outage",
                    "log_line:sha256:aaa",
                    llr=2.0,
                    agent_id=(k % 5) + 1,
                    reliability=0.95,
                    timestamp_ns=2000 + k,
                )
                self.assertTrue(echo)

            s2 = lat.query("db_outage")
            self.assertEqual(s2.unique_evidence, 1)
            self.assertEqual(s2.echoes_suppressed, 100)
            # Zero belief amplification from 100 echoes!
            self.assertEqual(s2.total_log_odds, s1.total_log_odds)
            self.assertEqual(s2.posterior_prob, s1.posterior_prob)
            self.assertEqual(s2.merged_e_value, s1.merged_e_value)

            # Integer hyp_id & provenance_id also supported directly
            self.assertFalse(lat.observe(42, 9001, llr=1.5, log_e=1.2, agent_id=1, timestamp_ns=50))
            self.assertTrue(lat.observe(42, 9001, llr=1.5, log_e=1.2, agent_id=2, timestamp_ns=60))
            hyps = lat.list_hyps()
            self.assertIn("db_outage", hyps)
            self.assertIn(42, hyps)
            raw_hyps = lat.list_hyps(resolve_labels=False)
            self.assertTrue(all(isinstance(h, int) for h in raw_hyps))

    def test_crdt_commutativity_associativity_idempotence(self) -> None:
        for mode in ("strict_origin", "max_reliability"):
            with (
                EvidenceLattice(mode) as a,
                EvidenceLattice(mode) as b,
                EvidenceLattice(mode) as c,
            ):
                a.set_prior("H", -0.5)
                b.set_prior("H", -0.5)
                c.set_prior("H", -0.5)

                a.observe("H", "p1", 1.5, log_e=1.5, agent_id=0, reliability=0.6, timestamp_ns=100)
                a.observe("H", "p2", -0.4, log_e=-0.2, agent_id=0, reliability=0.7, timestamp_ns=150)

                # b sees a conflicting echo of p1 with later ts but higher reliability
                b.observe("H", "p1", 2.2, log_e=2.0, agent_id=1, reliability=0.95, timestamp_ns=200)
                b.observe("H", "p3", 0.9, log_e=0.8, agent_id=1, reliability=0.8, timestamp_ns=120)

                c.observe("H", "p3", 0.9, log_e=0.8, agent_id=2, reliability=0.5, timestamp_ns=300)
                c.observe("H", "p4", 1.1, log_e=1.0, agent_id=2, reliability=0.85, timestamp_ns=90)

                # Non-mutating merged() does not mutate inputs
                a_before = a.query("H")
                b_before = b.query("H")

                with a.merged(b) as ab, b.merged(a) as ba:
                    self.assertEqual(a.query("H"), a_before)
                    self.assertEqual(b.query("H"), b_before)

                    sab = ab.query("H")
                    sba = ba.query("H")
                    # Commutativity (bit-exact!)
                    self.assertEqual(sab.total_log_odds, sba.total_log_odds)
                    self.assertEqual(sab.posterior_prob, sba.posterior_prob)
                    self.assertEqual(sab.merged_e_value, sba.merged_e_value)
                    self.assertEqual(sab.unique_evidence, 3)

                    # Mode semantics check on conflicting p1
                    if mode == "strict_origin":
                        # Earliest timestamp (100, llr=1.5) wins
                        expected_lo = -0.5 + 1.5 - 0.4 + 0.9
                    else:
                        # Highest reliability (0.95, llr=2.2) wins
                        expected_lo = -0.5 + 2.2 - 0.4 + 0.9
                    self.assertAlmostEqual(sab.total_log_odds, expected_lo, places=12)

                    # Associativity: (A U B) U C == A U (B U C)
                    with ab.merged(c) as ab_c, b.merged(c) as bc, a.merged(bc) as a_bc:
                        s_left = ab_c.query("H")
                        s_right = a_bc.query("H")
                        self.assertEqual(s_left.total_log_odds, s_right.total_log_odds)
                        self.assertEqual(s_left.posterior_prob, s_right.posterior_prob)
                        self.assertEqual(s_left.merged_e_value, s_right.merged_e_value)
                        self.assertEqual(s_left.unique_evidence, 4)

                    # Idempotence: merge(AB, A) == AB and merge(AB, AB) == AB
                    ab.merge(a)
                    ab.merge(ab)
                    s_idem = ab.query("H")
                    self.assertEqual(s_idem.total_log_odds, sab.total_log_odds)
                    self.assertEqual(s_idem.posterior_prob, sab.posterior_prob)
                    self.assertEqual(s_idem.merged_e_value, sab.merged_e_value)
                    self.assertEqual(s_idem.unique_evidence, sab.unique_evidence)

    def test_vovk_wang_merged_e_value_and_wire_roundtrip(self) -> None:
        with EvidenceLattice("max_reliability") as lat:
            lat.set_prior("hyp1", 0.25)
            lat.set_agent_weight(1, 2.0)
            lat.set_agent_weight(2, 1.0)
            lat.observe("hyp1", "src_a", llr=2.0, log_e=math.log(10.0), agent_id=1, reliability=0.9, timestamp_ns=10)
            lat.observe("hyp1", "src_b", llr=1.0, log_e=math.log(4.0), agent_id=2, reliability=0.8, timestamp_ns=20)

            s = lat.query("hyp1")
            # Weighted arithmetic mean of e-values: (2/3)*10 + (1/3)*4 = 24/3 = 8.0
            self.assertAlmostEqual(s.merged_e_value, 8.0, places=10)
            self.assertEqual(s.distinct_agents, 2)

            raw = lat.to_bytes()
            self.assertTrue(raw.startswith(b"SSLT"))
            b85 = lat.to_base85()
            self.assertIsInstance(b85, str)

            with EvidenceLattice.from_bytes(raw) as rt_bin, EvidenceLattice.from_base85(b85) as rt_txt:
                self.assertEqual(rt_bin.merge_mode, "max_reliability")
                self.assertEqual(rt_txt.merge_mode, "max_reliability")
                sb = rt_bin.query("hyp1")
                st = rt_txt.query("hyp1")
                self.assertEqual(sb.total_log_odds, s.total_log_odds)
                self.assertEqual(sb.merged_e_value, s.merged_e_value)
                self.assertEqual(st.total_log_odds, s.total_log_odds)
                self.assertEqual(st.merged_e_value, s.merged_e_value)

    def test_error_handling(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceLattice("unknown_mode")
        with self.assertRaises(StreamSieveError):
            EvidenceLattice.from_bytes(b"BAD!")
        with self.assertRaises(StreamSieveError):
            EvidenceLattice.from_base85("!!!not-valid-base85!!!")
        with EvidenceLattice() as lat:
            empty_q = lat.query("unseen_hyp")
            self.assertEqual(empty_q.posterior_prob, 0.5)
            self.assertEqual(empty_q.merged_e_value, 1.0)
            self.assertEqual(empty_q.unique_evidence, 0)


if __name__ == "__main__":
    unittest.main()
