"""Run from the source checkout after installing recastcrps, pandas and xarray."""

from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from recastcrps import Ensemble


def direct_crps(values, observed):
    pair_spread = np.abs(values[:, None] - values).mean()
    return np.abs(values - observed).mean() - pair_spread / 2


def verify_cycles(forecasts, observed):
    panel = Ensemble(range(256), slots=forecasts.sizes["member"])
    for cycle in forecasts.transpose("init", "member"):
        values = cycle.values
        for slot, wind in enumerate(values):
            panel.set(slot, int(wind))
        lowest, highest = panel.withdrawal_extremes(observed)
        # Drop each member in turn, renormalizing the surviving 30 points equally.
        brute = np.array([direct_crps(np.delete(values, i), observed) for i in range(len(values))])
        np.testing.assert_allclose([lowest[1], highest[1]], [brute.min(), brute.max()])
        labels = []
        for wind, score in (lowest, highest):
            np.testing.assert_allclose(brute[values == wind], score)
            member_ids = ",".join(cycle.member.values[values == wind])
            labels.append(f"{score:.3f} ({member_ids})")
        print(f"{cycle.init.item()}  {panel.score(observed):8.3f}  {labels[0]}  {labels[1]}")


if __name__ == "__main__":
    path = Path(__file__).resolve().parents[1] / "replay" / "lee_gefs.csv"
    frame = pd.read_csv(path).query("target_utc == 2023090812").set_index("init_utc")
    winds = frame.drop(columns=["target_utc", "observed_kt"])
    forecast = xr.DataArray(winds.to_numpy(), dims=("init", "member"),
                            coords={"init": winds.index.to_numpy(), "member": winds.columns.to_numpy()})
    print("init UTC    full CRPS  min after drop (IDs)  max after drop (IDs)  [knots]")
    verify_cycles(forecast, int(frame.observed_kt.iloc[0]))
