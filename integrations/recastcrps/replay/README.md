# Replaying Hurricane Lee's intensity forecasts

This fixture follows 31 NOAA Global Ensemble Forecast System members as they revise Hurricane Lee's forecast maximum sustained wind for the same valid time. Each member occupies a stable slot. The reference outcome is the National Hurricane Center's post-storm best-track intensity, in knots. For background see the [NHC storm report](https://www.nhc.noaa.gov/data/tcr/AL132023_Lee.pdf).

From the repository root, run the offline replay:

```sh
c++ -std=c++17 -O2 -Iinclude replay/replay.cpp -o /tmp/recast-replay
/tmp/recast-replay replay/lee_gefs.csv
```

Expected output:

```text
targets=11 cycles=124 members=31 verified_updates=3844 revised_values=3320 unchanged_resubmissions=183
mean_complete_cycle_crps_kt=19.50197207
```

For each of 11 target times, the replay starts an empty ensemble with integer support 0–255 knots. It walks forecast cycles in initialization-time order, inserting or replacing each equally weighted member. It compares every score to a separate evaluation of the empirical CRPS definition that explicitly sums all unordered member pairs. The reported mean weights each complete forecast cycle equally. One storm is sufficient to test the code's output on actual revisions. Estimating forecast skill across storms requires a broader sample.

**Time interpretation.** The archive gives cycle initialization times. It does not record when each member arrived, and the verifying best-track estimate was assigned retrospectively. The replay applies members in a fixed ID order within each cycle. Intermediate scores describe these simulated partial updates. The last score of each cycle describes the archived 31-member ensemble.

**Withdrawal example.** At the September 8, 2023, 06 UTC initialization for 12 UTC that day, the control member forecast 63 knots and the verifying intensity was 135 knots. The complete ensemble scored 66752/961 = 69.46097815 knots. Dropping the control and renormalizing the other 30 equally weighted members gives 10411/150 = 69.40666667 knots, a change of −0.05431148 knots for that outcome.

## Source and transformation

Forecasts come from the [NOAA ATCF a-deck for AL132023](https://ftp.nhc.noaa.gov/atcf/archive/2023/aal132023.dat.gz). Verifying intensities come from the [associated b-deck](https://ftp.nhc.noaa.gov/atcf/archive/2023/bal132023.dat.gz). The [ATCF field definitions](https://science.nrlmry.navy.mil/atcf/docs/database/new/abrdeck.html) and the [NHC technique list](https://ftp.nhc.noaa.gov/atcf/docs/nhc_techlist.dat) document the original columns and member IDs. Credit for the forecast and best-track values belongs to NOAA/NWS/NCEP and NOAA/NWS/NHC. NWS identifies its data as public domain unless specifically annotated. The transformed CSV is a project-generated extract. [NWS terms](https://www.nhc.noaa.gov/disclaimer.shtml)

| CSV field | Source |
| --- | --- |
| `target_utc` | Forecast initialization plus ATCF `TAU` hours, in UTC YYYYMMDDHH format |
| `init_utc` | A-deck forecast initialization `YYYYMMDDHH`, UTC |
| `observed_kt` | B-deck `VMAX` for `BEST`, zero lead and no off-hour minute value, at target UTC |
| `AC00`, `AP01` … `AP30` | A-deck `VMAX` for the GEFS control and 30 perturbed members, in integer knots |

The extraction selects daily 12 UTC verification times from September 6 through September 16, 2023, and leads of 6 to 72 hours in six-hour steps. It keeps cycles with positive wind estimates from every member, dropping 8 of the 132 possible cycles. Repeated wind-radii rows for the same cycle, member and lead must agree on intensity. Rows are sorted by target and initialization. All targets are grouped independently and no forecast value is imputed.

To reproduce the CSV from the upstream archives, run `python3 replay/extract_lee.py`. The extractor checks both uncompressed inputs before writing. It needs only Python's standard library and internet access.

| File | SHA-256 |
| --- | --- |
| Uncompressed NOAA a-deck | `0ed6ffa50154f6055f8a3cba4db475e3b4a42e99e61080c9b811809204556cbe` |
| Uncompressed NOAA b-deck | `ca9ae215e737d32cd13300fbeab900d75ab84c6d915f51f87e236c3cc46ee958` |
| `lee_gefs.csv` | `3314ea7d0e64f5b38ee5d92be288c886ea9bcc4aec99ff0d88712979c5b8ce2a` |
