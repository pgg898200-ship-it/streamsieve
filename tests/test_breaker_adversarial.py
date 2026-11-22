"""Independent Adversarial Breaker Audit suite for streamsieve.

Attacks all 7 adversarial surfaces under strict red-team breaker discipline:
  1. Extreme Numerical & Non-SIMD-Aligned Torture (EStream)
  2. Adversarial Continuous-Peeking Null Calibration Torture (6 nasty distributions)
  3. Exhaustive CRDT Algebraic Axiom Fuzzing (EvidenceLattice, 300 trials x 2 modes)
  4. Strict Zero-Lookahead Causality Torture (assert_causal on all 4 engines)
  5. Byzantine Poisoning, Overconfident Bloviator & Sudden Regime/Trust Flip (TrustCalibrator)
  6. MCP Server & Native CLI Fuzzing / Security Torture
  7. Zero-Runtime-Dependency & Cold-Import Latency Gate

Runnable with BOTH:
  - PYTHONPATH=python python3 -m unittest tests/test_breaker_adversarial.py -v
  - PYTHONPATH=python .bench-venv/bin/python -m pytest tests/test_breaker_adversarial.py -v
"""

from __future__ import annotations

import json
import math
import os
import random
import struct
import subprocess
import sys
import time
import unittest
from typing import Any

import streamsieve
from streamsieve import (
    CausalityViolationError,
    EStream,
    EvidenceLattice,
    StreamSieveError,
    TrustCalibrator,
    VOISifter,
    assert_causal,
    verify_estream_causality,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
NATIVE_CLI = os.path.join(REPO_ROOT, "build", "streamsieve")


def _repo_python_env() -> dict[str, str]:
    env = dict(os.environ)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(streamsieve.__file__)))
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_parent}{os.pathsep}{existing}" if existing else pkg_parent
    return env


def _sample_student_t(rng: random.Random, df: float) -> float:
    z = rng.gauss(0.0, 1.0)
    chi2 = rng.gammavariate(df * 0.5, 2.0)
    return z / math.sqrt(chi2 / df)


def _strict_json_loads(text: str) -> Any:
    """RFC-8259 strict JSON parser rejecting non-standard NaN/Infinity tokens."""

    def _reject_const(token: str) -> Any:
        raise ValueError(f"Non-RFC-8259 JSON constant emitted: {token!r}")

    return json.loads(text, parse_constant=_reject_const)


# =============================================================================
# Surface 1: Extreme Numerical & Non-SIMD-Aligned Torture (EStream)
# =============================================================================


class TestSurface1EStreamNumericalAndSIMDTorture(unittest.TestCase):
    """Hammer AVX2 lanes AND scalar tail lanes across non-multiple-of-4 channels."""

    NON_SIMD_COUNTS = (1, 3, 5, 7, 13, 17)

    def test_non_simd_aligned_channels_parity_batch_row_and_one(self) -> None:
        """Bit-exact parity between update_batch, update_row, and update_one across extreme values."""
        for n_ch in self.NON_SIMD_COUNTS:
            with self.subTest(n_ch=n_ch):
                rng = random.Random(1000 + n_ch)
                n_ticks = 140
                mat: list[list[float]] = []
                for t in range(n_ticks):
                    row: list[float] = []
                    for c in range(n_ch):
                        kind = c % 5
                        if kind == 0:
                            # 1e10 DC offset + N(0, 1) cancellation torture
                            val = 1e10 + rng.gauss(0.0, 1.0)
                        elif kind == 1:
                            # Constant-zero channel forever
                            val = 0.0
                        elif kind == 2:
                            # Subnormal numbers forever
                            val = 1e-308
                        elif kind == 3:
                            # 1e15 spike injected during warmup (t=10) AND after warmup (t=85)
                            val = 1e15 if t in (10, 85) else rng.gauss(0.0, 1.0)
                        else:
                            # Stationary then moderate shift
                            val = rng.gauss(0.0, 1.0) + (2.0 if t >= 65 else 0.0)
                        row.append(val)
                    mat.append(row)

                with (
                    EStream(n_ch, warmup=32, ring_capacity=64) as e_batch,
                    EStream(n_ch, warmup=32, ring_capacity=64) as e_row,
                    EStream(n_ch, warmup=32, ring_capacity=64) as e_one,
                ):
                    e_batch.update_batch(mat)
                    for row in mat:
                        e_row.update_row(row)
                        for c, v in enumerate(row):
                            e_one.update_one(c, v)

                    st_batch = [e_batch.channel_status(c) for c in range(n_ch)]
                    st_row = [e_row.channel_status(c) for c in range(n_ch)]
                    st_one = [e_one.channel_status(c) for c in range(n_ch)]

                    self.assertEqual(st_batch, st_row, f"batch != row for n_ch={n_ch}")
                    self.assertEqual(st_row, st_one, f"row != one for n_ch={n_ch}")

                    for c in range(n_ch):
                        s = st_batch[c]
                        self.assertTrue(math.isfinite(s.e_value), f"Non-finite e_value on ch={c}: {s}")
                        self.assertTrue(math.isfinite(s.log_e_mean), f"Non-finite log_e_mean on ch={c}: {s}")
                        self.assertTrue(math.isfinite(s.log_e_burst), f"Non-finite log_e_burst on ch={c}: {s}")
                        self.assertTrue(math.isfinite(s.z_score), f"Non-finite z_score on ch={c}: {s}")
                        self.assertTrue(math.isfinite(s.baseline_loc), f"Non-finite loc on ch={c}: {s}")
                        self.assertTrue(math.isfinite(s.baseline_scale) and s.baseline_scale > 0.0)
                        self.assertEqual(e_batch.fetch_raw(c), e_row.fetch_raw(c))
                        self.assertEqual(e_row.fetch_raw(c), e_one.fetch_raw(c))

    def test_nan_inf_scattered_lanes_and_scalar_tail_sync_post_desync(self) -> None:
        """NaN/+Inf/-Inf in lane 0, lane 2, and scalar tail (including exact checkpoint ticks)."""
        for n_ch in self.NON_SIMD_COUNTS:
            for desync_tick in (5, 32, 63, 64, 90):
                with self.subTest(n_ch=n_ch, desync_tick=desync_tick):
                    rng = random.Random(2000 + n_ch * 100 + desync_tick)
                    n_ticks = 135
                    mat = [[rng.gauss(0.0, 1.0) for _ in range(n_ch)] for _ in range(n_ticks)]

                    # Scatter NaN, +Inf, -Inf across lane 0, lane 2 (if present), and scalar tail
                    mat[desync_tick][0] = math.nan
                    if n_ch > 2:
                        mat[desync_tick + 1][2] = math.inf
                    mat[desync_tick + 2][n_ch - 1] = -math.inf
                    if n_ch > 6:
                        mat[desync_tick][6] = math.nan

                    with (
                        EStream(n_ch, warmup=32, ring_capacity=64) as e_batch,
                        EStream(n_ch, warmup=32, ring_capacity=64) as e_row,
                        EStream(n_ch, warmup=32, ring_capacity=64) as e_one,
                    ):
                        e_batch.update_batch(mat)
                        for row in mat:
                            e_row.update_row(row)
                            for c, v in enumerate(row):
                                e_one.update_one(c, v)

                        for c in range(n_ch):
                            sb = e_batch.channel_status(c)
                            sr = e_row.channel_status(c)
                            so = e_one.channel_status(c)

                            # Batch and row MUST match bit-for-bit across all fields
                            self.assertEqual(sb, sr)

                            # All numeric outputs MUST be finite (zero NaN/Inf leakage)
                            self.assertTrue(math.isfinite(sb.e_value) and sb.e_value > 0.0, f"Bad e_value: {sb}")
                            self.assertTrue(math.isfinite(sb.log_e_mean), f"Bad log_e_mean: {sb}")
                            self.assertTrue(math.isfinite(sb.log_e_burst), f"Bad log_e_burst: {sb}")
                            self.assertTrue(math.isfinite(sb.z_score), f"Bad z_score: {sb}")

                            # Statistical outputs MUST match pure scalar update_one bit-for-bit
                            # (proving sync_post_ NaN desync materialization is exact!)
                            self.assertEqual(sr.e_value, so.e_value)
                            self.assertEqual(sr.log_e_mean, so.log_e_mean)
                            self.assertEqual(sr.log_e_burst, so.log_e_burst)
                            self.assertEqual(sr.z_score, so.z_score)
                            self.assertEqual(sr.baseline_loc, so.baseline_loc)
                            self.assertEqual(sr.baseline_scale, so.baseline_scale)

    def test_witness_finite_extreme_spike_on_constant_zero_channel_no_nan_poison(self) -> None:
        """WITNESS #1 (EStream): Finite extreme value on a constant-zero channel must NOT poison e_value to NaN.

        On a constant-zero channel, warmup sets scale=1e-12 (inv_scale=1e12).
        A subsequent finite value x=1e300 overflows z=(x-loc)*inv_scale to +Inf,
        making bounded_squash(z) = Inf/sqrt(1+Inf^2) = Inf/Inf = NaN and permanently
        poisoning sum_u_, sum_u2_, and combined_e_ to NaN for all future ticks.
        """
        n_ch = 7  # Hits both AVX2 lane 0 and scalar tail lane 6
        with EStream(n_ch, warmup=32) as est:
            for _ in range(40):
                est.update_row([0.0] * n_ch)
            # Inject finite 1e300 into AVX2 lane 0 and scalar tail lane 6
            spike_row = [0.0] * n_ch
            spike_row[0] = 1e300
            spike_row[6] = -1e300
            est.update_row(spike_row)
            for _ in range(40):
                est.update_row([0.0] * n_ch)

            for ch in (0, 6):
                st = est.channel_status(ch)
                self.assertTrue(
                    math.isfinite(st.e_value) and math.isfinite(st.log_e_mean),
                    f"WITNESS #1: finite +/-1e300 spike on constant-zero ch={ch} permanently poisoned "
                    f"EStream to NaN (e_value={st.e_value}, log_e_mean={st.log_e_mean})",
                )

    def test_witness_tick0_warmup_spike_anchor_cancellation(self) -> None:
        """WITNESS #2 (EStream): A large spike on tick 0 must NOT trigger catastrophic cancellation in warmup.

        finalize_warmup() anchors all warmup samples against x[0] (anchor_[c] = buf[0]).
        If a 1e17 spike lands on tick 0 while ticks 1..31 are N(0, 1), subtracting 1e17
        wipes out all 53 mantissa bits of every normal sample, collapsing scale_ to 1e-12.
        """
        rng = random.Random(42)
        base = [rng.gauss(0.0, 1.0) for _ in range(100)]
        with EStream(2, warmup=32) as est:
            for t in range(100):
                v_tick0_spike = 1e17 if t == 0 else base[t]
                v_tick5_spike = 1e17 if t == 5 else base[t]
                est.update_row([v_tick0_spike, v_tick5_spike])
            s0 = est.channel_status(0)
            s1 = est.channel_status(1)
            self.assertAlmostEqual(
                s0.baseline_scale,
                s1.baseline_scale,
                delta=0.25,
                msg=(
                    f"WITNESS #2: 1e17 spike at tick 0 collapsed warmup scale via first-sample "
                    f"anchor cancellation: tick0_scale={s0.baseline_scale:.3e} vs "
                    f"tick5_scale={s1.baseline_scale:.6f}"
                ),
            )
            self.assertAlmostEqual(
                s0.baseline_loc,
                s1.baseline_loc,
                delta=0.05,
                msg=(
                    f"WITNESS #2: 1e17 spike at tick 0 corrupted baseline_loc: "
                    f"tick0_loc={s0.baseline_loc} vs tick5_loc={s1.baseline_loc}"
                ),
            )

    def test_witness_channel_status_rejected_ebh_matches_ebh_reject_and_topk(self) -> None:
        """WITNESS #3 (EStream): channel_status(c).rejected_ebh must agree with ebh_reject() and topk().

        EStreamEngine::get_channel() currently checks Bonferroni (e >= M/alpha) instead of
        exact e-BH (e >= M/(k*alpha)), so whenever k>=2 channels are rejected with e in
        [M/(k*alpha), M/alpha), channel_status(c).rejected_ebh falsely returns False.
        """
        rng = random.Random(42)
        with EStream(20, warmup=16, alpha=0.05) as est:
            for t in range(70):
                row = [rng.gauss(0.0, 1.0) for _ in range(20)]
                if t >= 40:
                    for c in range(6):
                        row[c] += 1.2
                est.update_row(row)
                rej = set(est.ebh_reject())
                if len(rej) >= 2:
                    top_map = {s.channel: s.rejected_ebh for s in est.topk(20)}
                    for c in range(20):
                        st = est.channel_status(c)
                        self.assertEqual(
                            top_map[c],
                            c in rej,
                            f"topk({c}).rejected_ebh disagreed with ebh_reject() at t={t}",
                        )
                        self.assertEqual(
                            st.rejected_ebh,
                            c in rej,
                            f"WITNESS #3: channel_status({c}).rejected_ebh={st.rejected_ebh} "
                            f"disagrees with ebh_reject()={sorted(rej)} (e={st.e_value:.2f}) at t={t}",
                        )
                    return
        self.fail("Failed to construct >=2 e-BH rejection scenario for consistency check")


# =============================================================================
# Surface 2: Adversarial Continuous-Peeking Null Calibration Torture
# =============================================================================


class TestSurface2ContinuousPeekingNullCalibration(unittest.TestCase):
    """300 trials x T=500 continuous peeking (every 5th tick) across 6 nasty stationary nulls."""

    N_TRIALS = 300
    N_TICKS = 500
    PEEK_EVERY = 5
    ALPHA = 0.05
    MAX_ALLOWED_FAR = 0.08  # Binomial allowance above 0.05 for N=300 trials

    def test_continuous_peeking_null_calibration_6_distributions(self) -> None:
        distributions = [
            ("Gaussian", 101, lambda r: r.gauss(0.0, 1.0)),
            ("Student-t(2.5)", 202, lambda r: _sample_student_t(r, 2.5)),
            ("LogNormal(0,1.1)", 303, lambda r: math.exp(r.gauss(0.0, 1.1))),
            ("Bimodal", 404, lambda r: (2.0 if r.random() < 0.5 else -2.0) + 0.2 * r.gauss(0.0, 1.0)),
            ("SparseBernoulli(0.05)", 505, lambda r: 1.0 if r.random() < 0.05 else 0.0),
            ("10000xLoud+1e10DC", 606, lambda r: 1e10 + 10000.0 * r.gauss(0.0, 1.0)),
        ]

        thresh = 1.0 / self.ALPHA
        far_report: dict[str, float] = {}

        for dist_name, seed, sampler in distributions:
            with self.subTest(distribution=dist_name):
                rng = random.Random(seed)
                ever_rejected = [False] * self.N_TRIALS
                ebh_ever_rejected = False
                max_e = 0.0

                # Per-channel e-values are 100% independent across channels inside EStream(N_TRIALS),
                # so running 300 parallel null channels simultaneously checks both exact single-channel
                # anytime-valid calibration AND multi-channel e-BH global-null FWER!
                with EStream(self.N_TRIALS, warmup=32, ring_capacity=0, alpha=self.ALPHA) as est:
                    for tick_block in range(0, self.N_TICKS, self.PEEK_EVERY):
                        batch = [
                            [sampler(rng) for _ in range(self.N_TRIALS)]
                            for _ in range(self.PEEK_EVERY)
                        ]
                        est.update_batch(batch)
                        for c in range(self.N_TRIALS):
                            ev = est.channel_status(c).e_value
                            self.assertTrue(
                                math.isfinite(ev),
                                f"Non-finite e-value on {dist_name} ch={c} at t={tick_block + self.PEEK_EVERY}",
                            )
                            if ev > max_e:
                                max_e = ev
                            if ev >= thresh:
                                ever_rejected[c] = True
                        if est.ebh_reject(self.ALPHA):
                            ebh_ever_rejected = True

                far = sum(ever_rejected) / self.N_TRIALS
                far_report[dist_name] = far
                self.assertLessEqual(
                    far,
                    self.MAX_ALLOWED_FAR,
                    f"Null calibration violated on {dist_name}: empirical FAR={far:.4f} "
                    f"({sum(ever_rejected)}/{self.N_TRIALS}) > {self.MAX_ALLOWED_FAR} "
                    f"(max_e={max_e:.2f}, ebh_hit={ebh_ever_rejected})",
                )


# =============================================================================
# Surface 3: Exhaustive CRDT Algebraic Axiom Fuzzing (EvidenceLattice)
# =============================================================================


class TestSurface3CRDTEvidenceLatticeFuzzing(unittest.TestCase):
    """300 randomized multi-agent fuzz trials across both CRDT merge modes + echo storm."""

    @staticmethod
    def _summary_key(lat: EvidenceLattice, hyp: str | int) -> tuple[Any, ...]:
        s = lat.query(hyp)
        return (
            s.hyp_id,
            s.total_log_odds,
            s.posterior_prob,
            s.merged_e_value,
            s.merged_log_e,
            s.unique_evidence,
            s.distinct_agents,
        )

    def test_300_trial_crdt_commutativity_associativity_idempotence_and_wire(self) -> None:
        for mode in ("strict_origin", "max_reliability"):
            with self.subTest(merge_mode=mode):
                rng = random.Random(3100 if mode == "strict_origin" else 3200)
                hyps: list[str | int] = ["hyp_alpha", "hyp_beta", 42]
                provs: list[str | int] = ["p0", "p1", "p2", "p3", "p4", 9001]

                for trial in range(300):
                    # Build 4 independent agent replicas with conflicting timestamps,
                    # identical provenance IDs with differing LLRs, conflicting agent weights & priors
                    replicas = [EvidenceLattice(mode) for _ in range(4)]
                    try:
                        for rep in replicas:
                            for h in hyps:
                                if rng.random() < 0.55:
                                    rep.set_prior(h, rng.choice([-1.5, -0.5, 0.0, 0.5, 1.25]))
                            for aid in range(5):
                                if rng.random() < 0.60:
                                    rep.set_agent_weight(aid, rng.choice([0.25, 0.5, 1.0, 1.5, 2.0]))
                            n_obs = rng.randint(2, 10)
                            for _ in range(n_obs):
                                rep.observe(
                                    rng.choice(hyps),
                                    rng.choice(provs),
                                    llr=rng.uniform(-3.0, 3.0),
                                    log_e=rng.uniform(-2.0, 4.0),
                                    agent_id=rng.randint(0, 4),
                                    reliability=rng.choice([0.1, 0.5, 0.5, 0.85, 1.0]),
                                    timestamp_ns=rng.choice([100, 100, 200, 300, 300]),
                                )

                        a, b, c, d = replicas

                        # 1. Commutativity: merge(A, B) == merge(B, A) (exact bit-for-bit)
                        ab = a.merged(b)
                        ba = b.merged(a)
                        # 2. Associativity: merge(merge(A, B), C) == merge(A, merge(B, C))
                        ab_c = ab.merged(c)
                        bc = b.merged(c)
                        a_bc = a.merged(bc)
                        # 3. Random merge tree topology: ((A U B) U (C U D)) vs shuffled fold
                        cd = c.merged(d)
                        balanced = ab.merged(cd)
                        order = [a, b, c, d]
                        rng.shuffle(order)
                        shuffled = EvidenceLattice.from_bytes(order[0].to_bytes())
                        for nxt in order[1:]:
                            shuffled.merge(nxt)
                        # 4. Repeated self-merges & idempotence
                        idem = EvidenceLattice.from_bytes(balanced.to_bytes())
                        idem.merge(idem)
                        idem.merge(a)
                        idem.merge(b)
                        idem.merge(cd)
                        # 5. Binary to_bytes/from_bytes & Base85 to_base85/from_base85 roundtrips
                        rt_bin = EvidenceLattice.from_bytes(balanced.to_bytes())
                        rt_b85 = EvidenceLattice.from_base85(balanced.to_base85())

                        try:
                            self.assertEqual(ab.to_bytes(), ba.to_bytes(), f"Commutativity wire diff trial={trial}")
                            self.assertEqual(ab_c.to_bytes(), a_bc.to_bytes(), f"Associativity wire diff trial={trial}")
                            self.assertEqual(
                                balanced.to_bytes(), shuffled.to_bytes(), f"Topology wire diff trial={trial}"
                            )
                            self.assertEqual(
                                balanced.to_bytes(), idem.to_bytes(), f"Idempotence wire diff trial={trial}"
                            )
                            for h in hyps:
                                self.assertEqual(self._summary_key(ab, h), self._summary_key(ba, h))
                                self.assertEqual(self._summary_key(ab_c, h), self._summary_key(a_bc, h))
                                k_bal = self._summary_key(balanced, h)
                                self.assertEqual(k_bal, self._summary_key(shuffled, h))
                                self.assertEqual(k_bal, self._summary_key(idem, h))
                                self.assertEqual(k_bal, self._summary_key(rt_bin, h))
                                self.assertEqual(k_bal, self._summary_key(rt_b85, h))
                        finally:
                            for tmp in (ab, ba, ab_c, bc, a_bc, cd, balanced, shuffled, idem, rt_bin, rt_b85):
                                tmp.close()
                    finally:
                        for rep in replicas:
                            rep.close()

    def test_echo_storm_500x_across_50_agents_zero_ulp_drift(self) -> None:
        """1 finding echoed 500 times across 50 agents must cause 0.0 ULP change."""
        with EvidenceLattice("strict_origin") as lat:
            lat.set_prior("incident_42", -0.4)
            self.assertFalse(
                lat.observe(
                    "incident_42",
                    "sha256:root_finding",
                    llr=2.25,
                    log_e=1.95,
                    agent_id=0,
                    reliability=0.8,
                    timestamp_ns=1000,
                )
            )
            baseline = lat.query("incident_42")

            for k in range(500):
                echo = lat.observe(
                    "incident_42",
                    "sha256:root_finding",
                    llr=2.25 + 0.01 * k,
                    log_e=1.95 + 0.02 * k,
                    agent_id=(k % 50) + 1,
                    reliability=0.99,
                    timestamp_ns=2000 + k,
                )
                self.assertTrue(echo)

            # Also gossip-merge 50 agent copies back into lat
            for a in range(50):
                with EvidenceLattice("strict_origin") as peer:
                    peer.observe(
                        "incident_42",
                        "sha256:root_finding",
                        llr=9.99,
                        log_e=9.99,
                        agent_id=a + 1,
                        reliability=1.0,
                        timestamp_ns=5000 + a,
                    )
                    lat.merge(peer)

            after = lat.query("incident_42")
            self.assertEqual(after.total_log_odds, baseline.total_log_odds)
            self.assertEqual(after.posterior_prob, baseline.posterior_prob)
            self.assertEqual(after.merged_e_value, baseline.merged_e_value)
            self.assertEqual(after.unique_evidence, 1)
            self.assertEqual(after.echoes_suppressed, 550)

    def test_witness_zero_weight_agent_excluded_from_merged_e_value(self) -> None:
        """WITNESS #4 (EvidenceLattice): An agent with weight=0.0 must NOT be promoted to weight=1.0 in merged_e_value.

        EvidenceLattice::query() multiplies LLR by weight=0.0 (correctly excluding the agent),
        but lines 1089 & 1095 use `(wit != end && wit->second > 0.0) ? wit->second : 1.0`,
        promoting explicitly zero-weighted (quarantined) agents back to full weight 1.0!
        """
        with EvidenceLattice("strict_origin") as lat:
            lat.set_agent_weight(0, 1.0)
            lat.set_agent_weight(1, 0.0)  # Explicitly zero-weighted / quarantined agent
            lat.observe("H", "p0", llr=1.0, log_e=math.log(2.0), agent_id=0, timestamp_ns=10)
            lat.observe("H", "p1", llr=10.0, log_e=math.log(1000.0), agent_id=1, timestamp_ns=20)

            s = lat.query("H")
            self.assertAlmostEqual(s.total_log_odds, 1.0, places=12)
            self.assertAlmostEqual(
                s.merged_e_value,
                2.0,
                places=9,
                msg=(
                    f"WITNESS #4: agent_id=1 with weight=0.0 was promoted to weight=1.0 in "
                    f"Vovk-Wang merged_e_value: got {s.merged_e_value:.4f}, expected 2.0"
                ),
            )

    def test_witness_corrupted_wire_nan_rejected_by_deserialize(self) -> None:
        """WITNESS #5 (EvidenceLattice): Wire deserialize() must reject non-finite NaN/Inf floats.

        observe() and set_prior() reject NaN/Inf, but EvidenceLattice::deserialize() reads raw
        IEEE-754 doubles without checking std::isfinite(), allowing untrusted wire bytes to
        inject NaN posteriors and violate entry_beats() strict weak ordering.
        """
        with EvidenceLattice("strict_origin") as lat:
            lat.observe("H", "p0", llr=1.0, log_e=1.0, agent_id=0, timestamp_ns=10)
            raw = bytearray(lat.to_bytes())

        # Patch llr at byte offset 49 (header 16 + n_aw 4 + hid 8 + prior 8 + has_p 1 + nev 4 + pid 8)
        struct.pack_into("<d", raw, 49, float("nan"))
        with self.assertRaises(
            StreamSieveError,
            msg="WITNESS #5: EvidenceLattice.from_bytes() accepted NaN llr in wire payload without error!",
        ):
            with EvidenceLattice.from_bytes(bytes(raw)) as bad:
                _ = bad.query("H")


# =============================================================================
# Surface 4: Strict Zero-Lookahead Causality Torture (ALL 4 Engines)
# =============================================================================


class TestSurface4ZeroLookaheadCausalityTorture(unittest.TestCase):
    """Future-poisoning (+1e12, -1e12, NaN, +Inf) bit-exact 0.0 diff across all 4 engines."""

    def test_estream_n_ch_7_strict_causality(self) -> None:
        rng = random.Random(4001)
        mat = [
            [rng.gauss(0.0, 1.0) + (1.8 if (t >= 45 and c in (0, 6)) else 0.0) for c in range(7)]
            for t in range(95)
        ]
        self.assertTrue(
            verify_estream_causality(
                mat,
                probe_ticks=[0, 15, 31, 32, 44, 45, 63, 64, 93],
                warmup=32,
                ring_capacity=64,
            )
        )

    def test_evidence_lattice_strict_causality(self) -> None:
        rng = random.Random(4002)
        stream = [[rng.uniform(-2.5, 2.5), rng.uniform(-1.5, 3.5)] for _ in range(45)]

        for mode in ("strict_origin", "max_reliability"):
            with self.subTest(merge_mode=mode):

                def _run_lattice(seq: list[list[float]]) -> list[Any]:
                    out = []
                    with EvidenceLattice(mode) as lat:
                        lat.set_prior("H", -0.25)
                        for t, row in enumerate(seq):
                            lat.observe(
                                "H",
                                f"prov_{t % 12}",
                                llr=row[0],
                                log_e=row[1],
                                agent_id=t % 4,
                                reliability=0.5 + 0.1 * (t % 5),
                                timestamp_ns=1000 + t,
                            )
                            out.append(lat.query("H"))
                    return out

                self.assertTrue(assert_causal(_run_lattice, stream, probe_ticks=[0, 5, 11, 12, 25, 43]))

    def test_voi_sifter_strict_causality(self) -> None:
        rng = random.Random(4003)
        stream = [[rng.gauss(0.0, 1.0) for _ in range(4)] for _ in range(36)]

        def _run_sifter(seq: list[list[float]]) -> list[Any]:
            out = []
            with VOISifter(8, n_sites=2, ticks_per_epoch=4) as sf:
                for t, row in enumerate(seq):
                    sf.observe_site_agg(t % 2, row[0])
                    sf.observe_mean(t % 8, row[1])
                    sf.observe_ticks((t + 3) % 8, row)
                    if t > 0 and t % 8 == 0:
                        sf.step_epoch(0.95)
                    acts = tuple(sf.allocate(budget_per_agent=2, n_agents=2))
                    posts = tuple(sf.posteriors())
                    out.append((posts, acts))
            return out

        self.assertTrue(assert_causal(_run_sifter, stream, probe_ticks=[0, 4, 8, 16, 25, 34]))

    def test_trust_calibrator_strict_causality(self) -> None:
        rng = random.Random(4004)
        # Binary mode with delayed labels
        prob_stream = [
            [rng.uniform(0.05, 0.95), rng.uniform(0.05, 0.95), 1.0 if rng.random() < 0.5 else 0.0]
            for _ in range(45)
        ]

        def _run_trust_prob(seq: list[list[float]]) -> list[Any]:
            out = []
            with TrustCalibrator(2) as tc:
                for t, row in enumerate(seq):
                    dec = tc.predict_prob(t, [row[0], row[1]])
                    if t >= 3:
                        y_past = seq[t - 3][2]
                        if math.isfinite(y_past):
                            tc.resolve(t - 3, y_past)
                    out.append((dec, tuple(tc.agents())))
            return out

        self.assertTrue(assert_causal(_run_trust_prob, prob_stream, probe_ticks=[0, 2, 3, 10, 25, 43]))

        # Scalar mode with delayed labels
        scalar_stream = [
            [10.0 + rng.gauss(0.0, 1.0), 10.0 + rng.gauss(0.0, 2.0), 10.0 + rng.gauss(0.0, 1.0)]
            for _ in range(45)
        ]

        def _run_trust_scalar(seq: list[list[float]]) -> list[Any]:
            out = []
            with TrustCalibrator(2) as tc:
                for t, row in enumerate(seq):
                    dec = tc.predict_scalar(t, [row[0], row[1]], agent_stds=[1.0, 2.0])
                    if t >= 3:
                        y_past = seq[t - 3][2]
                        if math.isfinite(y_past):
                            tc.resolve(t - 3, y_past)
                    out.append((dec, tuple(tc.agents())))
            return out

        self.assertTrue(assert_causal(_run_trust_scalar, scalar_stream, probe_ticks=[0, 2, 3, 10, 25, 43]))

    def test_assert_causal_detects_deliberate_lookahead(self) -> None:
        """Sanity check: assert_causal must catch a 1-tick lookahead leak."""
        series = [float(i) for i in range(20)]
        with self.assertRaises(CausalityViolationError):
            assert_causal(
                lambda xs: [xs[min(i + 1, len(xs) - 1)] for i in range(len(xs))],
                series,
                probe_ticks=[5],
            )

    def test_witness_clamp_d_nan_config_poisoning_voi_sifter(self) -> None:
        """WITNESS #7 (clamp_d NaN bug): NaN config parameters must NOT poison VOISifter posteriors to NaN.

        clamp_d(NaN, lo, hi) returns NaN because IEEE-754 comparisons against NaN are false.
        VOISifter(4, prior_active=NaN) silently produces [nan, nan, nan, nan] posteriors.
        """
        with VOISifter(4, prior_active=float("nan")) as sf:
            posts = sf.posteriors()
            self.assertTrue(
                all(math.isfinite(p) and 0.0 <= p <= 1.0 for p in posts),
                f"WITNESS #7: VOISifter(prior_active=NaN) poisoned posteriors to {posts} "
                f"(root cause: clamp_d(NaN, lo, hi) returns NaN)",
            )


# =============================================================================
# Surface 5: Byzantine Poisoning, Overconfident Bloviator & Sudden Trust Flip
# =============================================================================


class TestSurface5TrustCalibratorByzantineAndFlipTorture(unittest.TestCase):
    """Test Byzantine 40% (binary & scalar), sudden trust flip + recovery, and delayed labels."""

    def test_byzantine_40pct_scalar_outlier_attack(self) -> None:
        """Test A (Scalar): 3 good agents + 2 +-1e6 outlier Byzantine agents -> quarantine + accurate ensemble."""
        for mode in ("opposite_sign", "same_sign_pos"):
            with self.subTest(attack_mode=mode):
                rng = random.Random(5010 if mode == "opposite_sign" else 5011)
                late_errors: list[float] = []
                with TrustCalibrator(5) as tc:
                    for t in range(80):
                        truth = 10.0 + rng.gauss(0.0, 1.0)
                        means = [truth + rng.gauss(0.0, 0.5) for _ in range(3)]
                        if mode == "opposite_sign":
                            means.extend([1e6, -1e6])
                        else:
                            means.extend([1e6, 1e6])
                        dec = tc.predict_scalar(t, means, agent_stds=[0.5] * 5)
                        self.assertTrue(math.isfinite(dec.fused_point))
                        if t >= 30:
                            late_errors.append(abs(dec.fused_point - truth))
                        tc.resolve(t, truth)

                    ags = tc.agents()
                    for idx in (3, 4):
                        self.assertTrue(ags[idx].quarantined, f"Byzantine scalar agent {idx} not quarantined")
                        self.assertEqual(ags[idx].weight, 0.0, f"Quarantined agent {idx} has non-zero weight")
                    for idx in (0, 1, 2):
                        self.assertFalse(ags[idx].quarantined, f"Good scalar agent {idx} falsely quarantined")
                    mean_late_err = sum(late_errors) / len(late_errors)
                    self.assertLess(mean_late_err, 0.8, f"Ensemble accuracy degraded: MAE={mean_late_err:.3f}")

    def test_witness_byzantine_40pct_binary_and_honest_agent_preservation(self) -> None:
        """Test A (Binary) + WITNESS #6a: 3 good agents (85%) + 2 inverted Byzantine (0.99/0.01).

        Byzantine agents must be quarantined (weight=0.0), ensemble must stay accurate,
        AND honest 85%-accurate agents must NOT be falsely quarantined.
        Currently FAILS because ref_loss=min(weighted_avg, median) + reflected CUSUM max(0, ...)
        drives E[log_bet] > 0 for honest agents whenever 1 of 3 good agents makes a random error!
        """
        rng = random.Random(0)
        correct_late = 0
        with TrustCalibrator(5) as tc:
            for t in range(80):
                y = 1.0 if rng.random() < 0.5 else 0.0
                probs: list[float] = []
                for _ in range(3):
                    ok = rng.random() < 0.85
                    probs.append(0.85 if (ok == (y > 0.5)) else 0.15)
                for _ in range(2):
                    probs.append(0.01 if y > 0.5 else 0.99)
                dec = tc.predict_prob(t, probs)
                if t >= 30 and dec.optimal_action == y:
                    correct_late += 1
                tc.resolve(t, y)

            ags = tc.agents()
            for idx in (3, 4):
                self.assertTrue(ags[idx].quarantined, f"Byzantine binary agent {idx} not quarantined")
                self.assertEqual(ags[idx].weight, 0.0)
            self.assertGreaterEqual(correct_late / 50.0, 0.78)
            falsely_q = [a.agent_id for a in ags[:3] if a.quarantined]
            self.assertEqual(
                falsely_q,
                [],
                f"WITNESS #6a: honest 85%-accurate agents {falsely_q} were falsely quarantined "
                f"in 40% Byzantine binary test: {ags[:3]}",
            )

    def test_witness_honest_agents_zero_byzantine_false_quarantine_and_zero_weight_invariant(self) -> None:
        """WITNESS #6a & #6b: 3 identical honest 85% agents (0 Byzantine) must NOT suffer mass false quarantine,
        and EVERY quarantined agent must ALWAYS satisfy weight == 0.0.

        Currently:
          - 20/20 trials falsely quarantine honest agents (14/20 quarantine ALL 3/3 honest agents!)
          - When the last active agent is quarantined, total_w == 0 skips weight zeroing, leaving
            weight == 1.0 on a quarantined agent (violating C ABI contract `weight = 0.0 if quarantined`).
        """
        trials_with_false_q = 0
        all_quarantined_trials = 0
        weight_invariant_violations: list[tuple[int, int, float]] = []

        for seed in range(20):
            rng = random.Random(seed)
            with TrustCalibrator(3) as tc:
                for t in range(100):
                    y = 1.0 if rng.random() < 0.5 else 0.0
                    probs = []
                    for _ in range(3):
                        ok = rng.random() < 0.85
                        probs.append(0.85 if (ok == (y > 0.5)) else 0.15)
                    tc.predict_prob(t, probs)
                    tc.resolve(t, y)

                ags = tc.agents()
                n_q = sum(1 for a in ags if a.quarantined)
                if n_q > 0:
                    trials_with_false_q += 1
                if n_q == 3:
                    all_quarantined_trials += 1
                for a in ags:
                    if a.quarantined and a.weight != 0.0:
                        weight_invariant_violations.append((seed, a.agent_id, a.weight))

        self.assertEqual(
            weight_invariant_violations,
            [],
            f"WITNESS #6b: quarantined agents retained non-zero weight when all agents quarantined: "
            f"{weight_invariant_violations[:5]}",
        )
        self.assertLessEqual(
            trials_with_false_q,
            4,
            f"WITNESS #6a: {trials_with_false_q}/20 trials falsely quarantined honest identical 85% "
            f"agents ({all_quarantined_trials}/20 quarantined ALL 3/3 honest agents!) — "
            f"root cause: ref_loss=min(mean, median) + reflected CUSUM max(0, ...) breaks Ville martingale",
        )

    def test_sudden_trust_flip_and_shadow_recovery_2_agents_clean(self) -> None:
        """Test B (2 agents, clean 90%/10% flip): Agent 1 quarantined in Phase 1, then swapped at tick 50
        -> Agent 0 quarantined AND Agent 1 recovered/un-quarantined in Phase 2."""
        rng = random.Random(42)
        with TrustCalibrator(2) as tc:
            # Phase 1 (50 ticks): Agent 0 = 90% accurate, Agent 1 = 10% accurate
            for t in range(50):
                y = 1.0 if rng.random() < 0.5 else 0.0
                p0 = (0.90 if rng.random() < 0.90 else 0.10) if y > 0.5 else (0.10 if rng.random() < 0.90 else 0.90)
                p1 = (0.90 if rng.random() < 0.10 else 0.10) if y > 0.5 else (0.10 if rng.random() < 0.10 else 0.90)
                tc.predict_prob(t, [p0, p1])
                tc.resolve(t, y)

            mid = tc.agents()
            self.assertFalse(mid[0].quarantined, f"Phase 1: good Agent 0 quarantined: {mid}")
            self.assertTrue(mid[1].quarantined, f"Phase 1: bad Agent 1 NOT quarantined: {mid}")
            self.assertEqual(mid[1].weight, 0.0)

            # Phase 2 (80 ticks): SWAP ROLES! Agent 0 = 10% accurate, Agent 1 = 90% accurate
            for t in range(50, 130):
                y = 1.0 if rng.random() < 0.5 else 0.0
                p0 = (0.90 if rng.random() < 0.10 else 0.10) if y > 0.5 else (0.10 if rng.random() < 0.10 else 0.90)
                p1 = (0.90 if rng.random() < 0.90 else 0.10) if y > 0.5 else (0.10 if rng.random() < 0.90 else 0.90)
                tc.predict_prob(t, [p0, p1])
                tc.resolve(t, y)

            end = tc.agents()
            self.assertTrue(end[0].quarantined, f"Phase 2: newly-bad Agent 0 NOT quarantined: {end}")
            self.assertEqual(end[0].weight, 0.0)
            self.assertFalse(end[1].quarantined, f"Phase 2: newly-good Agent 1 FAILED shadow recovery: {end}")
            self.assertGreater(end[1].weight, 0.8)

    def test_witness_sudden_trust_flip_shadow_recovery_with_bystander_agent(self) -> None:
        """Test B + WITNESS #6c: 3-agent swap (Agent 0 & Agent 1 swap 90% <-> 10% alongside 70% Agent 2).

        After Agent 1 is quarantined in Phase 1 and then becomes the BEST agent (90% accurate vs
        Agent 2's 70%) in Phase 2 for 80 ticks, Agent 1 MUST recover from quarantine!
        Currently FAILS 10/10 seeds: Agent 1 remains permanently stuck quarantined (weight=0.0)
        even after its Platt slope recovers and its accuracy exceeds Agent 2's!
        """
        rng = random.Random(100)
        with TrustCalibrator(3) as tc:
            for t in range(50):
                y = 1.0 if rng.random() < 0.5 else 0.0
                p0 = 0.90 if y > 0.5 else 0.10
                p1 = 0.10 if y > 0.5 else 0.90
                p2 = 0.70 if y > 0.5 else 0.30
                tc.predict_prob(t, [p0, p1, p2])
                tc.resolve(t, y)

            mid = tc.agents()
            self.assertTrue(mid[1].quarantined, f"Phase 1: bad Agent 1 not quarantined: {mid}")

            for t in range(50, 130):
                y = 1.0 if rng.random() < 0.5 else 0.0
                p0 = 0.10 if y > 0.5 else 0.90
                p1 = 0.90 if y > 0.5 else 0.10
                p2 = 0.70 if y > 0.5 else 0.30
                tc.predict_prob(t, [p0, p1, p2])
                tc.resolve(t, y)

            end = tc.agents()
            self.assertTrue(end[0].quarantined, f"Phase 2: newly-bad Agent 0 not quarantined: {end}")
            self.assertFalse(
                end[1].quarantined,
                f"WITNESS #6c: newly-90%-accurate Agent 1 remained STUCK in quarantine after 80 "
                f"ticks of superior performance alongside 70% Agent 2: {end}",
            )
            self.assertGreater(end[1].weight, 0.0)

    def test_out_of_order_and_20pct_missing_delayed_labels_bounded_memory(self) -> None:
        """Test C: Reverse/random delayed resolution, 20% missing forever, and >65536 pending cap."""
        rng = random.Random(5050)
        with TrustCalibrator(3) as tc:
            # Part 1: Out-of-order resolution (reverse + random shuffle) with 20% never resolved
            pending: list[tuple[int, float]] = []
            for t in range(400):
                y = 1.0 if rng.random() < 0.5 else 0.0
                tc.predict_prob(t, [0.85 if y else 0.15, 0.65 if y else 0.35, 0.5])
                if t % 5 != 0:  # Leave 20% unresolved forever
                    pending.append((t, y))
                if len(pending) >= 32:
                    if (t // 32) % 2 == 0:
                        batch = list(reversed(pending))
                    else:
                        batch = list(pending)
                        rng.shuffle(batch)
                    pending.clear()
                    for did, lab in batch:
                        tc.resolve(did, lab)
            for did, lab in reversed(pending):
                tc.resolve(did, lab)

            ags = tc.agents()
            self.assertEqual(ags[0].n_resolved, 320)

            # Part 2: Push >65,536 unresolved decisions to verify bounded pending_ eviction
            base_id = 10_000
            for offset in range(66_000):
                tc.predict_prob(base_id + offset, [0.7, 0.6, 0.5])

            # Oldest unresolved decision (id=0) MUST have been evicted by trim_pending() (cap 65,536)
            with self.assertRaises(StreamSieveError):
                tc.resolve(0, 1.0)
            # Most recent decision MUST still be resolvable
            tc.resolve(base_id + 65_999, 1.0)


# =============================================================================
# Surface 6: MCP Server & Native CLI Fuzzing / Security Torture
# =============================================================================


class TestSurface6MCPServerAndCLISecurityTorture(unittest.TestCase):
    """Fuzz python3 -m streamsieve.mcp_server and native ./build/streamsieve CLI."""

    def _run_mcp_session(self, raw_payload: bytes) -> tuple[int, str, str]:
        proc = subprocess.Popen(
            [sys.executable, "-m", "streamsieve.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_repo_python_env(),
        )
        out, err = proc.communicate(raw_payload, timeout=10)
        return (
            proc.returncode,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )

    def test_mcp_server_clean_errors_and_survival_standard_fuzz(self) -> None:
        """Malformed JSON, unknown method, invalid tool, missing params, n_channels<=0, OOB handle,
        corrupt base85, and NaN/Inf in stream rows must return clean JSON-RPC errors and survive."""
        requests = [
            "{not valid json at all",
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "nonexistent/method"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "lattice_observe", "arguments": {}}}),
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "voi_allocate", "arguments": {"n_channels": 0}}}),
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "voi_allocate", "arguments": {"n_channels": -5}}}),
            json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "sift_stream", "arguments": {"session_id": "s1", "rows": [[1.0, 2.0], [1.1, 2.1]]}}}),
            json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "fetch_raw_ticks", "arguments": {"session_id": "s1", "handle": "ch:999@t=0..10"}}}),
            json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "lattice_merge", "arguments": {"source_base85": "!!!corrupted-b85!!!"}}}),
            '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"sift_stream","arguments":{"session_id":"s2","rows":[[NaN,Infinity,-Infinity],[1.0,2.0,3.0]]}}}',
            # Final survival check: valid ping + valid sift_stream after all attacks
            json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0", "id": 100, "method": "tools/call", "params": {"name": "sift_stream", "arguments": {"session_id": "alive", "rows": [[0.1, 0.2], [0.3, 0.4]]}}}),
        ]

        rc, out, err = self._run_mcp_session(("\n".join(requests) + "\n").encode("utf-8"))
        self.assertEqual(rc, 0, f"MCP server exited with rc={rc}, stderr={err}")
        self.assertEqual(err.strip(), "", f"Unexpected stderr/traceback from MCP server: {err}")

        responses = [_strict_json_loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(len(responses), len(requests))

        # Check specific error semantics
        self.assertEqual(responses[0]["error"]["code"], -32700)  # Parse error
        self.assertEqual(responses[1]["error"]["code"], -32601)  # Method not found
        self.assertEqual(responses[2]["error"]["code"], -32602)  # Unknown tool
        for idx in (3, 4, 5, 7, 8):
            self.assertTrue(responses[idx]["result"]["isError"], f"Request idx={idx} should return isError=True")
        # NaN/Inf row request must succeed and emit strict RFC-8259 JSON (verified by _strict_json_loads)
        self.assertFalse(responses[9]["result"]["isError"])
        # Survival checks
        self.assertEqual(responses[10]["id"], 99)
        self.assertEqual(responses[10]["result"], {})
        self.assertEqual(responses[11]["id"], 100)
        self.assertFalse(responses[11]["result"]["isError"])

    def test_witness_mcp_server_survives_adversarial_json_edge_cases(self) -> None:
        """WITNESS #8 (MCPServer): Adversarial inputs must return clean JSON-RPC errors, NOT crash with tracebacks!

        Currently ALL 5 of these kill python3 -m streamsieve.mcp_server with rc=1 and unhandled tracebacks:
          1. voi_allocate n_channels=Infinity          -> OverflowError
          2. sift_stream warmup=Infinity               -> OverflowError
          3. sift_stream rows=[[10**400, 1.0]]         -> OverflowError
          4. voi_allocate observations=["not_a_dict"]  -> AttributeError
          5. Content-Length: -5                        -> ValueError in inp.read(-5)
        """
        ping = json.dumps({"jsonrpc": "2.0", "id": 777, "method": "ping"})
        crash_vectors: list[tuple[str, bytes]] = [
            (
                "voi_n_channels_Infinity",
                (
                    '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"voi_allocate","arguments":{"n_channels":Infinity}}}\n'
                    + ping
                    + "\n"
                ).encode("utf-8"),
            ),
            (
                "sift_warmup_Infinity",
                (
                    '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"sift_stream","arguments":{"rows":[[1.0,2.0]],"warmup":Infinity}}}\n'
                    + ping
                    + "\n"
                ).encode("utf-8"),
            ),
            (
                "sift_rows_10_pow_400",
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "sift_stream", "arguments": {"rows": [[10**400, 1.0]]}},
                        }
                    )
                    + "\n"
                    + ping
                    + "\n"
                ).encode("utf-8"),
            ),
            (
                "voi_observations_non_dict",
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "voi_allocate",
                                "arguments": {"n_channels": 4, "observations": ["not_a_dict"]},
                            },
                        }
                    )
                    + "\n"
                    + ping
                    + "\n"
                ).encode("utf-8"),
            ),
            (
                "negative_content_length",
                b"Content-Length: -5\r\n\r\n"
                + f"Content-Length: {len(ping)}\r\n\r\n".encode("ascii")
                + ping.encode("utf-8"),
            ),
        ]

        crashes: list[str] = []
        for label, payload in crash_vectors:
            rc, out, err = self._run_mcp_session(payload)
            survived = '"id":777' in out
            if rc != 0 or err.strip() or not survived:
                tail = err.strip().splitlines()[-1] if err.strip() else "no stderr"
                crashes.append(f"{label}(rc={rc}, survived={survived}, err={tail})")

        self.assertEqual(
            crashes,
            [],
            f"WITNESS #8: MCP server crashed with unhandled Python exceptions on {len(crashes)}/5 "
            f"adversarial inputs: {crashes}",
        )

    @unittest.skipUnless(os.path.isfile(NATIVE_CLI), "native CLI binary not built in wheel-only test environment")
    def test_native_cli_edge_cases_and_cold_start_latency(self) -> None:
        """Native ./build/streamsieve handles empty stdin, header-only CSV, ragged rows, and <15 ms cold start."""
        self.assertTrue(os.path.isfile(NATIVE_CLI), f"Native CLI binary not found at {NATIVE_CLI}")

        # 1. Empty stdin -> clean non-zero exit, no crash/segfault
        r_empty = subprocess.run([NATIVE_CLI, "sift", "-"], input="", text=True, capture_output=True)
        self.assertEqual(r_empty.returncode, 1)
        self.assertIn("no numeric rows", r_empty.stderr.lower())

        # 2. Header-only CSV -> clean non-zero exit, no crash/segfault
        r_hdr = subprocess.run([NATIVE_CLI, "sift", "-"], input="cpu,mem,disk\n", text=True, capture_output=True)
        self.assertEqual(r_hdr.returncode, 1)
        self.assertIn("no numeric rows", r_hdr.stderr.lower())

        # 3. Ragged CSV rows -> skips mismatched rows cleanly and produces valid JSON
        ragged_csv = "c0,c1,c2\n1.0,2.0,3.0\n4.0,5.0\n6.0,7.0,8.0,9.0\n10.0,11.0,12.0\n"
        r_rag = subprocess.run([NATIVE_CLI, "sift", "--json", "-"], input=ragged_csv, text=True, capture_output=True)
        self.assertEqual(r_rag.returncode, 0, f"Ragged CSV failed: {r_rag.stderr}")
        parsed = _strict_json_loads(r_rag.stdout)
        self.assertEqual(parsed["channels"], 3)
        self.assertEqual(parsed["ticks"], 2)

        # 4. 10-run cold-start latency (< 15 ms median)
        latencies_ms: list[float] = []
        for _ in range(10):
            t0 = time.perf_counter()
            r_ver = subprocess.run([NATIVE_CLI, "version"], capture_output=True, check=True)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            self.assertIn(b"streamsieve", r_ver.stdout)
        latencies_ms.sort()
        median_ms = 0.5 * (latencies_ms[4] + latencies_ms[5])
        self.assertLess(
            median_ms,
            15.0,
            f"Native CLI 10-run cold-start median {median_ms:.2f} ms >= 15 ms (all={latencies_ms})",
        )

    @unittest.skipUnless(os.path.isfile(NATIVE_CLI), "native CLI binary not built in wheel-only test environment")
    def test_witness_native_cli_json_validity_on_quoted_header_and_extreme_values(self) -> None:
        """WITNESS #9 (Native CLI): `./build/streamsieve sift --json` must always emit strict RFC-8259 JSON.

        Currently fails on BOTH:
          - Quoted CSV header (`bad"name,ok`) -> unescaped quote breaks JSON syntax
          - 1e300 on constant-zero channel -> prints literal `-nan` token (invalid RFC-8259 JSON)
        """
        csv_quote = 'bad"name,ok\n1.0,2.0\n3.0,4.0\n'
        r_q = subprocess.run([NATIVE_CLI, "sift", "--json", "-"], input=csv_quote, text=True, capture_output=True)
        self.assertEqual(r_q.returncode, 0)
        try:
            _strict_json_loads(r_q.stdout)
        except ValueError as exc:
            self.fail(f"WITNESS #9a: CLI --json emitted invalid JSON for quoted CSV header: {exc} | out={r_q.stdout!r}")

        csv_extreme = "a,b\n" + ("0.0,0.0\n" * 35) + "1e300,0.0\n" + ("0.0,0.0\n" * 40)
        r_e = subprocess.run([NATIVE_CLI, "sift", "--json", "-"], input=csv_extreme, text=True, capture_output=True)
        self.assertEqual(r_e.returncode, 0)
        try:
            _strict_json_loads(r_e.stdout)
        except ValueError as exc:
            self.fail(f"WITNESS #9b: CLI --json emitted non-RFC-8259 NaN/Inf token: {exc} | out={r_e.stdout!r}")


# =============================================================================
# Surface 7: Zero-Runtime-Dependency & Cold-Import Gate
# =============================================================================


class TestSurface7ZeroDependencyAndColdImportGate(unittest.TestCase):
    """Verify cold import < 15 ms and zero third-party ML/scientific packages loaded."""

    FORBIDDEN = ("numpy", "scipy", "river", "torch", "pandas")

    def test_cold_import_under_15ms_and_zero_third_party_deps(self) -> None:
        script = (
            "import json, sys, time\n"
            "t0 = time.perf_counter()\n"
            "import streamsieve\n"
            "import_ms = (time.perf_counter() - t0) * 1000.0\n"
            "# Also exercise all 4 engines to confirm lazy imports pull zero third-party deps\n"
            "with streamsieve.EStream(4, warmup=12) as est:\n"
            "    est.update_batch([[0.1, 0.2, 0.3, 0.4]] * 16)\n"
            "    _ = est.brief().to_compact_text()\n"
            "with streamsieve.EvidenceLattice() as lat:\n"
            "    lat.observe('h', 'p', 1.0)\n"
            "    _ = streamsieve.EvidenceLattice.from_base85(lat.to_base85())\n"
            "with streamsieve.VOISifter(4, n_sites=2) as sf:\n"
            "    sf.observe_mean(0, 1.0)\n"
            "    _ = sf.allocate(2, n_agents=2)\n"
            "with streamsieve.TrustCalibrator(2) as tc:\n"
            "    tc.predict_prob(1, [0.7, 0.3])\n"
            "    tc.resolve(1, 1.0)\n"
            "forbidden = [m for m in ('numpy','scipy','river','torch','pandas') if m in sys.modules]\n"
            "print(json.dumps({'import_ms': import_ms, 'forbidden': forbidden}))\n"
        )

        timings: list[float] = []
        for _ in range(5):
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                env=_repo_python_env(),
                check=True,
            )
            info = json.loads(proc.stdout)
            self.assertEqual(
                info["forbidden"],
                [],
                f"Forbidden third-party packages loaded at runtime: {info['forbidden']}",
            )
            timings.append(float(info["import_ms"]))

        timings.sort()
        median_ms = timings[len(timings) // 2]
        budget_ms = 120.0 if sys.platform == "win32" else 25.0
        self.assertLess(
            median_ms,
            budget_ms,
            f"Cold `import streamsieve` median {median_ms:.3f} ms >= {budget_ms} ms (runs={timings})",
        )


if __name__ == "__main__":
    unittest.main()
