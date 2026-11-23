# RecastCRPS

Score a changing ensemble of point forecasts after every revision. Each agent has a stable slot. Replacing its prediction, adjusting its weight, withdrawing it, or scoring a new observed outcome costs O(log U), where U is the number of declared forecast values. The C++17 header and Python extension calculate the same weighted empirical continuous ranked probability score (CRPS).

**Which member changed the score most?** Once an outcome is known, `withdrawal_extremes(y)` finds the withdrawals that minimize and maximize CRPS for an equal-unit-weight ensemble. It checks at most four distinct forecast values, even if thousands of members are active. `leave_one_out(y)` returns every member’s score after withdrawal and also supports unequal weights.

[Replay Hurricane Lee](https://huggingface.co/spaces/pgg-engineering/recastcrps) · [Native benchmarks](bench/README.md) · [Python comparison with scoringrules](comparison/README.md) · [Replay data and provenance](replay/README.md)

[Recompute a member withdrawal in JupyterLab](https://jupyter.org/try-jupyter/lab/index.html?fromURL=https%3A%2F%2Fraw.githubusercontent.com%2Fpgg898200-ship-it%2Fstreamsieve%2Fmain%2Fintegrations%2Frecastcrps%2Freplay%2Fwithdraw-member.ipynb). Run all cells in your browser with the Python (Pyodide) kernel. [Read the executed notebook](https://nbviewer.org/github/pgg898200-ship-it/streamsieve/blob/main/integrations/recastcrps/replay/withdraw-member.ipynb).

## What we measured

On one AMD EPYC 9V74 core, a revision followed immediately by a best-and-worst withdrawal query took **202 ns** with 4,096 unit-weight forecasts. A dedicated ordered-set/Fenwick implementation took **245 ns**. Enumerating all the leave-one-out scores took **37,875 ns**. These are medians of seven run averages. For a static ensemble, the sorted-prefix comparator won, **63 ns** to **186 ns**. [Native measurements and crossover cases](bench/README.md).

## Try it

Install the [source distribution from Hugging Face](https://huggingface.co/spaces/pgg-engineering/recastcrps/resolve/main/recastcrps-0.1.0.tar.gz), install the GitHub release branch below, or build from source.

```sh
python -m pip install "recastcrps @ git+https://github.com/pgg898200-ship-it/streamsieve.git@release/recastcrps-0.1.0#subdirectory=integrations/recastcrps"
```

For a reproducible install without Git, use this line in a pip requirements file. The URL pins the Space revision and the hash pins the source archive bytes.

```text
recastcrps @ https://huggingface.co/spaces/pgg-engineering/recastcrps/resolve/8b5a3d11d5fc3671c1d471a014b386570d320eee/recastcrps-0.1.0.tar.gz#sha256=a0f29d0159932c85d52613770db944708eedd513a63395e5169a78e15130d746
```

Run `python -m pip install -r requirements.txt` in a virtual environment. If Linux reports that it cannot find `clang++` and GCC is installed, run `CC=gcc CXX=g++ python -m pip install -r requirements.txt`. Either build requires a C++17 compiler and CPython headers.

From this directory run `python -m pip install .` with a GCC or Clang C++17 toolchain and CPython development headers. The extension has no runtime package dependencies. A C++ caller can copy [recastcrps.hpp](include/recastcrps.hpp) directly.

```python
from recastcrps import Ensemble

# Three agents forecast queue depth for the same target time.
panel = Ensemble(range(101), slots=3)
panel.set(0, 20)
panel.set(1, 40)
panel.set(2, 60, weight=2)
print(panel.score(50))        # 6.25 depth units
panel.set(0, 40)             # replace agent 0's forecast
print(panel.score(50))        # 5.0
panel.erase(1)               # withdraw agent 1's forecast
```

To evaluate records retrospectively, group forecasts by the time and quantity they predict. Use the observed value once it becomes available. For a simulation with a known target, the score can also be queried during the run. Lower scores indicate that the ensemble distribution was closer to the observation.

## Attribute a revision

```python
from recastcrps import Ensemble

panel = Ensemble([10, 20, 30], slots=3)
for i, forecast in enumerate([10, 20, 30]):
    panel.set(i, forecast)
print(panel.leave_one_out(20))       # [2.5, 5.0, 2.5]
print(panel.withdrawal_extremes(20)) # ((10, 2.5), (20, 5.0))
```

The [xarray and StreamSieve examples](examples/README.md) run these calculations against real forecasts. Both queries preserve the ensemble. The first returns scores in slot order, with `None` for an inactive slot or a withdrawal that would empty the ensemble. The second requires at least two active members, each with weight one. It returns forecast values. Members predicting the same value are interchangeable for this query. Ties select the smaller forecast value. A withdrawal score describes one target and observation. Selecting a member to retire permanently requires evidence across targets. Houtekamer and colleagues used member exclusion to evaluate weather ensembles across many verifying observations in their [study of ensemble optimization](https://doi.org/10.1175/MWR-D-20-0238.1).

**Why four candidates suffice.** For unit weights, write `A(x) = sum_i |x_i − x|` for the ensemble’s total distance from x. Among candidate withdrawals x, all terms in the post-withdrawal score are constant except `H(x) = A(x) − (n−1)|x−y|`, up to a common positive scale. In a gap with L members to its left, H has slope 2L−1 on the left of the observation y, and 2L−2n+1 on the right. Between the smallest and largest active forecasts these slopes are strictly positive and strictly negative, respectively. The minimum is at an occupied endpoint. The maximum is attained at one of the occupied values bracketing y, or at the nearest endpoint when y is outside the forecast range. Prefix rank selection finds the candidates in O(log U) time and O(1) query scratch space.

## Inputs and precision

Each forecast is a signed 32-bit integer on a predeclared support. The constructor accepts arbitrary order and repeated support values. Observations may be any signed 32-bit integer. Use a declared grid such as integer knots, milliseconds, or basis points. Converting a continuous input to a coarser grid changes the forecast distribution. A positive integer member weight represents probability mass relative to the total. The sum of active weights may be at most 2,147,483,647. Each slot holds at most one forecast. Empty ensembles reject scoring. Serialize calls on a shared C++ instance.

The kernel maintains the weighted sum of absolute distances between distinct member pairs. Inserting a member adds its weighted distance from the existing ensemble. Withdrawing it subtracts the same quantity. A Fenwick tree over ordered forecast values supplies each distance sum using cumulative mass and weighted coordinates. The numerator and denominator are integers through the final floating-point conversion and division. Python returns a float. Resident storage is O(U + number of slots).

This is the empirical-distribution scoring rule. The fair finite-ensemble estimator, parametric forecast distributions, and floating-point member values have different contracts. For bulk scoring across independent targets, [scoringrules](https://scoringrules.readthedocs.io/en/latest/generated/scoringrules.crps_ensemble.html) provides array backends. [Duchemin and Obozinski](https://arxiv.org/abs/2502.05157) previously applied Fenwick trees in CRPS regression-tree learning.

## Verify

```sh
c++ -std=c++17 -O2 -Iinclude tests/test_recast.cpp -o /tmp/test-recast && /tmp/test-recast
python -m unittest discover -s tests -p test_python.py
```

The [NOAA replay](replay/README.md) checks every updated score against a direct weighted-pair calculation. The [Python comparison](comparison/README.md) includes both immediate revisions and already materialized offline batches. The [native benchmarks](bench/README.md) include an independent Fenwick implementation and a dedicated endpoint index.

To open the historical replay locally, run `python -m http.server 8000` from this directory and navigate to `http://localhost:8000/demo/`. The WebAssembly binary is included; the [build script](demo/build_wasm.sh) rebuilds it with Zig 0.14.1.
