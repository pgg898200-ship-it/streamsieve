# `COMPANY_STATE.md` — Autonomous Product & Organic Traction Ledger

> Operating discipline modeled on independent open-tool labs (zero paid promotion, zero astroturfing, zero spam outreach; traction earned strictly through drop-in utility, reproducible benchmarks, crisp documentation, and registry discoverability).

## 1. Current State (`2026-11-22`, v0.2.0)

- **Product:** `streamsieve` v0.2.0 — zero-dependency 8-wide cache-tiled AVX2 C++20 runtime + `<4 ms` native CLI (`--jsonl` / CSV / `--season` / `--detrend`) + zero-dependency Python SDK (source build from `main`) + drop-in `river.drift` (`EStreamDrift`) & `scikit-learn` (`SklearnSieveDetector`) adapters + 7-tool stdio MCP server.
- **Verification Gate:**
  - Native C++20 `-O3 -march=native -ffp-contract=off -Wall -Wextra -Wpedantic`: **0 warnings**, PASS.
  - Strict `clang++ -std=c++20 -O3 -ffp-contract=off -Wall -Wextra -Wpedantic -Werror`: **0 warnings**, PASS.
  - ASan + UBSan + `float-cast-overflow` scalar build (`test-san`): **0 sanitizer errors**, 0-ULP bit-exact AVX2 vs scalar parity.
  - Windows MSVC C++20 CI + a configured 15-wheel build matrix (`Linux x86_64`, `macOS ARM64`, `Windows AMD64`, Python `3.9`–`3.13`).
  - Zero-dep Python unittest + Adversarial Breaker + v0.2.0 parity/regression suite: **79/79 tests passing** (all 13 independent breaker witnesses `W1`–`W13` resolved & locked under regression tests).
  - Affiliation hygiene audit: **0** external/personal references anywhere in source, docs, or git history.
- **Verified Frontier Edge (`benchmarks/results.json`):**
  - `255.67 M ch-ticks/s` native 8-wide AVX2 (`256×16k`), `191.20 M/s` (`1024×4k`), `146.69 M/s` (`4096×2k`); **26.9×** faster than `river.ADWIN`, **92.3×** faster than `river.PageHinkley`, **10.55×** faster than NumPy/SciPy rolling z+BH.
  - `3.87 ms` native CLI / `25.30 ms` Python cold import (**70.8×** faster than `numpy+scipy+river`, **463×** for CLI).
  - `0.004–0.010` stationary peeking null FAR across Gaussian, Student-$t(3)$, LogNormal, and $10^{10}$ DC + 1000× loud channels.
  - `0.010–0.050` non-stationary peeking null FAR across 6σ diurnal waves (`P=24`), linear trends, random walks, and diurnal+trend streams (where Welch/ADWIN/PageHinkley emit `83.5–100%` false alarms); `0.799` F1 in `1.72 ms` on 64-ch diurnal firehose (vs `0.667` / `125.5 ms` Welch and `0.618` / `11.6 ms` ADWIN).
  - `40.85 ms` even for 55× continuous peeking on 256-ch firehose (**faster than `river.ADWIN` `49.33 ms`**) with higher F1 (`0.909` vs `0.862`), lower FDR (`0.040` vs `0.054`), `0.65–0.70` zero-mean `±3.5σ` burst recall (`0.00` for ADWIN/Welch), and `0.0` loud FPs.
  - `99.99%` (`11,829×`) LLM prompt token compression (`3.84M -> 325` tokens) in `53.4 ms` with `0.0` bit-exact raw-tick drill-down error.
  - `0.0%` CRDT overwrite churn (`1.00×` inflation vs `10.39×`), `10.7×` smaller `SSLT` v1 wire vs JSON debate, `100%` Byzantine quarantine, and `-30.39%` cost reduction with `90.01%` 90% AgACI coverage on real NOAA Seattle weather.

## 2. Organic Distribution Surfaces (Zero-Outreach / Pull-Based Only)

| Surface | Artifact in Repo | Why It Drives Organic Third-Party Pull | Status |
|---|---|---|---|
| **Python distribution** | [GitHub release](https://github.com/pgg898200-ship-it/streamsieve/releases/tag/v0.1.0), `pyproject.toml`, `setup.py` | 10 CPython wheels for Linux x86-64 and macOS ARM64 (3.9–3.13), plus sdist | GitHub v0.1.0 available. Build v0.2.0 from `main`. |
| **Native CLI** | [Release assets](https://github.com/pgg898200-ship-it/streamsieve/releases/tag/v0.1.0), `Makefile` | Standalone Linux x86-64 AVX2 binary | GitHub v0.1.0 available. Build the current CLI from `main`. |
| **Drop-in `river` & `sklearn` Adapters** | `python/streamsieve/adapters.py` (`EStreamDrift`, `SklearnSieveDetector`) | 1-line swaps (`from streamsieve import EStreamDrift as ADWIN`, `SklearnSieveDetector().fit_predict(X)`) with 0 deps | Shipped & tested |
| **MCP Directories & Registries** | `server.json`, `python/streamsieve/mcp_server.py` | Agents discover 7 streaming tools (`sift_stream` with `jsonl_text`/`season_period`/`detrend_alpha`, `lattice_*`, `voi_allocate`, `trust_step`) | Local stdio supported after installation. The registry manifest anticipates a future PyPI release. |
| **Claude Code / Cursor Skills** | `SKILL.md`, `.claude-plugin/plugin.json`, `.cursor-plugin/plugin.json` | Native skill/plugin install in coding agents | Shipped |
| **AEO / AI-Agent Search Indexing** | `llms.txt`, `llms-full.txt`, `docs/benchmarks.md`, `docs/comparisons/*.md` | Dated, reproducible, honest X-vs-Y tables give AI coding assistants verifiable grounds to recommend `streamsieve` where it genuinely wins | Synced to v0.2.0 |
| **Provenance & Checksums** | `LICENSE`, `NOTICE`, `SHA256SUMS` | Satisfies downstream maintainers auditing dependencies | Generated automatically per release |

## 3. Hard Operating Rules

1. **No spam, no astroturfing, no fake accounts, no unsolicited comments on external repos.**
2. **Every benchmark claim must be reproduced from `benchmarks/run_all.py` on the exact release commit.**
3. **Always document honest failure boundaries** (`detrend_alpha=1.0` for steep linear trends vs `0 < detrend_alpha < 1` EWMA for slow drift; `3*P` seasonal warmup budget; fixed-sample offline tests favor `scipy.stats`; online supervised ML favors `river`).
4. **Iterate strictly on real adoption demand signals** (PyPI downloads, GitHub dependents/issues, MCP installs, third-party benchmark comparisons).
