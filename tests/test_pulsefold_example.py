import io
import unittest

from examples.pulsefold_lattice import ingest
from streamsieve import EvidenceLattice


class PulsefoldExampleTest(unittest.TestCase):
    def test_overlap_order_namespaces_and_wire_roundtrip(self):
        a = "9 0 0 stale\n10 0 0 fresh\n15 0 0 stale\n22 0 1 stale\n"
        b = "22 0 1 stale\n9 0 0 stale\n22 1 1 stale\n22 0 1 fresh\n"
        with EvidenceLattice() as first, EvidenceLattice() as second:
            self.assertEqual(ingest(first, io.StringIO(a), "lab/1", 0, 1000), 4)
            self.assertEqual(ingest(second, io.StringIO(b), "lab/1", 1, 1000), 4)
            with EvidenceLattice.from_bytes(second.to_bytes()) as received:
                first.merge(received).merge(received)
            self.assertEqual(first.query("pulsefold/stale").unique_evidence, 4)
            self.assertEqual(first.query("pulsefold/fresh").unique_evidence, 2)
            self.assertEqual(first.query("pulsefold/stale").merged_e_value, 1.0)
            self.assertEqual(first.query("pulsefold/stale").total_log_odds, 0.0)
            ingest(first, io.StringIO("9 0 0 stale\n"), "lab/2", 0, 1000)
            self.assertEqual(first.query("pulsefold/stale").unique_evidence, 5)

    def test_rejects_bad_transitions(self):
        with EvidenceLattice() as lattice:
            for line in ("-1 0 0 stale", "0 0 0 unknown", "0 1 stale",
                         "18446744073709552 0 0 stale", "9 4294967296 0 stale"):
                with self.subTest(line=line), self.assertRaises(ValueError):
                    ingest(lattice, [line], "lab", 0, 1000)
            self.assertEqual(lattice.query("pulsefold/stale").unique_evidence, 0)
