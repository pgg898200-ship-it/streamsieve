# Which zone did this taxi leave?

Two consecutive GPS fixes from Porto put the taxi inside zone 15 at the first fix and inside zone 198 at the second. Five other memberships stayed the same. The map shows just the two changed rectangles, the two fixes and a straight line between them. Blue marks the entry and orange marks the exit. Select a feature to inspect its trip, step and zone ID.

[View the GeoJSON on GitHub](./porto-crossing.geojson) · [Open in geojson.io](https://geojson.io/#data=data:text/x-url,https%3A%2F%2Fraw.githubusercontent.com%2Fpgg898200-ship-it%2Fstreamsieve%2Fmain%2Fedgechime%2Fbench%2Fgis%2Fporto-crossing.geojson) · [Download for a GIS reader](https://raw.githubusercontent.com/pgg898200-ship-it/streamsieve/main/edgechime/bench/gis/porto-crossing.geojson)

## Map another update

The replay contains 4,469 moves across 99 trip prefixes. Each label compares membership at the two recorded endpoints. The actual path between fixes is unknown. A rectangle includes its west and south boundaries and excludes its east and north boundaries. A map reader draws a closed polygon, so use that half-open rule when scoring points on its border.

From this directory, run the following commands with Python 3. The two input files are pinned to the dataset revision used for the checked-in example. Change the trip and step arguments to export a different move.

```sh
mkdir -p porto-data
base=https://huggingface.co/datasets/pgg-engineering/edgechime-bench/resolve/6279138a1540a8aaa182df33195a39cb98078401
curl -fL "$base/moves.jsonl.gz" -o porto-data/moves.jsonl.gz
curl -fL "$base/zones.json" -o porto-data/zones.json
python3 map_move.py porto-data 48 8 > crossing.geojson
```

Coordinates are WGS84 longitude and latitude in degrees. An unchanged move has no rectangle features. A stationary move has coincident previous and current point features.

[Porto Geofence Membership Changes dataset and schema](https://huggingface.co/datasets/pgg-engineering/edgechime-bench) · [EdgeChime implementation](../../README.md)

## Data credit

The coordinates derive from the Taxi Service Trajectory dataset by Luis Moreira-Matias and colleagues. PGG Engineering selected and quantized trip prefixes, generated the synthetic rectangles and computed their membership labels. The exported map data are available under CC BY 4.0. [UCI source](https://doi.org/10.24432/C55W25) · [Dataset license](https://creativecommons.org/licenses/by/4.0/)
