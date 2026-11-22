# `COMPANY_STATE.md` — Autonomous Product & Organic Traction Ledger

> Operating discipline modeled on independent open-tool labs (zero paid promotion, zero astroturfing, zero spam outreach; traction earned strictly through drop-in utility, reproducible benchmarks, crisp documentation, and registry discoverability).

## 1. Current State (`2026-11-22`, v0.1.0)

- **Product:** `streamsieve` v0.1.0 — zero-dependency SIMD C++20/AVX2 runtime + `<5 ms` native CLI + Python SDK + 7-tool stdio MCP server + drop-in `river.drift` adapter (`EStreamDrift`).
- **Verification Gate:**
  - Native C++20 `-O3 -march=native -Wall -Wextra -Wpedantic`: **0 warnings**, PASS.
  - ASan + UBSan scalar build (`test-san`): **0 sanitizer errors**, bit-exact AVX2 vs scalar parity.
  - Zero-dep Python unittest + Adversarial Breaker suite: **56/56 tests passing** (26 core + 3 adapter + 27 adversarial breaker tests).
  - Affiliation hygiene audit: **0** personal/external lab references anywhere in source, docs, or git history.
- **Verified Frontier Edge (`benchmarks/results.json`):**
  - `160.75 M ch-ticks/s` native AVX2; `17.1×` faster than `river.ADWIN`, `54.4×` faster than `river.PageHinkley`, `5.65×` faster than NumPy/SciPy rolling z+BH.
  - `4.03 ms` native CLI / `24.90 ms` Python cold import (`67.9×` faster than `numpy+scipy+river`).
  - `0.004–0.010` continuous-peeking null FAR across Gaussian, Student-$t(3)$, LogNormal, and $10^{10}$ DC + 1000× loud channels.
  - `0.65–0.70` recall on zero-mean symmetric `±3.5σ` bursts (`0.000–0.022` FDR) where `ADWIN` and Welch $t$-test score `0.00`.
  - `99.99%` (`11,829×`) LLM prompt token compression (`3.84M -> 325` tokens) with `0.0` bit-exact raw-tick drill-down error.
  - `0.0%` CRDT overwrite churn (`1.00×` inflation vs `10.39×`), `10.7×` smaller `SSLT` v1 wire vs JSON debate, `100%` Byzantine quarantine, and `-30.39%` cost reduction with `90.01%` 90% AgACI coverage on real NOAA Seattle weather.

## 2. Organic Distribution Surfaces (Zero-Outreach / Pull-Based Only)

| Surface | Artifact in Repo | Why It Drives Organic Third-Party Pull | Status |
|---|---|---|---|
| **PyPI (`pip install streamsieve`)** | `pyproject.toml`, `setup.py`, `MANIFEST.in`, `.github/workflows/release.yml` | Zero-dep prebuilt wheels (`cibuildwheel`) let developers & CI adopt in 1 command | Ready for PyPI Trusted Publisher tag `v0.1.0` |
| **Drop-in `river.drift` substitution** | `python/streamsieve/adapters.py` (`EStreamDrift`) | 1-line swap (`from streamsieve import EStreamDrift as ADWIN`) with `0` deps & zero-mean burst detection | Implemented & tested |
| **MCP Directories & Registries** | `server.json`, `python/streamsieve/mcp_server.py` | Agents and MCP clients discover 7 streaming tools (`sift_stream`, `lattice_*`, `voi_allocate`, `trust_step`) | Ready for MCP Registry listing on publish |
| **Claude Code / Cursor Skills** | `SKILL.md`, `.claude-plugin/plugin.json`, `.cursor-plugin/plugin.json` | Native skill/plugin install in coding agents | Ready |
| **AEO / AI-Agent Search Indexing** | `llms.txt`, `llms-full.txt`, `docs/benchmarks.md`, `docs/comparisons/*.md` | Dated, reproducible, honest X-vs-Y tables give AI coding assistants verifiable grounds to recommend `streamsieve` where it genuinely wins | Ready |
| **Provenance & Checksums** | `LICENSE`, `NOTICE`, `SHA256SUMS` | Satisfies enterprise/OSS downstream maintainers auditing dependencies | Generated automatically per release |

## 3. Hard Operating Rules

1. **No spam, no astroturfing, no fake accounts, no unsolicited comments on external repos.**
2. **Every benchmark claim must be reproduced from `benchmarks/run_all.py` on the exact release commit.**
3. **Always document honest failure boundaries** (non-stationary trends/seasonality require residuals; fixed-sample offline tests favor `scipy.stats`; online supervised ML favors `river`).
4. **Iterate strictly on real adoption demand signals** (PyPI downloads, GitHub dependents/issues, MCP installs, third-party benchmark comparisons).

## 4. Next Highest-Leverage Iterations (Post-v0.1.0 Launch)

1. **Human operator ticket:** Provision clean unaffiliated GitHub org/account + PyPI Trusted Publisher + push tag `v0.1.0` to trigger `cibuildwheel` cross-platform wheels and GitHub Release with `SHA256SUMS`.
2. **v0.2.0 demand-driven candidates:**
   - Streaming deseasonalization / online residual pre-filter (`EStreamResidual`) so users with raw diurnal/trending Prometheus or CloudWatch metrics get out-of-the-box stationary residuals.
   - Standalone WebAssembly (`wasm32`) build of `streamsieve_core.hpp` for browser / edge-worker / Cloudflare Worker agents.
   - `prometheus` / OpenTelemetry JSONL pipe mode directly in the `<5 ms` native CLI (`streamsieve sift --promql` / `--jsonl`).
   - Independent third-party benchmark harness (`StreamSwarmBench` standalone wheel/dataset) for reproducible community evaluations.
