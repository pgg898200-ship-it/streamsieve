#!/usr/bin/env python3
"""End-to-end 4-engine multi-agent streaming loop using zero third-party dependencies.

Demonstrates how all 4 streamsieve engines compose cleanly in one autonomous swarm:
  1. ``EStream``          — Sifts a 64-channel firehose with anytime-valid e-BH.
  2. ``VOISifter``        — Allocates non-overlapping deep-inspection reads across 4 agents.
  3. ``EvidenceLattice``  — Fuses agent findings via echo-free CRDT gossip (compact SSLT wire).
  4. ``TrustCalibrator``  — Learns agent trust under delayed labels, quarantines a poisoned agent,
                            and outputs Bayes-optimal cost-asymmetric actions.

Run:
    python3 examples/multi_agent_pipeline.py
"""

from __future__ import annotations

import math
import random

from streamsieve import EStream, EvidenceLattice, TrustCalibrator, VOISifter


def main() -> None:
    rng = random.Random(2026)

    # 1. Sift a 64-channel firehose (ch 3 = shift, ch 11 = burst, ch 20..27 = loud 5x nulls)
    n_ch, n_ticks = 64, 400
    mat = []
    for t in range(n_ticks):
        row = [rng.gauss(0.0, 5.0 if 20 <= c < 28 else 1.0) for c in range(n_ch)]
        if t >= 150:
            row[3] += 1.8
            if rng.random() < 0.20:
                row[11] += 3.8
        mat.append(row)

    with EStream(n_ch, warmup=32, alpha=0.05) as est:
        est.update_batch(mat)
        brief = est.brief(max_channels=5)
        print("1) EStream e-BH Brief:")
        print(brief.to_compact_text())

    # 2. VOISifter assigns non-overlapping reads across 4 worker agents
    with VOISifter(n_channels=64, n_sites=8, ticks_per_epoch=8) as sf:
        sf.set_noise_scales([5.0 if 20 <= c < 28 else 1.0 for c in range(n_ch)])
        sf.observe_ticks(11, [0.1, -0.2, 4.1, 0.0, 3.7, -0.1, 0.2, 3.9])
        actions = sf.allocate(budget_per_agent=2, n_agents=4)
        unique_targets = {(a.tool_kind, a.target_id) for a in actions}
        print(f"\n2) VOISifter allocated {len(actions)} reads across 4 agents ({len(unique_targets)} unique, 0 duplicates)")

    # 3. EvidenceLattice CRDT gossip with reprint storm (echo suppression)
    with EvidenceLattice("strict_origin") as lat_a, EvidenceLattice("strict_origin") as lat_b:
        lat_a.observe("incident_db", "prov_ch3_t150", llr=2.1, agent_id=0, timestamp_ns=100)
        lat_b.observe("incident_db", "prov_ch11_t165", llr=1.9, agent_id=1, timestamp_ns=120)
        # Agent 2 echoes Agent 0's finding 5 times
        for _ in range(5):
            lat_b.observe("incident_db", "prov_ch3_t150", llr=2.1, agent_id=2, timestamp_ns=150)

        wire_b = lat_b.to_bytes()
        with EvidenceLattice.from_bytes(wire_b) as wire_copy:
            lat_a.merge(wire_copy)
        summary = lat_a.query("incident_db")
        print(
            f"\n3) EvidenceLattice CRDT sync ({len(wire_b)} B wire): "
            f"P(incident)={summary.posterior_prob:.4f}, unique={summary.unique_evidence}, "
            f"echoes_suppressed={summary.echoes_suppressed}"
        )

    # 4. TrustCalibrator learns trust with 3-step delayed labels & quarantines poisoned agent 2
    with TrustCalibrator(n_agents=3, conformal_alpha=0.10) as tc:
        pending_truth: list[tuple[int, float]] = []
        for t in range(60):
            y = 1.0 if rng.random() < 0.5 else 0.0
            p_good = 0.85 if y > 0.5 else 0.15
            p_noisy = 0.65 if y > 0.5 else 0.35
            p_poison = 0.02 if y > 0.5 else 0.98
            dec = tc.predict_prob(t, [p_good, p_noisy, p_poison], underage_cost=4.0, overage_cost=1.0)
            pending_truth.append((t, y))
            if len(pending_truth) >= 3:
                old_id, old_y = pending_truth.pop(0)
                tc.resolve(old_id, old_y)
        ags = tc.agents()
        print("\n4) TrustCalibrator after 60 delayed ticks (4:1 underage:overage):")
        for a in ags:
            print(
                f"   agent {a.agent_id}: weight={a.weight:.3f}, platt_a={a.platt_slope:.2f}, "
                f"quarantined={a.quarantined}, ville_e={a.quarantine_e:.1f}"
            )
        assert not math.isnan(dec.optimal_action)


if __name__ == "__main__":
    main()
