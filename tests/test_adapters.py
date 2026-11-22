"""Unit tests for the zero-dependency drop-in river.drift adapter (EStreamDrift)."""

from __future__ import annotations

import math
import random
import unittest

from streamsieve import EStreamDrift


class TestEStreamDriftAdapter(unittest.TestCase):
    def test_river_duck_type_protocol_and_multi_changepoint_auto_reset(self) -> None:
        rng = random.Random(123)
        detections: list[tuple[int, str, float]] = []

        with EStreamDrift(alpha=0.05, warmup=24, auto_reset=True) as det:
            # Segment 1: stationary N(0,1), then sustained +2.5 shift at t=90
            # Segment 2: after auto-reset, stationary around 2.5, then drop back to 0.0 at t=260
            for t in range(420):
                if t < 90:
                    x = rng.gauss(0.0, 1.0)
                elif t < 260:
                    x = 2.5 + rng.gauss(0.0, 1.0)
                else:
                    x = rng.gauss(0.0, 1.0)
                ret = det.update(x)
                self.assertIs(ret, det)
                if det.drift_detected:
                    detections.append((t, det.mode, det.e_value))

            self.assertGreaterEqual(len(detections), 2, f"Expected >=2 detections across 2 shifts, got {detections}")
            self.assertGreaterEqual(detections[0][0], 90)
            self.assertLess(detections[0][0], 170)
            self.assertTrue(any(t >= 260 for t, _, _ in detections), f"Missed 2nd shift after reset: {detections}")
            self.assertEqual(det.total_ticks, 420)
            self.assertEqual(det.n_detections, len(detections))

            cloned = det.clone()
            try:
                self.assertEqual(cloned.total_ticks, 0)
                self.assertFalse(cloned.drift_detected)
                self.assertEqual(cloned.e_value, 1.0)
            finally:
                cloned.close()

    def test_detects_zero_mean_symmetric_bursts(self) -> None:
        rng = random.Random(456)
        hit = False
        with EStreamDrift(alpha=0.05, warmup=24, auto_reset=False) as det:
            for t in range(360):
                if t >= 120 and rng.random() < 0.22:
                    x = (4.0 if rng.random() < 0.5 else -4.0) + rng.gauss(0.0, 0.3)
                else:
                    x = rng.gauss(0.0, 1.0)
                det.update(x)
                if det.drift_detected:
                    hit = True
                    self.assertIn(det.mode, ("burst", "shift+burst"))
                    break
        self.assertTrue(hit, "EStreamDrift failed to detect zero-mean symmetric +-4 sigma bursts")

    def test_null_calibration_heavy_tail_and_dc_offset(self) -> None:
        rng = random.Random(789)
        false_alarms = 0
        n_trials = 60
        for _ in range(n_trials):
            with EStreamDrift(alpha=0.05, warmup=24, auto_reset=False, check_every=5) as det:
                for _ in range(250):
                    # 1e10 DC + heavy-tailed LogNormal
                    x = 1e10 + math.exp(rng.gauss(0.0, 1.0))
                    det.update(x)
                    if det.drift_detected:
                        false_alarms += 1
                        break
        self.assertLessEqual(false_alarms / n_trials, 0.12, f"Too many null false alarms: {false_alarms}/{n_trials}")


if __name__ == "__main__":
    unittest.main()
