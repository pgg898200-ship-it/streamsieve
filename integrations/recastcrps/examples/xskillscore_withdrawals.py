"""Check RecastCRPS revisions against batch xskillscore for two NOAA storms."""

from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import xskillscore as xs

from recastcrps import Ensemble


def compare_cycles(frame):
    frame = frame.reset_index(drop=True)
    members = ["AC00", *(f"AP{i:02}" for i in range(1, 31))]
    # Each row is a distinct (target, initialization) pair.
    forecasts = xr.DataArray(frame[members].to_numpy(), dims=("cycle", "member"),
                             coords={"member": members})
    observations = xr.DataArray(frame.observed_kt.to_numpy(), dims="cycle")

    def batch_score(values):
        # Empty reduction dimensions preserve one empirical CRPS per cycle.
        return xs.crps_ensemble(observations, values, member_dim="member", dim=[])

    full = batch_score(forecasts)
    omitted = xr.concat(
        [batch_score(forecasts.drop_sel(member=name)) for name in members],
        dim=pd.Index(members, name="withdrawn"),
    ).transpose("cycle", "withdrawn")

    errors = []
    for _, group in frame.groupby("target_utc", sort=False):
        panel = Ensemble(range(256), slots=len(members))
        for row in group.sort_values("init_utc").itertuples():
            for slot, name in enumerate(members):
                panel.set(slot, int(getattr(row, name)))
            y = int(row.observed_kt)
            online = np.array([panel.score(y), *panel.leave_one_out(y)])
            batch = np.r_[full.values[row.Index], omitted.values[row.Index]]
            np.testing.assert_allclose(online, batch, rtol=0, atol=1e-10)
            errors.append(np.max(np.abs(online - batch)))
    return len(errors), max(errors)


if __name__ == "__main__":
    replay = Path(__file__).resolve().parents[1] / "replay"
    for storm in ("lee", "milton"):
        frame = pd.read_csv(replay / f"{storm}_gefs.csv")
        cycles, error = compare_cycles(frame)
        print(f"{storm}: {cycles} cycles, {32 * cycles} scores, max error {error:.3g} kn")
