# Working with forecast-verification tools

## xarray

From the RecastCRPS source directory, run `python -m pip install . pandas xarray` followed by `python examples/xarray_revisions.py`. The script reads the bundled NOAA extract into an xarray DataArray with forecast-cycle and member dimensions. For each of 12 forecast cycles before the same verifying time, it retains the native ensemble between updates and asks which single withdrawal gives the lowest and highest CRPS. It verifies both extremes by omitting each member and evaluating the pairwise definition. This example uses complete panels, equal member weights and integer knots.

The `member` dimension follows [xskillscore's ensemble interface](https://xskillscore.readthedocs.io/en/latest/api/xskillscore.crps_ensemble.html). Houtekamer et al. measured the contribution of each member by leaving it out of the Canadian ensemble Kalman filter and aggregating CRPS over many observations. The example here reports a single-target sensitivity per forecast cycle. Finding the best withdrawal after aggregating across targets requires computing contributions for each stable member across those targets. [Houtekamer et al., Monthly Weather Review (2021)](https://doi.org/10.1175/MWR-D-20-0238.1)

## StreamSieve

Run the following from the RecastCRPS source directory with StreamSieve and its native library installed. The same 31 NOAA forecasts feed a scalar fusion decision and an empirical forecast distribution. Once the historical outcome becomes available, the decision is resolved and the empirical distribution can be verified.

```python
import csv
from recastcrps import Ensemble
from streamsieve import TrustCalibrator

with open("replay/lee_gefs.csv", newline="") as data:
    row = next(r for r in csv.DictReader(data) if r["init_utc"] == "2023090806"
               and r["target_utc"] == "2023090812")
winds = [int(row[key]) for key in row if key.startswith(("AC", "AP"))]
panel = Ensemble(range(256), slots=len(winds))
for slot, wind in enumerate(winds):
    panel.set(slot, wind)

with TrustCalibrator(n_agents=len(winds)) as trust:
    decision = trust.predict_scalar(row["target_utc"], winds)
    observed = int(row["observed_kt"])
    trust.resolve(row["target_utc"], observed)
    print(round(decision.fused_point, 3), round(panel.score(observed), 3))
```

The example prints `63.387 69.461`. Both outputs are in knots. The first is the fused point prediction. The second is the score of the equally weighted forecast distribution against the eventual best-track estimate.
