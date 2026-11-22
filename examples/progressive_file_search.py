"""Show a lexical preview while a semantic file search is still running.

Run with Python 3.10+ from the RankFence source checkout:
    python examples/progressive_file_search.py

This acceptance fixture models the UI contract in FileForce/files issue #4.
The three strings stand for stable file IDs shared by both retrievers. Page
arrivals and the UI deadline are explicit events, so reruns need no sleeps,
file index, vector database or network connection.
"""

from collections import defaultdict
from fractions import Fraction
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rankfence import Fence


LEXICAL = ("a", "b", "c")
SEMANTIC = ("b", "c", "a")
C = 60


def exhaustive_first(*rankings):
    """Independent RRF oracle with exact rational scores and lexical tie order."""
    scores = defaultdict(Fraction)
    for ranking in rankings:
        for rank, file_id in enumerate(ranking, start=1):
            scores[file_id] += Fraction(1, C + rank)
    return min(scores, key=lambda file_id: (-scores[file_id], file_id))


class ProgressiveFileSearch(unittest.TestCase):
    def test_deep_results_can_replace_preview(self):
        fusion = Fence(sources=2, k=1, c=C)
        fusion.push(0, LEXICAL[:1])
        self.assertEqual(LEXICAL[0], "a")  # Available to render immediately.
        self.assertIsNone(fusion.ordered())

        # With both first pages in, either first-place file can still win.
        fusion.push(1, SEMANTIC[:1])
        self.assertIsNone(fusion.ordered())
        self.assertEqual(exhaustive_first(("a", "c", "b"), ("b", "a", "c")), "a")

        fusion.push(0, LEXICAL[1:2])
        fusion.push(1, SEMANTIC[1:2])
        self.assertEqual(fusion.depths, (2, 2))
        self.assertEqual(fusion.done, (False, False))
        self.assertEqual(fusion.ordered(), ("b",))
        self.assertEqual(fusion.ordered(), (exhaustive_first(LEXICAL, SEMANTIC),))

    def test_deadline_preserves_preview_and_uncertainty(self):
        fusion = Fence(sources=2, k=1, c=C)
        fusion.push(0, LEXICAL, done=True)

        # The UI deadline fires with no semantic page. Render the available
        # lexical results. A deadline supplies no page or EOF to the fusion.
        self.assertEqual(exhaustive_first(LEXICAL), "a")
        self.assertIsNone(fusion.ordered())
        self.assertEqual(fusion.done, (True, False))

        # A late semantic completion at the same query snapshot changes first place.
        fusion.push(1, SEMANTIC, done=True)
        self.assertEqual(fusion.ordered(), ("b",))
        self.assertEqual(fusion.ordered(), (exhaustive_first(LEXICAL, SEMANTIC),))

    def test_successful_empty_deep_result(self):
        fusion = Fence(sources=2, k=1, c=C)
        fusion.push(0, LEXICAL, done=True)
        fusion.push(1, [], done=True)
        self.assertEqual(fusion.ordered(), ("a",))
        self.assertEqual(fusion.ordered(), (exhaustive_first(LEXICAL, ()),))


if __name__ == "__main__":
    unittest.main(verbosity=2)
