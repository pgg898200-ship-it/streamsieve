# Porto movement trace

The replay uses GPS samples from taxis in Porto, Portugal. Each inner array in `data/trace.json` is one trip prefix, in recording order. The coordinates are WGS84 longitude and latitude in signed integer microdegrees. UCI describes a nominal sampling cadence of 15 seconds. Compare adjacent observations within an array. A new array starts a new trip, and equal consecutive observations represent legitimate updates.

| Quantity | Count |
| --- | ---: |
| Trip prefixes | 99 |
| Positions | 4,568 |
| Within-trip moves | 4,469 |
| Stationary moves after quantization | 116 |

## Selection and reproduction

Take the first 100 data records in file order from `Porto_taxi_data_test_partial_trajectories.csv`. Keep trips flagged `MISSING_DATA=False` with at least two observations. Keep the full polyline from each selected record. Multiply each coordinate by one million and round to the nearest integer. This removes floating-point formatting noise and all identifying metadata. The benchmark uses generated rectangles. The source has no zone subscriptions or notification logs. These trip prefixes represent a historical sample from one city.

From the repository root, regenerate the file with Python 3 and compare its digest. Use `--input path/to/Porto_taxi_data_test_partial_trajectories.csv` for an offline rebuild.

```sh
python3 bench/trace_porto.py
sha256sum bench/data/trace.json
```

| File | SHA-256 |
| --- | --- |
| [Original CSV](https://archive.ics.uci.edu/ml/machine-learning-databases/00339/Porto_taxi_data_test_partial_trajectories.csv) | `f376cca6839ec7a16c46d8619b28230114b9bba5e9b0dec7e4abae44e82b542f` |
| Derived JSON | `4197da9bf36caf10235da69b073d503d2c6404c74541e5a94942225cf8c5d134` |

## Data attribution

The original creators credited by UCI are Luis Moreira-Matias, Michel Ferreira, Joao Mendes-Moreira, L. L., and J. J. (2013). *Taxi Service Trajectory – Prediction Challenge, ECML PKDD 2015*. UCI Machine Learning Repository. [Dataset record and citation](https://doi.org/10.24432/C55W25).

The original dataset and the selected, quantized trajectories in `data/trace.json` are distributed under [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/). The repository's code license applies to the sampler.
