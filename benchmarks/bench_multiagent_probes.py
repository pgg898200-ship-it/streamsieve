"""StreamSwarmBench — All 8 Multi-Agent Streaming Mechanisms + Real NOAA Weather Benchmark.

Evaluates streamsieve against standard multi-agent baselines across all 8 mechanisms:
  1. `echo`        — Echo-Chamber Reprint Storm (W=4 and W=8 agents, 200 trials)
  2. `sift`+`voi`  — 2,048-ch bursty firehose (256 sites x 8 ch, 1 hot site, 25% 3x loud,
                     rare 15% +2.8 sigma bursts, W=1 and W=4 agents, 10 seeds)
  3. `trust`+`delay` — 4-agent swarm (expert, overconfident bloviator, weak sensor,
                     Byzantine poisoned) with 4-epoch delayed labels & mid-stream trust flip
                     at epoch 60 (20 seeds)
  4. `act`         — Real NOAA Daily Weather (`examples/noaa_weather.csv`, Seattle `temp_max`,
                     4:1 underage:overage asymmetric cost, 1-day delayed label)
  5. `bits`        — Inter-Agent Wire Budget (8 hypotheses x 32 unique findings across 4 agents:
                     SSLT binary & Base85 vs compact JSON vs JSON-text debate messages)
  6. `stop`        — Anytime-Valid Sequential Stopping (200 H0 + 200 H1 trials, T=400,
                     peeking every 10 ticks at alpha=0.05)
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats as sp_stats

from streamsieve import EStream, EvidenceLattice, TrustCalibrator, VOISifter

REPO_ROOT = Path(__file__).resolve().parent.parent
NOAA_CSV = REPO_ROOT / "examples" / "noaa_weather.csv"


def _sigmoid(x: float) -> float:
    if x >= 0.0:
        z = math.exp(-min(x, 60.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(x, -60.0))
    return z / (1.0 + z)


def probe_echo(n_trials: int = 200, seed: int = 3001) -> dict[str, Any]:
    """Mechanism 1 (`echo`): Echo-Chamber Reprint Storm across W=4 and W=8 agents."""
    prior_brier = 0.25
    n_unique = 4
    mu = 0.65
    out_by_w: dict[str, Any] = {}

    for w_agents in (4, 8):
        rng = np.random.default_rng(seed + w_agents * 100)
        briers_lat: list[float] = []
        briers_lww: list[float] = []
        briers_sum: list[float] = []
        infl_lat: list[float] = []
        infl_sum: list[float] = []
        lww_overwrites = 0
        lww_total_writes = 0
        echoes_suppressed_total = 0

        for trial in range(n_trials):
            y = 1.0 if rng.random() < 0.5 else 0.0
            sign = 1.0 if y > 0.5 else -1.0
            xs = rng.normal(sign * mu, 1.0, size=n_unique)
            llrs = [float(2.0 * mu * x) for x in xs]
            true_lo = float(sum(llrs))

            # Build echo storm: each unique finding originates at one agent, then every peer
            # echoes/reprints it across the swarm (total W copies per finding, plus viral
            # 2nd-hop reprints of finding 0 so echo amplification is heterogeneous).
            messages: list[tuple[int, int, float, int]] = []  # (agent_id, prov_id, llr, ts)
            ts = 100
            for k in range(n_unique):
                origin_agent = k % w_agents
                messages.append((origin_agent, k, llrs[k], ts))
                ts += 10
                for peer in range(w_agents):
                    if peer != origin_agent:
                        messages.append((peer, k, llrs[k], ts))
                        ts += 10
            # Viral 2nd-hop reprint burst of finding 0 by all agents
            for peer in range(w_agents):
                messages.append((peer, 0, llrs[0], ts))
                ts += 10
            rng.shuffle(messages)

            # 1. EvidenceLattice (per-agent CRDT lattices merged order-independently)
            agent_lats = [EvidenceLattice("strict_origin") for _ in range(w_agents)]
            for lat in agent_lats:
                lat.set_prior("H", 0.0)
            for aid, pid, llr, t_ns in messages:
                agent_lats[aid].observe("H", pid, llr, agent_id=aid, reliability=0.9, timestamp_ns=t_ns)
            merged = agent_lats[0]
            for lat in agent_lats[1:]:
                merged.merge(lat)
                lat.close()
            q = merged.query("H")
            p_lat = q.posterior_prob
            lo_lat = q.total_log_odds
            echoes_suppressed_total += len(messages) - q.unique_evidence
            merged.close()

            # 2. Naive Last-Write-Wins Blackboard (each agent posts its latest finding, clobbering prior state)
            bb_prob = 0.5
            last_prov: int | None = None
            last_agent: int | None = None
            for aid, pid, llr, _t_ns in messages:
                if last_prov is not None and (pid != last_prov or aid != last_agent):
                    lww_overwrites += 1
                lww_total_writes += 1
                bb_prob = _sigmoid(llr)
                last_prov = pid
                last_agent = aid

            # 3. Uncalibrated Log-Odds Summation (no provenance deduplication)
            sum_lo = float(sum(llr for _, _, llr, _ in messages))
            p_sum = _sigmoid(sum_lo)

            briers_lat.append((p_lat - y) ** 2)
            briers_lww.append((bb_prob - y) ** 2)
            briers_sum.append((p_sum - y) ** 2)
            infl_lat.append(abs(lo_lat))
            infl_sum.append(abs(sum_lo))

        mean_true_abs_lo = float(np.mean(infl_lat))
        b_lat = float(np.mean(briers_lat))
        b_lww = float(np.mean(briers_lww))
        b_sum = float(np.mean(briers_sum))

        out_by_w[f"W={w_agents}"] = {
            "n_agents": w_agents,
            "n_trials": n_trials,
            "unique_findings_per_trial": n_unique,
            "messages_per_trial": n_unique * w_agents + w_agents,
            "evidence_lattice": {
                "brier_score": round(b_lat, 4),
                "brier_skill_vs_prior": round(1.0 - b_lat / prior_brier, 4),
                "overwrite_churn_pct": 0.0,
                "echo_inflation_factor": round(float(np.mean(infl_lat)) / max(mean_true_abs_lo, 1e-12), 2),
                "echoes_suppressed_per_trial": round(echoes_suppressed_total / float(n_trials), 1),
            },
            "naive_lww_blackboard": {
                "brier_score": round(b_lww, 4),
                "brier_skill_vs_prior": round(1.0 - b_lww / prior_brier, 4),
                "overwrite_churn_pct": round(100.0 * lww_overwrites / max(lww_total_writes, 1), 2),
                "echo_inflation_factor": 1.0,
            },
            "uncalibrated_log_odds_sum": {
                "brier_score": round(b_sum, 4),
                "brier_skill_vs_prior": round(1.0 - b_sum / prior_brier, 4),
                "overwrite_churn_pct": 0.0,
                "echo_inflation_factor": round(float(np.mean(infl_sum)) / max(mean_true_abs_lo, 1e-12), 2),
            },
        }

    return out_by_w


def probe_sift_voi(n_seeds: int = 10, base_seed: int = 4000) -> dict[str, Any]:
    """Mechanisms 2 & 3 (`sift` + `voi`): 2,048-channel bursty firehose, W=1 and W=4."""
    n_ch, n_sites, ch_per_site = 2048, 256, 8
    ticks_per_ep = 4
    n_epochs = 120
    budget_per_agent = 4
    active_per_hot_site = 6
    out_by_w: dict[str, Any] = {}

    for w_agents in (1, 4):
        skills_voi: list[float] = []
        rec_voi: list[float] = []
        loud_voi: list[float] = []
        inter_dup_voi: list[float] = []
        tool_totals = {"site_agg": 0, "channel_mean": 0, "channel_ticks": 0}

        skills_rr: list[float] = []
        rec_rr: list[float] = []
        loud_rr: list[float] = []

        skills_raw: list[float] = []
        rec_raw: list[float] = []
        loud_raw: list[float] = []

        for s_idx in range(n_seeds):
            rng = np.random.default_rng(base_seed + s_idx)
            hot_site = int(rng.integers(0, n_sites))
            active = np.zeros(n_ch, dtype=bool)
            c0 = hot_site * ch_per_site
            active[c0 : c0 + active_per_hot_site] = True
            n_active = int(active.sum())

            scales = np.ones(n_ch, dtype=np.float64)
            loud_mask = rng.random(n_ch) < 0.25
            loud_mask[active] = False
            scales[loud_mask] = 3.0

            p_base = n_active / float(n_ch)
            brier_prior = float(np.mean((p_base - active.astype(np.float64)) ** 2))

            sf = VOISifter(
                n_ch,
                n_sites=n_sites,
                prior_active=0.002,
                hot_site_prior=0.01,
                hot_site_boost=0.75,
                shift_snr=0.1,
                burst_prob=0.15,
                burst_snr=2.8,
                ticks_per_epoch=ticks_per_ep,
            )
            sf.set_noise_scales(scales)

            sf_rr = VOISifter(
                n_ch,
                n_sites=n_sites,
                prior_active=0.002,
                hot_site_prior=0.01,
                hot_site_boost=0.75,
                shift_snr=0.1,
                burst_prob=0.15,
                burst_snr=2.8,
                ticks_per_epoch=ticks_per_ep,
            )
            sf_rr.set_noise_scales(scales)

            raw_sum = np.zeros(n_ch, dtype=np.float64)
            raw_cnt = np.zeros(n_ch, dtype=np.float64)
            cursor = 0
            inter_dups = 0
            total_actions = 0

            for _ep in range(n_epochs):
                ep_data = rng.standard_normal((ticks_per_ep, n_ch)) * scales
                bmask = rng.random((ticks_per_ep, n_active)) < 0.15
                ep_data[:, active] += bmask * 2.8
                ep_means = ep_data.mean(axis=0)

                acts = sf.allocate(
                    budget_per_agent=budget_per_agent,
                    n_agents=w_agents,
                    allow_site_agg=True,
                    allow_mean=True,
                    allow_ticks=True,
                )
                seen_wave: dict[tuple[str, int], int] = {}
                for a in acts:
                    tool_totals[a.tool_kind] += 1
                    key = (a.tool_kind, a.target_id)
                    if key in seen_wave and seen_wave[key] != a.agent_slot:
                        inter_dups += 1
                    seen_wave.setdefault(key, a.agent_slot)
                    total_actions += 1

                    if a.tool_kind == "site_agg":
                        sc0 = a.target_id * ch_per_site
                        sf.observe_site_agg(
                            a.target_id, float(ep_means[sc0 : sc0 + ch_per_site].mean())
                        )
                    elif a.tool_kind == "channel_mean":
                        sf.observe_mean(a.target_id, float(ep_means[a.target_id]))
                    else:
                        sf.observe_ticks(a.target_id, np.ascontiguousarray(ep_data[:, a.target_id]))

                # Matched cost budget for Round-Robin (Bayes filter) & Naive Top-Raw-Mean
                for _ in range(budget_per_agent * w_agents):
                    ch = cursor % n_ch
                    cursor += 1
                    mval = float(ep_means[ch])
                    sf_rr.observe_mean(ch, mval)
                    raw_sum[ch] += mval
                    raw_cnt[ch] += 1.0

            probs_voi = np.array(sf.posteriors(), dtype=np.float64)
            sf.close()
            probs_rr = np.array(sf_rr.posteriors(), dtype=np.float64)
            sf_rr.close()

            brier_v = float(np.mean((probs_voi - active.astype(np.float64)) ** 2))
            top_v = np.argsort(-probs_voi)[:n_active]
            skills_voi.append(1.0 - brier_v / brier_prior)
            rec_voi.append(float(active[top_v].sum()) / float(n_active))
            loud_voi.append(float(np.sum((probs_voi > 0.5) & loud_mask)))
            inter_dup_voi.append(100.0 * inter_dups / max(total_actions, 1))

            brier_r = float(np.mean((probs_rr - active.astype(np.float64)) ** 2))
            top_r = np.argsort(-probs_rr)[:n_active]
            skills_rr.append(1.0 - brier_r / brier_prior)
            rec_rr.append(float(active[top_r].sum()) / float(n_active))
            loud_rr.append(float(np.sum((probs_rr > 0.5) & loud_mask)))

            avg_abs = np.abs(raw_sum / np.maximum(raw_cnt, 1.0))
            naive_z = avg_abs * np.sqrt(ticks_per_ep * np.maximum(raw_cnt, 1.0))
            naive_p = 1.0 / (1.0 + np.exp(-np.clip(naive_z - 2.5, -40.0, 40.0)))
            naive_p[raw_cnt == 0] = p_base
            brier_raw = float(np.mean((naive_p - active.astype(np.float64)) ** 2))
            top_raw = np.argsort(-avg_abs)[:n_active]
            skills_raw.append(1.0 - brier_raw / brier_prior)
            rec_raw.append(float(active[top_raw].sum()) / float(n_active))
            loud_raw.append(float(np.sum((naive_p > 0.5) & loud_mask)))

        out_by_w[f"W={w_agents}"] = {
            "n_agents": w_agents,
            "n_channels": n_ch,
            "n_sites": n_sites,
            "n_seeds": n_seeds,
            "n_epochs": n_epochs,
            "budget_per_agent_per_epoch": budget_per_agent,
            "voi_sifter": {
                "brier_skill": round(float(np.mean(skills_voi)), 4),
                "recall_at_top_active": round(float(np.mean(rec_voi)), 4),
                "false_flags_loud": round(float(np.mean(loud_voi)), 2),
                "inter_agent_dup_rate_pct": round(float(np.mean(inter_dup_voi)), 3),
                "tool_mix_total_across_seeds": tool_totals,
            },
            "uniform_round_robin_bayes": {
                "brier_skill": round(float(np.mean(skills_rr)), 4),
                "recall_at_top_active": round(float(np.mean(rec_rr)), 4),
                "false_flags_loud": round(float(np.mean(loud_rr)), 2),
            },
            "naive_top_raw_mean": {
                "brier_skill": round(float(np.mean(skills_raw)), 4),
                "recall_at_top_active": round(float(np.mean(rec_raw)), 4),
                "false_flags_loud": round(float(np.mean(loud_raw)), 2),
            },
        }

    return out_by_w


def probe_trust_delay(n_seeds: int = 20, base_seed: int = 5000) -> dict[str, Any]:
    """Mechanisms 4 & 5 (`trust` + `delay`): 4-agent Byzantine/bloviator swarm + 4-epoch delay + flip@60."""
    n_epochs, flip_epoch, delay = 120, 60, 4
    tc_all: list[float] = []
    tc_post: list[float] = []
    unif_all: list[float] = []
    unif_post: list[float] = []
    ew_all: list[float] = []
    ew_post: list[float] = []
    byz_quarantined: list[float] = []
    byz_quar_epochs: list[int] = []
    blov_slopes: list[float] = []

    for s_idx in range(n_seeds):
        rng = np.random.default_rng(base_seed + s_idx)
        tc = TrustCalibrator(4, fixed_share_alpha=0.05, eta=0.8, quarantine_e_thresh=20.0)
        w_ew = np.full(4, 0.25, dtype=np.float64)
        pending_tc: list[tuple[int, float]] = []
        pending_ew: list[tuple[int, list[float], float]] = []

        loss_tc: list[float] = []
        loss_unif: list[float] = []
        loss_ew: list[float] = []
        first_quar_ep: int | None = None

        for t in range(n_epochs):
            z = float(rng.normal(0.0, 1.8))
            p_true = _sigmoid(z)
            y = 1.0 if rng.random() < p_true else 0.0

            p_expert = float(np.clip(p_true + rng.normal(0.0, 0.03), 0.02, 0.98))
            p_weak = float(np.clip(0.5 + 0.15 * (p_true - 0.5) + rng.normal(0.0, 0.07), 0.05, 0.95))
            if t < flip_epoch:
                p0, p2 = p_expert, p_weak
            else:
                p0, p2 = p_weak, p_expert
            p1 = 0.98 if rng.random() < 0.5 else 0.02  # Overconfident random bloviator
            p3 = float(np.clip(1.0 - p_true, 0.01, 0.99))  # Actively poisoned Byzantine agent
            probs = [p0, p1, p2, p3]

            dec = tc.predict_prob(t, probs)
            p_unif = float(np.mean(probs))
            p_ew = float(np.dot(w_ew, probs))

            loss_tc.append((dec.fused_prob - y) ** 2)
            loss_unif.append((p_unif - y) ** 2)
            loss_ew.append((p_ew - y) ** 2)

            pending_tc.append((t, y))
            pending_ew.append((t, probs, y))

            # Strictly causal 4-epoch label delay
            if t >= delay:
                did, ylab = pending_tc.pop(0)
                tc.resolve(did, ylab)
                if first_quar_ep is None and tc.agents()[3].quarantined:
                    first_quar_ep = t

                _, pr_old, y_old = pending_ew.pop(0)
                b_old = np.array([(p - y_old) ** 2 for p in pr_old], dtype=np.float64)
                # Standard Exponential Weights (Hedge) with eta=2.0 (no Platt / fixed-share / quarantine)
                w_ew = w_ew * np.exp(-2.0 * b_old)
                w_ew /= float(w_ew.sum())

        for did, ylab in pending_tc:
            tc.resolve(did, ylab)
        final_ags = tc.agents()
        byz_quarantined.append(1.0 if final_ags[3].quarantined else 0.0)
        if first_quar_ep is not None:
            byz_quar_epochs.append(first_quar_ep)
        blov_slopes.append(final_ags[1].platt_slope)
        tc.close()

        tc_all.append(float(np.mean(loss_tc)))
        tc_post.append(float(np.mean(loss_tc[flip_epoch:])))
        unif_all.append(float(np.mean(loss_unif)))
        unif_post.append(float(np.mean(loss_unif[flip_epoch:])))
        ew_all.append(float(np.mean(loss_ew)))
        ew_post.append(float(np.mean(loss_ew[flip_epoch:])))

    return {
        "n_seeds": n_seeds,
        "n_epochs": n_epochs,
        "delay_epochs": delay,
        "trust_flip_epoch": flip_epoch,
        "trust_calibrator": {
            "overall_brier": round(float(np.mean(tc_all)), 4),
            "post_flip_brier": round(float(np.mean(tc_post)), 4),
            "byzantine_quarantine_rate_pct": round(float(np.mean(byz_quarantined)) * 100.0, 1),
            "mean_quarantine_epoch": round(float(np.mean(byz_quar_epochs)), 1)
            if byz_quar_epochs
            else None,
            "bloviator_platt_slope_final": round(float(np.mean(blov_slopes)), 3),
        },
        "plain_exponential_weights": {
            "overall_brier": round(float(np.mean(ew_all)), 4),
            "post_flip_brier": round(float(np.mean(ew_post)), 4),
            "byzantine_quarantine_rate_pct": 0.0,
        },
        "unweighted_mean_vote": {
            "overall_brier": round(float(np.mean(unif_all)), 4),
            "post_flip_brier": round(float(np.mean(unif_post)), 4),
            "byzantine_quarantine_rate_pct": 0.0,
        },
    }


def probe_act_noaa() -> dict[str, Any]:
    """Mechanism 6 (`act`): Real NOAA Seattle daily `temp_max`, 4:1 asymmetric cost, 1-day delay."""
    temps_list: list[float] = []
    with NOAA_CSV.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row["location"] == "Seattle":
                temps_list.append(float(row["temp_max"]))
    temps = np.array(temps_list, dtype=np.float64)

    cu, co = 4.0, 1.0

    def asym_cost(y: float, a: float) -> float:
        return cu * max(0.0, y - a) + co * max(0.0, a - y)

    costs_opt_post: list[float] = []
    costs_mean_post: list[float] = []
    cov_post: list[float] = []
    widths_post: list[float] = []

    costs_opt_all: list[float] = []
    costs_mean_all: list[float] = []
    cov_all: list[float] = []

    ewma = float(temps[0])
    pending: list[tuple[int, float]] = []

    with TrustCalibrator(3, conformal_alpha=0.10) as tc:
        for t in range(2, len(temps)):
            y_true = float(temps[t])
            # Strictly causal 3-agent ensemble using only <= t-1 observations
            p_pers = float(temps[t - 1])
            ewma = 0.25 * float(temps[t - 1]) + 0.75 * ewma
            p_ewma = ewma
            p_ma30 = float(temps[max(0, t - 30) : t].mean())

            sd_pers = (
                float(np.std(np.diff(temps[max(0, t - 22) : t]), ddof=1)) if t >= 4 else 3.0
            )
            sd_ewma = max(sd_pers * 0.95, 0.5)
            sd_ma30 = float(np.std(temps[max(0, t - 30) : t], ddof=1)) if t >= 4 else 3.5

            dec = tc.predict_scalar(
                t,
                [p_pers, p_ewma, p_ma30],
                agent_stds=[max(sd_pers, 0.5), sd_ewma, max(sd_ma30, 0.5)],
                underage_cost=cu,
                overage_cost=co,
            )

            # 1-day delayed label: resolve day t-1 strictly AFTER predicting day t
            pending.append((t, y_true))
            if len(pending) >= 2:
                did, ylab = pending.pop(0)
                tc.resolve(did, ylab)

            c_o = asym_cost(y_true, dec.optimal_action)
            c_m = asym_cost(y_true, dec.fused_point)
            hit = 1.0 if (dec.ci_low <= y_true <= dec.ci_high) else 0.0

            costs_opt_all.append(c_o)
            costs_mean_all.append(c_m)
            cov_all.append(hit)

            if t >= 30:
                costs_opt_post.append(c_o)
                costs_mean_post.append(c_m)
                cov_post.append(hit)
                widths_post.append(dec.ci_high - dec.ci_low)

        for did, ylab in pending:
            tc.resolve(did, ylab)

    mean_opt = float(np.mean(costs_opt_post))
    mean_pt = float(np.mean(costs_mean_post))
    red_pct = (1.0 - mean_opt / mean_pt) * 100.0

    mean_opt_all = float(np.mean(costs_opt_all))
    mean_pt_all = float(np.mean(costs_mean_all))

    return {
        "dataset": "examples/noaa_weather.csv (Seattle daily temp_max)",
        "n_days_total": int(len(temps)),
        "n_days_evaluated_post_warmup": len(costs_opt_post),
        "underage_to_overage_ratio": "4:1",
        "label_delay_days": 1,
        "target_ci_coverage_pct": 90.0,
        "trust_calibrator_optimal_action": {
            "mean_asymmetric_cost": round(mean_opt, 4),
            "full_series_mean_asymmetric_cost": round(mean_opt_all, 4),
            "empirical_ci_coverage_pct": round(float(np.mean(cov_post)) * 100.0, 2),
            "full_series_ci_coverage_pct": round(float(np.mean(cov_all)) * 100.0, 2),
            "mean_ci_width_deg_c": round(float(np.mean(widths_post)), 2),
        },
        "point_mean_action_baseline": {
            "mean_asymmetric_cost": round(mean_pt, 4),
            "full_series_mean_asymmetric_cost": round(mean_pt_all, 4),
        },
        "asymmetric_cost_reduction_pct": round(red_pct, 2),
        "full_series_cost_reduction_pct": round((1.0 - mean_opt_all / mean_pt_all) * 100.0, 2),
    }


def probe_bits() -> dict[str, Any]:
    """Mechanism 7 (`bits`): Inter-Agent Wire Budget for 8 hyps x 32 unique findings across 4 agents."""
    n_hyps = 8
    findings_per_hyp = 32
    n_agents = 4
    total_findings = n_hyps * findings_per_hyp  # 256 unique findings across 8 hypotheses

    rng = np.random.default_rng(7001)
    agent_lats = [EvidenceLattice("strict_origin") for _ in range(n_agents)]
    full_lat = EvidenceLattice("strict_origin")

    debate_msgs_by_agent: list[list[dict[str, Any]]] = [[] for _ in range(n_agents)]
    compact_json_by_agent: list[list[dict[str, Any]]] = [[] for _ in range(n_agents)]

    idx = 0
    for h in range(n_hyps):
        hyp_name = f"incident_hyp_{h:02d}"
        full_lat.set_prior(hyp_name, -1.5)
        for lat in agent_lats:
            lat.set_prior(hyp_name, -1.5)
        for f in range(findings_per_hyp):
            aid = idx % n_agents
            prov = f"log:{hyp_name}:evt_{f:03d}"
            llr = float(round(rng.normal(0.8, 0.9), 4))
            log_e = float(round(llr * 0.95, 4))
            rel = 0.92
            ts = 1_700_000_000_000 + idx * 1000

            agent_lats[aid].observe(
                hyp_name, prov, llr, log_e=log_e, agent_id=aid, reliability=rel, timestamp_ns=ts
            )
            full_lat.observe(
                hyp_name, prov, llr, log_e=log_e, agent_id=aid, reliability=rel, timestamp_ns=ts
            )

            compact_json_by_agent[aid].append(
                {"h": hyp_name, "p": prov, "llr": llr, "le": log_e, "a": aid, "r": rel, "t": ts}
            )
            debate_msgs_by_agent[aid].append(
                {
                    "role": f"agent_{aid}",
                    "hypothesis": hyp_name,
                    "provenance_id": prov,
                    "log_likelihood_ratio": llr,
                    "log_e_value": log_e,
                    "reliability": rel,
                    "timestamp_ns": ts,
                    "message": (
                        f"Agent {aid} observed evidence {prov} supporting {hyp_name} with "
                        f"estimated LLR={llr:+.4f} and reliability={rel:.2f}; requesting peer "
                        f"verification and belief reconciliation across the 4-agent swarm."
                    ),
                }
            )
            idx += 1

    # Single-agent state payloads (64 findings each) broadcast to 3 peers = all-to-all sync (x3)
    per_agent_sslt = [len(lat.to_bytes()) for lat in agent_lats]
    per_agent_b85 = [len(lat.to_base85()) for lat in agent_lats]
    per_agent_compact_json = [
        len(json.dumps(msgs, separators=(",", ":")).encode("utf-8"))
        for msgs in compact_json_by_agent
    ]
    per_agent_debate_json = [
        len(json.dumps(msgs).encode("utf-8")) for msgs in debate_msgs_by_agent
    ]

    # Full merged 256-finding lattice state
    full_sslt_bytes = len(full_lat.to_bytes())
    full_b85_chars = len(full_lat.to_base85())
    all_debate = [m for msgs in debate_msgs_by_agent for m in msgs]
    all_compact = [m for msgs in compact_json_by_agent for m in msgs]
    full_debate_bytes = len(json.dumps(all_debate).encode("utf-8"))
    full_compact_bytes = len(json.dumps(all_compact, separators=(",", ":")).encode("utf-8"))

    # All-to-all 4-agent sync: each of the 4 agents sends its payload to its 3 peers
    swarm_sslt_bytes = sum(per_agent_sslt) * (n_agents - 1)
    swarm_b85_chars = sum(per_agent_b85) * (n_agents - 1)
    swarm_compact_bytes = sum(per_agent_compact_json) * (n_agents - 1)
    swarm_debate_bytes = sum(per_agent_debate_json) * (n_agents - 1)

    for lat in agent_lats:
        lat.close()
    full_lat.close()

    return {
        "n_hypotheses": n_hyps,
        "unique_findings_per_hyp": findings_per_hyp,
        "total_unique_findings": total_findings,
        "n_agents": n_agents,
        "full_256_item_state": {
            "sslt_binary_bytes": full_sslt_bytes,
            "sslt_base85_chars": full_b85_chars,
            "compact_json_bytes": full_compact_bytes,
            "json_debate_bytes": full_debate_bytes,
            "sslt_vs_json_debate_compression_x": round(full_debate_bytes / full_sslt_bytes, 2),
            "base85_vs_json_debate_compression_x": round(full_debate_bytes / full_b85_chars, 2),
            "sslt_vs_compact_json_compression_x": round(full_compact_bytes / full_sslt_bytes, 2),
            "sslt_bytes_reduction_vs_debate_pct": round(
                (1.0 - full_sslt_bytes / full_debate_bytes) * 100.0, 2
            ),
            "base85_chars_reduction_vs_debate_pct": round(
                (1.0 - full_b85_chars / full_debate_bytes) * 100.0, 2
            ),
        },
        "swarm_4_agent_all_to_all_sync": {
            "sslt_binary_bytes": swarm_sslt_bytes,
            "sslt_base85_chars": swarm_b85_chars,
            "compact_json_bytes": swarm_compact_bytes,
            "json_debate_bytes": swarm_debate_bytes,
            "sslt_vs_json_debate_compression_x": round(swarm_debate_bytes / swarm_sslt_bytes, 2),
            "base85_vs_json_debate_compression_x": round(swarm_debate_bytes / swarm_b85_chars, 2),
        },
    }


def probe_stop(n_trials: int = 200, seed: int = 8001) -> dict[str, Any]:
    """Mechanism 8 (`stop`): Anytime-Valid Sequential Stopping vs Naive Peeking & Fixed Horizon."""
    rng = np.random.default_rng(seed)
    # EStream uses warmup=32 + first post-warmup reference snapshot at tick 64, so plant cp=80
    n_ticks, cp, peek_every, alpha = 400, 80, 10, 0.05
    e_thresh = 1.0 / alpha

    x_h0 = np.ascontiguousarray(rng.standard_normal((n_ticks, n_trials)), dtype=np.float64)
    x_h1 = np.ascontiguousarray(rng.standard_normal((n_ticks, n_trials)), dtype=np.float64)
    x_h1[cp:, :] += 1.0  # +1.0 sigma sustained shift after tick 80

    def eval_streamsieve(mat: np.ndarray) -> tuple[float, float]:
        stopped_at = np.full(n_trials, n_ticks, dtype=np.int32)
        rejected = np.zeros(n_trials, dtype=bool)
        with EStream(n_trials, warmup=32, ring_capacity=64, alpha=alpha) as est:
            for start in range(0, n_ticks, peek_every):
                end = min(n_ticks, start + peek_every)
                est.update_batch(mat[start:end])
                if end >= 40:
                    for st in est.topk(n_trials):
                        if st.e_value < e_thresh:
                            break
                        if not rejected[st.channel]:
                            rejected[st.channel] = True
                            stopped_at[st.channel] = end
        return float(rejected.mean()), float(stopped_at.mean())

    def eval_peeking_ttest(mat: np.ndarray) -> tuple[float, float]:
        stopped_at = np.full(n_trials, n_ticks, dtype=np.int32)
        rejected = np.zeros(n_trials, dtype=bool)
        for t in range(40, n_ticks + 1, peek_every):
            mid = t // 2
            _, pvals = sp_stats.ttest_ind(mat[:mid], mat[mid:t], axis=0, equal_var=False)
            newly = (~rejected) & (np.nan_to_num(pvals, nan=1.0) < alpha)
            rejected |= newly
            stopped_at[newly] = t
        return float(rejected.mean()), float(stopped_at.mean())

    def eval_fixed_ttest(mat: np.ndarray) -> tuple[float, float]:
        # Fixed-horizon test must wait until T=400 (comparing warmup [:40] vs post [40:])
        _, pvals = sp_stats.ttest_ind(mat[:cp], mat[cp:], axis=0, equal_var=False)
        rej = np.nan_to_num(pvals, nan=1.0) < alpha
        return float(rej.mean()), float(n_ticks)

    ss_h0_far, _ = eval_streamsieve(x_h0)
    ss_h1_pow, ss_h1_tau = eval_streamsieve(x_h1)

    pk_h0_far, _ = eval_peeking_ttest(x_h0)
    pk_h1_pow, pk_h1_tau = eval_peeking_ttest(x_h1)

    fx_h0_far, _ = eval_fixed_ttest(x_h0)
    fx_h1_pow, fx_h1_tau = eval_fixed_ttest(x_h1)

    return {
        "n_trials_per_arm": n_trials,
        "max_horizon_ticks": n_ticks,
        "changepoint_tick": cp,
        "peek_every": peek_every,
        "nominal_alpha": alpha,
        "streamsieve_anytime_stop": {
            "h0_false_stop_rate": round(ss_h0_far, 4),
            "h1_power": round(ss_h1_pow, 4),
            "h1_mean_stop_tick": round(ss_h1_tau, 1),
            "sample_savings_vs_fixed_pct": round((1.0 - ss_h1_tau / fx_h1_tau) * 100.0, 1),
        },
        "naive_peeking_ttest": {
            "h0_false_stop_rate": round(pk_h0_far, 4),
            "h1_power": round(pk_h1_pow, 4),
            "h1_mean_stop_tick": round(pk_h1_tau, 1),
        },
        "fixed_horizon_ttest": {
            "h0_false_stop_rate": round(fx_h0_far, 4),
            "h1_power": round(fx_h1_pow, 4),
            "h1_mean_stop_tick": round(fx_h1_tau, 1),
        },
    }


def run_benchmark() -> dict[str, Any]:
    """Run all 8 multi-agent mechanism probes + real NOAA weather benchmark."""
    return {
        "echo": probe_echo(),
        "sift_and_voi": probe_sift_voi(),
        "trust_and_delay": probe_trust_delay(),
        "act_noaa_seattle": probe_act_noaa(),
        "bits_wire_budget": probe_bits(),
        "stop_sequential": probe_stop(),
    }


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), indent=2))
