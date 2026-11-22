---
name: streamsieve
description: >
  Sift high-frequency telemetry, metrics, and time-series streams into compact anytime-valid e-BH briefs (>95% context token reduction) with lossless raw-tick drill-down, deduplicate multi-agent beliefs via echo-free CRDT lattices, partition budgeted inspections across agents with zero-overlap hierarchical VOI, and calibrate/quarantine unreliable or poisoned agents under delayed ground truth. Use whenever analyzing live or batch numeric streams, detecting mean shifts or sparse bursts under continuous peeking, merging evidence across multiple agents, or calibrating agent forecasts.
---

# `streamsieve` — Multi-Agent Streaming Inference & Firehose Sifter

`streamsieve` is a zero-dependency C++20/AVX2 streaming inference runtime with a native CLI, stdio MCP server, and pure-stdlib Python bindings. It solves four recurring AI-agent failure modes simultaneously:

1. **Context explosion & p-hacking on live streams** (`EStream` / `EStreamEngine`) — Compresses wide `N channels × T ticks` firehoses into compact e-BH briefs (>95% fewer tokens) with anytime-valid FDR control ($\operatorname{FDR} \le \alpha$ under arbitrary dependence and continuous peeking) and lossless raw-tick ring drill-down.
2. **Echo-chamber double counting** (`EvidenceLattice`) — State-based join-semilattice CRDT keyed by 64-bit `provenance_id` (bit-exact commutative, associative, idempotent merge) plus Vovk–Wang admissible cross-agent e-value merging.
3. **Redundant & loud-channel-trapped multi-agent reads** (`VOISifter`) — Noise-normalized `site × channel × tick` hierarchical Bayesian inference with exact sparse-burst mixture LLR and zero-overlap greedy Expected Brier-Risk Reduction allocation.
4. **Delayed-label leakage, bloviators & poisoned agents** (`TrustCalibrator`) — Strictly causal pending join buffer, non-negative online Platt shrinkage, discounted fixed-share BOA/Hedge, Ville e-value quarantine + shadow recovery, multi-rate delayed AgACI conformal intervals, and Bayes-optimal cost-asymmetric actions.

---

## When to Activate This Skill

Activate `streamsieve` automatically when the task involves any of:
- Inspecting CSV/TSV/matrix telemetry, observability metrics, sensor feeds, financial ticks, or benchmark logs with many channels or rows.
- Detecting regime shifts, latency regressions, or rare burst anomalies continuously without inflating false alarms from repeated peeking.
- Fusing evidence, alerts, or hypotheses reported by multiple agents or tools where observations may be forwarded, gossiped, or echoed.
- Allocating limited read/tool budgets across multiple subagents inspecting a large fleet of sites and channels.
- Combining probability or continuous scalar forecasts from multiple agents/models with delayed ground truth, overconfidence, regime flips, or Byzantine/poisoned members.
- Auditing whether a streaming detector or feature pipeline has zero lookahead leakage (`assert_causal`).

---

## Golden Workflow: Sift First, Drill Down Losslessly

**Never paste raw multi-hundred-row or wide-channel numeric tables directly into LLM context.** Follow this two-stage pattern:

1. **Sift the firehose first** via CLI (`streamsieve sift`) or MCP (`sift_stream`). Inspect only the compact e-BH brief (`ALERT` channels passing exact online e-BH at level $\alpha$, plus top `watch` channels, changepoint tick, and anomaly `mode`: `mean_shift`, `burst`, or `both`).
2. **Drill down losslessly only on flagged handles** (`ch:<id>@t=<cp>..<tick>`) via `fetch_raw_ticks` (MCP) or `est.fetch_raw(handle)` (Python) to inspect exact raw ticks around the detected changepoint.

---

## 1. CLI Usage (`streamsieve` / `sieve`)

Both the native C++20 binary (`build/streamsieve`, `<3 ms` cold start) and the zero-dependency Python CLI (`streamsieve` / `sieve`) expose compact streaming sifting from files or `stdin` (CSV, TSV, whitespace, and JSON arrays of rows or dicts in the Python CLI):

```sh
# Sift a telemetry file into a compact agent-ready e-BH brief
streamsieve sift --alpha 0.05 --top 10 --warmup 32 metrics.csv

# Machine-readable JSON brief (with lossless drill-down handles) from stdin
cat metrics.csv | streamsieve sift --alpha 0.05 --top 10 --json -

# Start the JSON-RPC 2.0 stdio MCP server for agent runtimes
streamsieve mcp
# (or: python3 -m streamsieve.mcp_server)

# Verify causality, CRDT axioms, VOI zero-overlap, and Ville quarantine self-checks
streamsieve verify --json

# Print version + AVX2 capability, or run streaming throughput self-check
streamsieve version
streamsieve bench --channels 1024 --ticks 2000 --json
```

**CLI `sift` flags:**
- `--alpha FLOAT`: Anytime-valid online e-BH FDR level (default `0.05`).
- `--top INT`: Max top-ranked channels included in the brief (default `10`).
- `--warmup INT`: Causal warmup ticks for shifted-anchor Winsorized-MAD baseline estimation (default `32`, engine floor `12`).
- `--ring-capacity INT`: Per-channel raw-tick ring buffer size for lossless drill-down (Python CLI, default `256`).
- `--json`: Emit compact JSON containing `n_channels`/`channels`, `ticks`, `raw_bytes`, `n_rejected`/`ebh_rejected`, `top[]` (with `channel`, `name`, `e_value`, `log_e_mean`, `log_e_burst`, `changepoint_tick`, `mode`, `rejected_ebh`, and `handle` `ch:<id>@t=<cp>..<tick>`).

---

## 2. MCP Tool Suite (`streamsieve mcp`)

When connected via `.claude-plugin/plugin.json` or `.cursor-plugin/plugin.json` (which launch `streamsieve mcp` over `stdio`), call these stateful JSON-RPC 2.0 tools directly:

| MCP Tool | Engine | Key Arguments | Returns |
|---|---|---|---|
| `sift_stream` | `EStreamEngine` | `rows` \| `csv_text` \| `json_data`, optional `session_id`, `names`, `alpha` (`0.05`), `warmup` (`32`), `max_channels` (`10`), `ring_capacity` (`256`), `append` (`false`) | `compact_text`, `rejected_channels`, `top[]`, `handles[]` (`ch:<id>@t=<cp>..<tick>`), and `token_savings` (`compression_ratio`, `savings_pct`). |
| `fetch_raw_ticks` | `EStreamEngine` | `handle` (e.g. `"ch:3@t=180..600"`) or `channel`, optional `session_id`, `limit`, `start_tick`, `end_tick` | Exact chronological `ticks: [[tick, value], ...]` from the channel's lossless ring buffer. |
| `lattice_observe` | `EvidenceLattice` | Required `hyp_id`, `provenance_id`, `llr`; optional `lattice_id`, `merge_mode` (`"strict_origin"` \| `"max_reliability"`), `log_e`, `agent_id`, `reliability`, `timestamp_ns`, `prior_log_odds`, `agent_weight` | `was_echo` (`bool`) and deduplicated `summary` (`posterior_prob`, `total_log_odds`, `merged_e_value`, `unique_evidence`, `echoes_suppressed`). |
| `lattice_merge` | `EvidenceLattice` | `target_id` plus `source_id` and/or `source_base85`; optional `merge_mode`, `return_base85` (`true`) | Bit-exact CRDT join: `hypotheses`, `summaries`, `wire_bytes`, and optional ASCII `base85` wire payload. |
| `lattice_query` | `EvidenceLattice` | Optional `lattice_id`, `hyp_id` (omit to list all hypotheses), `include_base85` | Current deduplicated `summary` / `summaries` without mutating state. |
| `voi_allocate` | `VOISifter` | `n_channels` (on create/reset), optional `sifter_id`, `n_sites`, `ticks_per_epoch`, `noise_scales`, `observations` (`[{kind: "site_agg"\|"mean"\|"ticks", target_id, value}]`), `budget_per_agent`, `n_agents`, `allow_site_agg`, `allow_mean`, `allow_ticks`, `step_epoch`, `stay_prob` | Zero-overlap `actions[]` (`agent_slot`, `tool_kind`, `target_id`, `cost`, `expected_voi`, `current_prob`) + `top_posteriors`. |
| `trust_step` | `TrustCalibrator` | Required `action` (`"predict_prob"` \| `"predict_scalar"` \| `"resolve"` \| `"status"`); `calibrator_id`, `decision_id`, `agent_probs` or `agent_means`/`agent_stds` (`null` = abstain), `underage_cost`, `overage_cost`, `outcome`, `conformal_alpha`, `quarantine_e_thresh`, `fixed_share_alpha`, `eta` | Calibrated `decision` (`fused_prob`, `fused_point`, `ci_low`, `ci_high`, `optimal_action`, `active_agents`) and per-agent `agents[]` trust/Platt/quarantine diagnostics. |

**Multi-agent MCP orchestration recipe:**
1. Call `sift_stream` with `csv_text`/`rows`/`json_data` (use `append: true` for incremental batches) → inspect `compact_text` and `rejected_channels` (cutting context tokens by >95%).
2. Call `voi_allocate` with `noise_scales` and `n_agents` to partition deeper inspections (`site_agg`, `mean`, `ticks`) with zero overlap across agents.
3. Have workers call `fetch_raw_ticks` ONLY on flagged handles (`ch:<id>@t=<cp>..<tick>`) and record findings via `lattice_observe` using a deterministic content-derived `provenance_id`.
4. Reconcile worker lattices in any order via `lattice_merge` (by `source_id` or compact `source_base85` wire string; safe over arbitrary cyclic gossip) and inspect with `lattice_query`.
5. Combine forecasts via `trust_step(action="predict_prob" | "predict_scalar")` with operational `underage_cost` / `overage_cost`, then call `trust_step(action="resolve")` whenever delayed ground truth arrives out-of-order to update Platt shrinkage, Fixed-Share BOA weights, AgACI intervals, and Ville quarantine/shadow-recovery states.

---

## 3. Zero-Dependency Python API

The `streamsieve` Python package uses only the Python standard library (`ctypes` + `array`, zero NumPy/SciPy imports) while accepting Python lists, `array('d')`, `memoryview`, and duck-typed NumPy `float64` arrays zero-copy.

### 3.1 Firehose Sifting & Lossless Drill-Down (`sift_matrix`, `EStream`)

```python
from streamsieve import EStream, sift_matrix

# One-line batch sifting (zero-copy for contiguous float64 matrices)
est, brief = sift_matrix(
    matrix,                 # shape (n_ticks, n_channels)
    names=channel_names,    # optional list[str]
    alpha=0.05,             # anytime-valid e-BH FDR level
    warmup=32,              # causal Winsorized-MAD warmup ticks
    max_channels=10,
    ring_capacity=256,      # lossless raw-tick ring buffer per channel
)

print(brief.to_compact_text())          # >95% smaller than raw telemetry
flagged_ids = brief.rejected_channels   # exact online e-BH rejections

# Lossless raw-tick drill-down ONLY for flagged handles
for status in brief.statuses:
    if status.rejected_ebh:
        raw_pairs = est.fetch_raw(status.handle, limit=64)  # [(tick, value), ...]
```

For incremental tick-by-tick ingestion, use `EStream(n_channels, warmup=32, ring_capacity=256, alpha=0.05)` with `update_row(row)`, `update_batch(matrix)`, `update_one(channel, value)`, `ebh_reject(alpha)`, `topk(k)`, `channel_status(c)`, and `brief()`.

### 3.2 Echo-Free Multi-Agent Belief Fusion (`EvidenceLattice`)

```python
from streamsieve import EvidenceLattice

lat_a = EvidenceLattice(merge_mode="strict_origin")  # or "max_reliability"
lat_b = EvidenceLattice(merge_mode="strict_origin")

# Deterministic 64-bit provenance hash prevents echo-chamber double counting
pid = EvidenceLattice.hash_id("sensor-west-42@tick-180")

lat_a.set_prior("outage_us_west", prior_log_odds=-2.0)
was_echo = lat_a.observe(
    hyp="outage_us_west",
    provenance_id=pid,
    llr=2.4,
    log_e=2.4,        # defaults to llr if omitted
    agent_id=0,
    reliability=0.95,
    timestamp_ns=1_700_000_000_000,
)

# Agent B echoes the exact same provenance item + adds independent evidence
lat_b.observe("outage_us_west", pid, llr=2.4, agent_id=1, reliability=0.90, timestamp_ns=1_700_000_000_500)
lat_b.observe("outage_us_west", "trace-db-99@tick-181", llr=1.8, agent_id=1, timestamp_ns=1_700_000_000_600)

# Compact SSLT v1 wire sync (bytes or ASCII Base85) + idempotent CRDT join
wire_b85 = lat_b.to_base85()
lat_a.merge(EvidenceLattice.from_base85(wire_b85))
lat_a.merge(lat_b)  # idempotent: re-merging never changes log-odds or e-value!

summary = lat_a.query("outage_us_west")
# summary.posterior_prob      -> deduplicated Bayes posterior (independent unique evidence)
# summary.merged_e_value      -> Vovk-Wang admissible cross-agent merged e-value
# summary.unique_evidence     -> 2 (echo suppressed!)
# summary.echoes_suppressed   -> >= 1
```

### 3.3 Hierarchical Zero-Overlap VOI Attention (`VOISifter`)

```python
from streamsieve import VOISifter

with VOISifter(n_channels=64, n_sites=8, ticks_per_epoch=8) as sf:
    sf.set_noise_scales(channel_noise_stds)   # causal noise scales -> loud-channel immunity
    sf.observe_site_agg(site_id=0, value=1.8)
    sf.observe_mean(channel=10, value=1.5)
    sf.observe_ticks(channel=2, ticks=raw_8_ticks)  # exact sparse-burst mixture LLR

    # Zero-overlap read plan across 4 parallel agents (budget 4 units each)
    plan = sf.allocate(
        budget_per_agent=4,
        n_agents=4,
        allow_site_agg=True,
        allow_mean=True,
        allow_ticks=True,
    )
    # Each VOIAction has: agent_slot, tool_kind ('site_agg'|'channel_mean'|'channel_ticks'),
    # target_id, cost, expected_voi, current_prob
    probs = sf.posteriors()
    sf.step_epoch(stay_prob=0.99)
```

### 3.4 Delayed-Label Trust, Quarantine & Conformal Action (`TrustCalibrator`)

```python
from streamsieve import TrustCalibrator

with TrustCalibrator(
    n_agents=3,
    fixed_share_alpha=0.04,
    eta=0.8,
    quarantine_e_thresh=20.0,
    recovery_e_thresh=5.0,
    conformal_alpha=0.10,     # 90% multi-rate delayed AgACI interval
) as tc:
    # Binary probability fusion (None or NaN = agent abstains)
    dec = tc.predict_prob(
        decision_id="req-1042",
        agent_probs=[0.85, 0.99, 0.05],
        underage_cost=4.0,    # 4:1 underage:overage -> binary threshold co/(cu+co) = 0.20
        overage_cost=1.0,
    )
    # Continuous scalar fusion: tau* = cu/(cu+co) = 0.80 newsvendor quantile
    sdec = tc.predict_scalar(
        decision_id="inv-1043",
        agent_means=[102.0, 98.0, None],
        agent_stds=[4.0, 5.0, None],
        underage_cost=4.0,
        overage_cost=1.0,
    )

    # Strictly causal out-of-order resolution when ground truth arrives later
    tc.resolve("req-1042", outcome=1.0)
    tc.resolve("inv-1043", outcome=105.0)

    for ag in tc.agents():
        # ag.weight (0.0 if quarantined), ag.platt_slope (>= 0), ag.quarantine_e, ag.quarantined
        print(ag.to_dict())
```

### 3.5 Zero-Lookahead Causality Audit (`assert_causal`)

```python
from streamsieve import assert_causal, verify_estream_causality

# Overwrites all future ticks t' > probe_tick with +1e12, -1e12, NaN, +Inf
# and asserts bit-exact identical outputs at every probe_tick.
assert verify_estream_causality(matrix, warmup=32, alpha=0.05)
```

---

## 4. Operational Guardrails & Honest Boundaries

Keep these boundaries in mind when relying on `streamsieve` outputs:

- **Warmup & earliest alert tick:** `EStream` requires `warmup` ticks (default `32`, floor `12`) to freeze its shifted-anchor Winsorized-MAD baseline, plus the first snapshot at post-warmup tick `max(warmup, 24)` and `> 4` post-split ticks (`>= 8` for JZS). Under default settings, non-trivial e-values begin at tick `69`. Shifts occurring *during* warmup contaminate the baseline rather than alerting.
- **Stationary/exchangeable null requirement:** `EStream` guarantees $\mathbb{E}_{H_0}[E_\tau] \le 1$ for stationary exchangeable streams (even heavy-tailed Student-$t$, skewed LogNormal, $10^{10}$ DC offsets, or $1000\times$ loud channels). For strongly seasonal, trending, or autocorrelated raw telemetry, **pre-difference, deseasonalize, or feed one-step forecast residuals** first.
- **Wide-panel sparse-burst power:** On very wide panels ($M \ge 1000$) with few active sparse-burst channels, online e-BH requires $E_{(k^*)} \ge M / (\alpha k^*)$; ensure enough baseline ticks $n_0$ accumulate before burst onset or pair with `VOISifter` site-level triage.
- **Provenance discipline:** `EvidenceLattice` deduplicates only when duplicate observations share the same `provenance_id`. Hash canonical source event identifiers (`EvidenceLattice.hash_id(...)`). When cross-agent correlation is possible, gate decisions on `merged_e_value` (Vovk–Wang arithmetic mean) rather than assuming full independence in `posterior_prob`.
- **Delayed-label hygiene & Byzantine limit:** Never pass future ground truth into `TrustCalibrator.resolve` before it is causally observed. Keep unresolved decisions below the bounded `65,536`-entry pending table cap (oldest IDs evict first). Median reference loss tolerates up to `< 50%` simultaneously poisoned agents; if $\ge 50\%$ collude, the benchmark degrades.
- **Full mathematical specification:** See `docs/architecture.md` for exact formulas, AVX2 SoA layout, `SSLT` v1 wire format, complexity bounds, and complete failure boundaries.
