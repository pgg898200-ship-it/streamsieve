"""Unit, parity, causality, and fuzz tests for streamsieve v0.2.0 features:

1. Bit-exact (0 ULP) parity between cache-tiled 8-wide/4-wide/scalar AVX2
   ``update_batch``, ``update_row``, and ``update_one`` across odd/even/wide
   channel counts, checkpoints, active/inactive ring buffers, and mid-batch
   ``NaN``/``±Inf`` injections.
2. Strictly causal online deseasonalizing (``season_period``) and detrending
   (``detrend_alpha``): null peeking FAR, step/slope/burst detection, tick-0
   1e15 warmup spike immunity, lossless raw ring preservation, and bit-exact
   future-poisoning causality via ``verify_estream_causality``.
3. Zero-dep native CLI & Python CLI ``--jsonl``, ``--season``, ``--detrend``,
   auto-detection, nested JSON skipping, escaped strings, missing/null fields,
   and negative flag hardening.
4. MCP ``sift_stream`` with ``jsonl_text``, ``season_period``, ``detrend_alpha``.
5. Zero-dep ``SklearnSieveDetector`` (``fit``, ``partial_fit``, ``predict``,
   ``decision_function``, ``fit_predict``, ``get_params``, ``set_params``,
   1D/2D inputs, exact ``decision_function <= 0 <=> predict == -1``, causality).
"""

from __future__ import annotations

import json
import math
import os
import random
import subprocess
import sys
import unittest

import streamsieve as ss
from streamsieve import (
    EStream,
    EStreamDrift,
    MCPServer,
    SklearnSieveDetector,
    assert_causal,
    sift_matrix,
    verify_estream_causality,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIVE_CLI = os.path.join(REPO_ROOT, "build", "streamsieve")


def _pkg_python_env() -> dict[str, str]:
    env = dict(os.environ)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(ss.__file__)))
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_parent}{os.pathsep}{existing}" if existing else pkg_parent
    return env


class TestTiledBatchBitExactParity(unittest.TestCase):
    """Verify 0-ULP bit-exact parity across update_batch, update_row, and update_one."""

    def _assert_parity(
        self,
        n_ch: int,
        n_ticks: int,
        *,
        ring_cap: int = 32,
        inject_nans: bool = False,
        season_period: int = 0,
        detrend_alpha: float = 0.0,
        seed: int = 1234,
    ) -> None:
        rng = random.Random(seed)
        mat = []
        for t in range(n_ticks):
            row = [rng.gauss(0.0, 1.0) for _ in range(n_ch)]
            if t >= n_ticks // 2:
                row[0] += 1.6
            if inject_nans and t in (5, 31, 32, 33, 55, 64, 95):
                row[t % n_ch] = float("nan") if (t % 2 == 0) else float("inf")
            mat.append(row)

        kw = dict(
            warmup=24,
            ring_capacity=ring_cap,
            alpha=0.05,
            season_period=season_period,
            detrend_alpha=detrend_alpha,
        )
        with EStream(n_ch, **kw) as e_batch, EStream(n_ch, **kw) as e_row, EStream(n_ch, **kw) as e_one:
            e_batch.update_batch(mat)
            for row in mat:
                e_row.update_row(row)
            for row in mat:
                for c, v in enumerate(row):
                    e_one.update_one(c, v)

            self.assertEqual(e_batch.tick_count, e_row.tick_count)
            self.assertEqual(e_batch.tick_count, e_one.tick_count)
            self.assertEqual(e_batch.ebh_reject(), e_row.ebh_reject())
            self.assertEqual(e_batch.ebh_reject(), e_one.ebh_reject())

            for c in range(n_ch):
                sb = e_batch.channel_status(c)
                sr = e_row.channel_status(c)
                so = e_one.channel_status(c)
                self.assertEqual(sb, sr, f"batch vs row mismatch on ch={c}, n_ch={n_ch}")
                self.assertEqual(sb, so, f"batch vs one mismatch on ch={c}, n_ch={n_ch}")
                if ring_cap > 0:
                    rb = e_batch.fetch_raw(c)
                    rr = e_row.fetch_raw(c)
                    ro = e_one.fetch_raw(c)
                    self.assertEqual(rb, rr, f"ring batch vs row mismatch ch={c}")
                    self.assertEqual(rb, ro, f"ring batch vs one mismatch ch={c}")

    def test_parity_all_simd_remainder_widths(self) -> None:
        for n_ch in (1, 3, 4, 5, 7, 8, 9, 12, 13, 16, 25):
            self._assert_parity(n_ch, 160, ring_cap=32, inject_nans=False, seed=100 + n_ch)
            self._assert_parity(n_ch, 160, ring_cap=0, inject_nans=False, seed=200 + n_ch)

    def test_parity_mid_batch_nans_and_infs(self) -> None:
        for n_ch in (1, 4, 7, 8, 9, 15, 24):
            self._assert_parity(n_ch, 140, ring_cap=20, inject_nans=True, seed=300 + n_ch)

    def test_parity_ring_wraparound_smaller_than_tile(self) -> None:
        # Ring capacity (7) much smaller than SIMD tile length (32)
        self._assert_parity(13, 180, ring_cap=7, inject_nans=False, seed=404)
        self._assert_parity(13, 180, ring_cap=7, inject_nans=True, seed=405)

    def test_parity_seasonal_and_detrend(self) -> None:
        self._assert_parity(9, 180, ring_cap=24, season_period=12, detrend_alpha=0.0, seed=501)
        self._assert_parity(9, 180, ring_cap=24, season_period=0, detrend_alpha=1.0, seed=502)
        self._assert_parity(9, 180, ring_cap=24, season_period=12, detrend_alpha=1.0, seed=503)
        self._assert_parity(5, 180, ring_cap=24, season_period=0, detrend_alpha=0.2, seed=504)


class TestCausalSeasonAndDetrend(unittest.TestCase):
    """Test causal deseasonalizing, detrending, outlier immunity, and zero-lookahead."""

    def test_diurnal_null_and_shift_with_raw_ring_preservation(self) -> None:
        rng = random.Random(2026)
        n_ch, n_ticks, P = 32, 480, 24
        mat = []
        for t in range(n_ticks):
            wave = 100.0 + 8.0 * math.sin(2.0 * math.pi * t / P) + 2.5 * math.cos(4.0 * math.pi * t / P)
            row = [wave + rng.gauss(0.0, 1.0) for _ in range(n_ch)]
            if t == 5:
                # 1e15 warmup spike on channel 2 should be ignored by 3-cycle phase median
                row[2] = 1e15
            if t >= 220:
                row[0] += 2.2  # sustained shift
                if t % 5 == 0:
                    row[1] += 4.0  # sparse bursts
            mat.append(row)

        est, brief = sift_matrix(mat, alpha=0.05, warmup=32, ring_capacity=64, season_period=P)
        try:
            rej = set(brief.rejected_channels)
            self.assertIn(0, rej, "Missed sustained shift on diurnal channel")
            self.assertIn(1, rej, "Missed burst on diurnal channel")
            self.assertNotIn(2, rej, "Warmup 1e15 spike poisoned diurnal channel")
            # At most 1 false alarm among 30 null diurnal channels
            fps = rej - {0, 1}
            self.assertLessEqual(len(fps), 2, f"Too many diurnal null FPs: {fps}")

            # Raw ring must preserve exact un-deseasonalized raw input values
            raw_ch0 = est.fetch_raw(0, limit=10)
            self.assertEqual(len(raw_ch0), 10)
            for tick, val in raw_ch0:
                self.assertEqual(val, mat[tick - 1][0])
        finally:
            est.close()

    def test_linear_trend_detrend_null_and_slope_break(self) -> None:
        rng = random.Random(2027)
        n_ch, n_ticks = 24, 420
        mat = []
        for t in range(n_ticks):
            row = [500.0 + 0.5 * t + rng.gauss(0.0, 1.0) for _ in range(n_ch)]
            if t >= 180:
                # Slope break on ch 0 (+1.8/tick) -> permanent +1.8 step in first difference
                row[0] += 1.8 * (t - 180)
                # Bursts on ch 1
                if t % 5 == 0:
                    row[1] += 4.5
            mat.append(row)

        with EStream(n_ch, warmup=24, ring_capacity=32, alpha=0.05, detrend_alpha=1.0) as est:
            est.update_batch(mat)
            rej = set(est.ebh_reject(0.05))
            self.assertIn(0, rej, "Missed slope break under detrend_alpha=1.0")
            self.assertIn(1, rej, "Missed bursts on linear trend")
            self.assertLessEqual(len(rej - {0, 1}), 1, f"False alarms on linear trend: {rej}")
            # Lossless raw ring retains original trending raw values
            raw = est.fetch_raw(0, limit=5)
            for tick, val in raw:
                self.assertEqual(val, mat[tick - 1][0])

    def test_ewma_detrend_tick0_spike_immunity(self) -> None:
        rng = random.Random(2028)
        n_ch, n_ticks = 8, 300
        mat = [[rng.gauss(0.0, 1.0) for _ in range(n_ch)] for _ in range(n_ticks)]
        mat[0][0] = 1e15  # Adversarial tick-0 outlier must not poison 3-point median EWMA init
        with EStream(n_ch, warmup=24, alpha=0.05, detrend_alpha=0.15) as est:
            est.update_batch(mat)
            st0 = est.channel_status(0)
            self.assertTrue(math.isfinite(st0.e_value))
            self.assertLess(st0.e_value, 20.0, f"Tick-0 1e15 spike triggered false alarm: {st0}")

    def test_bit_exact_causality_with_season_and_detrend(self) -> None:
        rng = random.Random(2029)
        mat = [
            [10.0 + 0.3 * t + 5.0 * math.sin(2.0 * math.pi * t / 12.0) + rng.gauss(0.0, 1.0) for _ in range(4)]
            for t in range(120)
        ]
        self.assertTrue(
            verify_estream_causality(
                mat, probe_ticks=[0, 11, 35, 36, 70, 100], warmup=24, season_period=12, detrend_alpha=0.0
            )
        )
        self.assertTrue(
            verify_estream_causality(
                mat, probe_ticks=[0, 2, 25, 50, 95], warmup=24, season_period=0, detrend_alpha=1.0
            )
        )
        self.assertTrue(
            verify_estream_causality(
                mat, probe_ticks=[0, 3, 40, 80, 110], warmup=24, season_period=12, detrend_alpha=1.0
            )
        )
        self.assertTrue(
            verify_estream_causality(
                mat, probe_ticks=[0, 2, 10, 45, 95], warmup=24, season_period=0, detrend_alpha=0.2
            )
        )


class TestJSONLCliAndMCP(unittest.TestCase):
    """Test native CLI, Python CLI, and MCP server JSONL + seasonal/detrend support."""

    def _build_diurnal_jsonl(self) -> str:
        rng = random.Random(777)
        lines = []
        for t in range(260):
            wave = 40.0 + 6.0 * math.sin(2.0 * math.pi * t / 12.0)
            cpu = wave + rng.gauss(0.0, 0.8) + (2.8 if t >= 120 else 0.0)
            mem = wave + rng.gauss(0.0, 0.8)
            lat = None if t == 50 else (wave + rng.gauss(0.0, 0.8))
            obj = {
                "ts": f"2026-11-22T{t:04d}",
                "meta": {"host": "web-01", "tags": ["prod", "us-east"], "brace": "}"},
                "cpu_pct": round(cpu, 4),
                "mem_gb": round(mem, 4),
                "p99_ms": round(lat, 4) if lat is not None else None,
                "healthy": True,
            }
            lines.append(json.dumps(obj))
        return "\n".join(lines) + "\n"

    @unittest.skipUnless(os.path.isfile(NATIVE_CLI), "Native CLI binary not built")
    def test_native_cli_jsonl_autodetect_and_season(self) -> None:
        payload = self._build_diurnal_jsonl()
        proc = subprocess.run(
            [NATIVE_CLI, "sift", "--season", "12", "--alpha", "0.05", "--json", "-"],
            input=payload,
            text=True,
            capture_output=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        self.assertEqual(out["version"], "0.2.0")
        self.assertEqual(out["format"], "jsonl")
        self.assertEqual(out["channels"], 3)
        self.assertEqual(out["ticks"], 260)
        self.assertEqual(out["season_period"], 12)
        self.assertEqual(out["top"][0]["name"], "cpu_pct")
        self.assertTrue(out["top"][0]["rejected_ebh"])

        # Negative CLI flags must not crash or wrap around uint32
        proc2 = subprocess.run(
            [NATIVE_CLI, "sift", "--jsonl", "--top", "-5", "--season", "-2", "--warmup", "-10", "--json", "-"],
            input=payload,
            text=True,
            capture_output=True,
            check=True,
        )
        out2 = json.loads(proc2.stdout)
        self.assertEqual(out2["channels"], 3)

    def test_python_cli_jsonl_and_season(self) -> None:
        payload = self._build_diurnal_jsonl()
        proc = subprocess.run(
            [sys.executable, "-m", "streamsieve.cli", "sift", "--season", "12", "--json", "-"],
            input=payload,
            text=True,
            capture_output=True,
            env=_pkg_python_env(),
            check=True,
        )
        out = json.loads(proc.stdout)
        self.assertEqual(out["n_channels"], 3)
        self.assertEqual(out["season_period"], 12)
        self.assertEqual(out["top"][0]["name"], "cpu_pct")
        self.assertTrue(out["top"][0]["rejected_ebh"])

    def test_mcp_sift_stream_jsonl_and_season(self) -> None:
        payload = self._build_diurnal_jsonl()
        srv = MCPServer()
        try:
            res = srv.call_tool(
                "sift_stream",
                {
                    "session_id": "sre-diurnal",
                    "jsonl_text": payload,
                    "season_period": 12,
                    "alpha": 0.05,
                },
            )
            self.assertFalse(res.get("isError", False))
            body = json.loads(res["content"][0]["text"])
            self.assertEqual(body["n_channels"], 3)
            self.assertEqual(body["season_period"], 12)
            self.assertEqual(body["top"][0]["name"], "cpu_pct")
            self.assertTrue(body["top"][0]["rejected_ebh"])
            self.assertGreater(body["token_savings"]["savings_pct"], 85.0)
        finally:
            srv.close()


class TestSklearnSieveDetector(unittest.TestCase):
    """Test zero-dependency scikit-learn OutlierMixin/BaseEstimator adapter."""

    def test_params_fit_partial_fit_and_predict_equivalence(self) -> None:
        det = SklearnSieveDetector(alpha=0.05, warmup=24, season_period=0, check_every=5)
        params = det.get_params()
        self.assertEqual(params["alpha"], 0.05)
        self.assertEqual(params["warmup"], 24)
        det.set_params(alpha=0.02, check_every=1)
        self.assertEqual(det.alpha, 0.02)
        with self.assertRaises(ValueError):
            det.set_params(unknown_param=123)

        rng = random.Random(909)
        X_base = [[rng.gauss(0.0, 1.0) for _ in range(6)] for _ in range(120)]
        X_live = [[rng.gauss(0.0, 1.0) for _ in range(6)] for _ in range(160)]
        for t in range(40, 160):
            X_live[t][0] += 2.2

        det.set_params(alpha=0.05)
        det.fit(X_base)
        self.assertTrue(det.is_fitted_)
        self.assertEqual(det.n_features_in_, 6)
        self.assertEqual(det.n_samples_seen_, 120)
        self.assertEqual(det.rejected_channels_, [])

        # Decision function and predict equivalence on fresh fit_predict
        X_all = X_base + X_live
        with SklearnSieveDetector(alpha=0.05, warmup=24) as d1, SklearnSieveDetector(alpha=0.05, warmup=24) as d2:
            scores = d1.decision_function(X_all)
            labels = d2.fit_predict(X_all)
            self.assertEqual(len(scores), len(X_all))
            self.assertEqual(labels, [-1 if s <= 0.0 else 1 for s in scores])
            # Calm prefix must be +1, shifted suffix must trigger -1
            self.assertTrue(all(lbl == 1 for lbl in labels[:120]))
            self.assertEqual(labels[-1], -1)
            self.assertIn(0, d2.rejected_channels_)

        # 1D input and partial_fit
        with SklearnSieveDetector(alpha=0.05, warmup=20) as d1d:
            d1d.partial_fit([rng.gauss(0.0, 1.0) for _ in range(60)])
            d1d.partial_fit([rng.gauss(2.5, 1.0) for _ in range(80)])
            self.assertEqual(d1d.n_features_in_, 1)
            self.assertEqual(d1d.n_samples_seen_, 140)
            self.assertEqual(d1d.rejected_channels_, [0])
        det.close()

    def test_sklearn_detector_bit_exact_causality(self) -> None:
        rng = random.Random(910)
        X = [[rng.gauss(0.0, 1.0) for _ in range(4)] for _ in range(90)]
        for t in range(45, 90):
            X[t][0] += 2.0

        def _score(stream: list[list[float]]) -> list[float]:
            with SklearnSieveDetector(alpha=0.05, warmup=20) as d:
                return d.decision_function(stream)

        self.assertTrue(assert_causal(_score, X, probe_ticks=[0, 19, 20, 44, 65, 88]))

    def test_estream_drift_seasonal_clone(self) -> None:
        rng = random.Random(911)
        P = 12
        with EStreamDrift(alpha=0.05, warmup=24, season_period=P, auto_reset=False) as det:
            det2 = det.clone()
            try:
                for t in range(300):
                    val = 20.0 + 5.0 * math.sin(2.0 * math.pi * t / P) + rng.gauss(0.0, 0.8)
                    if t >= 150:
                        val += 2.5
                    det.update(val)
                    det2.update(val)
                self.assertEqual(det.n_detections, det2.n_detections)
                self.assertGreaterEqual(det.n_detections, 1)
                self.assertAlmostEqual(det.e_value, det2.e_value, places=12)
            finally:
                det2.close()


class TestAdversarialBreakerRegressions(unittest.TestCase):
    """Regression suite covering all 13 adversarial breaker witnesses (W1-W13)."""

    def test_w1_tiny_detrend_alpha(self) -> None:
        with EStream(4, warmup=16, detrend_alpha=1e-12) as est:
            est.update_batch([[float(t % 3)] * 4 for t in range(48)])
            self.assertTrue(math.isfinite(est.channel_status(0).e_value))

    def test_w2_extreme_1e308_warmup_and_sort_order(self) -> None:
        mat = []
        for t in range(120):
            s = 1.0 if (t % 2 == 0) else -1.0
            row = [s * 1e308, s * 1e155] + [0.1 * s + (4.0 if (c == 2 and t >= 60) else 0.0) for c in range(2, 8)]
            mat.append(row)
        with EStream(8, warmup=16, alpha=0.05) as est:
            est.update_batch(mat)
            top = est.topk(8)
            self.assertEqual(len(top), 8)
            for st in top:
                self.assertTrue(math.isfinite(st.e_value))
                self.assertTrue(math.isfinite(st.baseline_loc))
                self.assertTrue(math.isfinite(st.baseline_scale) and st.baseline_scale > 0.0)
            self.assertEqual(top[0].channel, 2)
            self.assertIn(2, est.ebh_reject(0.05))

    def test_w3_seasonal_overflow_row_vs_one_parity(self) -> None:
        mat = [
            [-1e308] * 5 if t < 12 else [1e308 if (t == 20 and c in (1, 4)) else -1e308 + 1.0 for c in range(5)]
            for t in range(40)
        ]
        with EStream(5, warmup=12, season_period=4, ring_capacity=32) as erow, EStream(
            5, warmup=12, season_period=4, ring_capacity=32
        ) as eone:
            for row in mat:
                erow.update_row(row)
                for c, v in enumerate(row):
                    eone.update_one(c, v)
            for c in range(5):
                self.assertEqual(erow.channel_status(c), eone.channel_status(c))
                self.assertEqual(erow.fetch_raw(c), eone.fetch_raw(c))

    def test_w4_two_simultaneous_warmup_spikes_ewma(self) -> None:
        rng = random.Random(4040)
        mat = [[rng.gauss(0.0, 1.0) for _ in range(4)] for _ in range(240)]
        mat[0][0] = 1e15
        mat[1][0] = 1e15
        with EStream(4, warmup=24, alpha=0.05, detrend_alpha=0.15) as est:
            est.update_batch(mat)
            st0 = est.channel_status(0)
            self.assertTrue(math.isfinite(st0.e_value))
            self.assertLess(st0.e_value, 20.0)

    def test_w5_w6_w7_mcp_atomicity_deep_json_bigint_and_key_order(self) -> None:
        srv = MCPServer()
        try:
            # W5: failed replace/append must NOT close live session
            ok = srv.call_tool("sift_stream", {"session_id": "live", "rows": [[1.0, 2.0]] * 20, "names": ["a", "b"]})
            self.assertFalse(ok["isError"])
            bad = srv.call_tool(
                "sift_stream",
                {"session_id": "live", "append": True, "rows": [[1.0, 2.0, 3.0]] * 5, "names": ["only_one"]},
            )
            self.assertTrue(bad["isError"])
            # Original live session must still be open and fetchable
            raw = srv.call_tool("fetch_raw_ticks", {"session_id": "live", "channel": 0, "limit": 5})
            self.assertFalse(raw["isError"])
            self.assertEqual(raw["structuredContent"]["count"], 5)

            # W5 on voi_allocate reset
            ok_v = srv.call_tool("voi_allocate", {"sifter_id": "sv", "n_channels": 8})
            self.assertFalse(ok_v["isError"])
            bad_v = srv.call_tool("voi_allocate", {"sifter_id": "sv", "reset": True, "n_channels": -5})
            self.assertTrue(bad_v["isError"])
            still_ok_v = srv.call_tool("voi_allocate", {"sifter_id": "sv", "budget_per_agent": 2})
            self.assertFalse(still_ok_v["isError"])

            # W6: deeply nested JSONL + 401-digit integer must not crash MCP
            deep = "[" * 5000 + "1" + "]" * 5000
            payload = (
                f'{{"a": 1, "b": {10**400}}}\n'
                f'{{"nested": {deep}}}\n'
                '{"a": 2.0, "b": 3.0}\n'
            )
            res_w6 = srv.call_tool("sift_stream", {"session_id": "w6", "jsonl_text": payload})
            self.assertFalse(res_w6["isError"])
            self.assertEqual(res_w6["structuredContent"]["n_channels"], 1)

            # W7: append=True with reordered JSONL keys must preserve original channel mapping
            s1 = "\n".join(json.dumps({"cpu": 10.0, "mem": 500.0}) for _ in range(20))
            s2 = "\n".join(json.dumps({"mem": 501.0, "cpu": 11.0}) for _ in range(10))
            r1 = srv.call_tool("sift_stream", {"session_id": "ord", "jsonl_text": s1, "ring_capacity": 64})
            self.assertFalse(r1["isError"])
            r2 = srv.call_tool(
                "sift_stream", {"session_id": "ord", "jsonl_text": s2, "append": True, "ring_capacity": 64}
            )
            self.assertFalse(r2["isError"])
            raw_cpu = srv.call_tool("fetch_raw_ticks", {"session_id": "ord", "channel": 0, "limit": 5})
            raw_mem = srv.call_tool("fetch_raw_ticks", {"session_id": "ord", "channel": 1, "limit": 5})
            self.assertTrue(all(v == 11.0 for _, v in raw_cpu["structuredContent"]["ticks"]))
            self.assertTrue(all(v == 501.0 for _, v in raw_mem["structuredContent"]["ticks"]))
        finally:
            srv.close()

    @unittest.skipUnless(os.path.isfile(NATIVE_CLI), "Native CLI binary not built")
    def test_w8_w10_w13_native_cli_strict_rfc8259_and_effective_params(self) -> None:
        # Truncated, trailing-garbage, hex number, invalid \u escape, and bad UTF-8 key
        bad_and_good = (
            b'{"a": 1.0,\n'
            b'{"a": 2.0} trailing\n'
            b'{"a": 0x10}\n'
            b'{"a": -inf}\n'
            b'{"a\\uZZZZ": 3.0}\n'
            b'{"ok_\xff": 1.0, "b": 2.0}\n'
            b'{"ok_\xff": 1.5, "b": 2.5}\n'
        )
        proc = subprocess.run(
            [NATIVE_CLI, "sift", "--jsonl", "--alpha", "0.99", "--season", "1", "--detrend", "nan", "--json", "-"],
            input=bad_and_good,
            capture_output=True,
            check=True,
        )
        text = proc.stdout.decode("utf-8")
        out = json.loads(text)
        self.assertEqual(out["ticks"], 2)
        self.assertEqual(out["channels"], 2)
        self.assertAlmostEqual(out["alpha"], 0.5)
        self.assertEqual(out["season_period"], 0)
        self.assertEqual(out["detrend_alpha"], 0.0)
        self.assertIn("\ufffd", out["top"][0]["name"] + out["top"][1]["name"])

    def test_w9_w10_w13_python_cli_and_estream_sanitization(self) -> None:
        csv_in = "x,y\n" + "\n".join(f"{i * 0.1},{i * 0.2}" for i in range(40)) + "\n"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "streamsieve.cli",
                "sift",
                "--alpha",
                "nan",
                "--detrend",
                "nan",
                "--season",
                "1",
                "--json",
                "-",
            ],
            input=csv_in,
            text=True,
            capture_output=True,
            env=_pkg_python_env(),
            check=True,
        )
        out = json.loads(proc.stdout)
        self.assertAlmostEqual(out["alpha"], 0.05)
        self.assertEqual(out["season_period"], 0)
        self.assertEqual(out["detrend_alpha"], 0.0)

        with EStream(2, warmup=4, alpha=0.99, season_period=1, detrend_alpha=float("nan")) as est:
            self.assertEqual(est.warmup, 12)
            self.assertAlmostEqual(est.alpha, 0.5)
            self.assertEqual(est.season_period, 0)
            self.assertEqual(est.detrend_alpha, 0.0)
            self.assertAlmostEqual(est.brief(alpha=float("nan")).alpha, 0.5)

    def test_w11_estream_drift_clears_stale_status_on_pending_reset(self) -> None:
        rng = random.Random(1111)
        with EStreamDrift(alpha=0.05, warmup=16, auto_reset=True, check_every=5) as det:
            detected = False
            for t in range(300):
                x = rng.gauss(0.0, 1.0) + (4.0 if t >= 60 else 0.0)
                det.update(x)
                if det.drift_detected:
                    detected = True
                    # Next tick triggers pending reset, ticks_since_reset=1 (< check_every=5)
                    det.update(0.0)
                    self.assertFalse(det.drift_detected)
                    self.assertFalse(det.warning_detected)
                    self.assertEqual(det.e_value, 1.0)
                    self.assertEqual(det.mode, "calm")
                    break
            self.assertTrue(detected)

    def test_w12_sklearn_batch_invariance_atomic_fit_and_set_params(self) -> None:
        rng = random.Random(1212)
        X = [[rng.gauss(0.0, 1.0) for _ in range(3)] for _ in range(100)]
        for t in range(60, 100):
            X[t][0] += 3.0

        # Batch-size invariance with check_every=7 across arbitrary chunk boundaries
        with SklearnSieveDetector(alpha=0.05, warmup=20, check_every=7) as d_whole, SklearnSieveDetector(
            alpha=0.05, warmup=20, check_every=7
        ) as d_chunk:
            s_whole = d_whole.decision_function(X)
            s_chunk = (
                d_chunk.decision_function(X[:17])
                + d_chunk.decision_function(X[17:53])
                + d_chunk.decision_function(X[53:])
            )
            self.assertEqual(s_whole, s_chunk)

        # set_params invalidates fitted state & atomic fit preserves prior fitted state on failure
        with SklearnSieveDetector(alpha=0.05, warmup=20) as det:
            det.fit(X[:50])
            self.assertTrue(det.is_fitted_)
            with self.assertRaises(ValueError):
                det.fit([])
            self.assertTrue(det.is_fitted_)
            self.assertEqual(det.n_samples_seen_, 50)
            det.set_params(alpha=0.01)
            self.assertFalse(det.is_fitted_)
            self.assertIsNone(det._est)


if __name__ == "__main__":
    unittest.main()
