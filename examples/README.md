# `streamsieve` Examples & Cookbooks

## Included Dataset & Runnable Scripts

| File | What It Demonstrates | Run Command |
|---|---|---|
| [`examples/jsonl_diurnal_sre.py`](jsonl_diurnal_sre.py) | Streaming SRE JSONL/NDJSON telemetry with 6σ diurnal cycles (`season_period=24`), sustained latency shift + sparse DB lock bursts, 108× brief compression, and bit-exact lossless raw drill-down | `python3 examples/jsonl_diurnal_sre.py` |
| [`examples/multi_agent_pipeline.py`](multi_agent_pipeline.py) | End-to-end 4-engine multi-agent loop: `EStream` e-BH sift → `VOISifter` zero-overlap read allocation → `EvidenceLattice` echo-free CRDT sync (`SSLT` wire) → `TrustCalibrator` Byzantine quarantine + 4:1 asymmetric action | `python3 examples/multi_agent_pipeline.py` |
| [`examples/noaa_weather.csv`](noaa_weather.csv) | 2,922 daily NOAA weather observations (Seattle & New York, 2012–2015, public domain `other-pd` via Vega Datasets) | Used in CLI & NOAA delayed-AgACI examples below |

---

## Quick CLI & Python Snippets

### 1. Native CLI Sift on JSONL Logs & Real NOAA Telemetry (`<4 ms` cold start)

```bash
# Auto-detect JSONL/NDJSON logs with diurnal deseasonalizing
tail -n 2000 metrics.jsonl | streamsieve sift --season 24 --alpha 0.05 --json -

# Detrend linear ramps or random walks (1.0 = causal first difference)
cat cpu_trend.csv | streamsieve sift --detrend 1.0 --alpha 0.05 -

# Extract Seattle numeric columns (precipitation, temp_max, temp_min, wind) and sift
awk -F, 'NR==1 || $1=="Seattle" {print $3","$4","$5","$6}' examples/noaa_weather.csv \
  | streamsieve sift --alpha 0.05 --top 4 -

# Emit strict RFC-8259 JSON brief
awk -F, 'NR==1 || $1=="Seattle" {print $3","$4","$5","$6}' examples/noaa_weather.csv \
  | streamsieve sift --alpha 0.05 --top 4 --json -
```

### 2. Real-Data Delayed-Label AgACI & Cost-Asymmetric Action (`act` Probe)

Reproduce the real NOAA Seattle `temp_max` experiment with 4 forecasters (1-day delayed causal resolution, 4:1 underage:overage cost ratio):

```bash
python3 -c '
import csv
from streamsieve import TrustCalibrator

with open("examples/noaa_weather.csv", newline="") as f:
    temps = [float(r["temp_max"]) for r in csv.DictReader(f) if r["location"] == "Seattle"]

tc = TrustCalibrator(n_agents=4, conformal_alpha=0.10)
ema, alpha_e = temps[0], 0.25
cost_opt = cost_mean = covered = n = 0.0

for t in range(1, len(temps)):
    y_prev, y_true = temps[t - 1], temps[t]
    ema = alpha_e * y_prev + (1.0 - alpha_e) * ema
    # 4 forecasters: persistence, EMA, biased (+4 C), and inverted outlier
    means = [y_prev, ema, y_prev + 4.0, -y_prev]
    stds = [2.5, 2.5, 3.0, 4.0]
    dec = tc.predict_scalar(t, means, agent_stds=stds, underage_cost=4.0, overage_cost=1.0)
    if t >= 31:
        def asym_loss(a, y):
            return 4.0 * max(0.0, y - a) + 1.0 * max(0.0, a - y)
        cost_opt += asym_loss(dec.optimal_action, y_true)
        cost_mean += asym_loss(dec.fused_point, y_true)
        covered += 1.0 if dec.ci_low <= y_true <= dec.ci_high else 0.0
        n += 1.0
    tc.resolve(t, y_true)  # 1-day delayed causal resolution

print(f"Optimal action cost: {cost_opt/n:.3f} vs Point-mean: {cost_mean/n:.3f} ({(1 - cost_opt/cost_mean)*100:.2f}% saved)")
print(f"90% AgACI coverage: {covered/n*100:.2f}% | Agents: {[(a.agent_id, round(a.weight, 3), a.quarantined) for a in tc.agents()]}")
tc.close()
'
```
