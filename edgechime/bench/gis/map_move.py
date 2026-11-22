"""Write one Porto geofence update as GeoJSON for a map or GIS reader."""

import argparse
import gzip
import json
import sys
from pathlib import Path


SOURCE = "https://huggingface.co/datasets/pgg-engineering/edgechime-bench"
COLORS = {"entered": "#254fc8", "exited": "#bf4a24"}


def lon_lat(x, y):
    return [x / 1_000_000, y / 1_000_000]


def feature(geometry_type, coordinates, **properties):
    return {
        "type": "Feature",
        "geometry": {"type": geometry_type, "coordinates": coordinates},
        "properties": properties,
    }


def map_move(move, zones):
    start = lon_lat(move["x0"], move["y0"])
    end = lon_lat(move["x1"], move["y1"])
    trip, step = move["trip"], move["step"]
    features = []
    for event, color in COLORS.items():
        for zone_id in move[event]:
            west, south, east, north = zones[zone_id]
            ring = [
                lon_lat(west, south), lon_lat(east, south), lon_lat(east, north),
                lon_lat(west, north), lon_lat(west, south),
            ]
            features.append(feature(
                "Polygon", [ring], title=f"Zone {zone_id} · {event}",
                event=event, zone_id=zone_id, stroke=color, fill=color,
                **{"fill-opacity": 0.16},
            ))
    if start != end:
        features.append(feature("LineString", [start, end],
                                title="Between recorded fixes", stroke="#30343c"))
    for title, point, color in [("Previous fix", start, "#536171"), ("Current fix", end, "#172b4d")]:
        features.append(feature("Point", point, title=title, **{"marker-color": color}))
    for item in features:
        item["properties"].update(trip=trip, step=step)
    return {
        "type": "FeatureCollection",
        "name": f"Porto geofence update · trip {trip}, step {step}",
        "description": "Synthetic geofences with changed endpoint membership. The line joins two recorded positions.",
        "source": SOURCE,
        "attribution": "Moreira-Matias et al., UCI Taxi Service Trajectory dataset (doi:10.24432/C55W25). PGG Engineering generated the zones, labels and map.",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "features": features,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path, help="directory containing moves.jsonl.gz and zones.json")
    parser.add_argument("trip", type=int)
    parser.add_argument("step", type=int)
    args = parser.parse_args()
    zones = json.loads((args.data_dir / "zones.json").read_text(encoding="utf-8"))
    with gzip.open(args.data_dir / "moves.jsonl.gz", "rt", encoding="utf-8") as rows:
        for row in map(json.loads, rows):
            if (row["trip"], row["step"]) == (args.trip, args.step):
                json.dump(map_move(row, zones), sys.stdout, ensure_ascii=False, indent=2)
                print()
                return
    parser.exit(1, f"No movement for trip {args.trip}, step {args.step}.\n")


if __name__ == "__main__":
    main()
