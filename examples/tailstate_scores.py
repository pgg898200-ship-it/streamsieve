"""Reconnect to a StreamSieve score board after a worker misses updates.

From a StreamSieve checkout, build the native detector and install Tailstate:

    make lib
    python3 -m pip install https://pgg-tailstate.tancurl3.chatgpt.site/tailstate.tar.gz
    PYTHONPATH=python python3 examples/tailstate_scores.py

A C compiler is needed for the Tailstate source install. The included NOAA
Seattle file supplies one-step differences of four weather measurements.
Tailstate maintains the current e-value of each channel as an exact binary64
payload. A disconnected worker can skip superseded scores. Consumers needing
every alarm transition should use an event stream. PGG maintains both tools.
"""
import csv
import json
from pathlib import Path
import struct

from streamsieve import EStream
from tailstate import Store


FIELDS = ("precipitation", "temp_max", "temp_min", "wind")
SOURCE = Path(__file__).with_name("noaa_weather.csv")


def word(value):
    return struct.unpack(">Q", struct.pack(">d", value))[0]


def score(bits):
    return struct.unpack(">d", struct.pack(">Q", bits))[0]


def weather_differences():
    previous = None
    with SOURCE.open(newline="") as stream:
        for record in csv.DictReader(stream):
            if record["location"] != "Seattle":
                continue
            current = [float(record[field]) for field in FIELDS]
            if previous is not None:
                yield [a - b for a, b in zip(current, previous)]
            previous = current


def catch_up(store, replica, cursor):
    """Apply each two-row page before acknowledging its cursor."""
    consumed = 0
    while True:
        page = store.page(cursor, limit=2)
        if not page.changes:
            return page.cursor, consumed
        for change in page.changes:
            if change.deleted:
                replica.pop(change.key, None)
            else:
                replica[change.key] = change.value
        consumed += len(page.changes)
        cursor = page.cursor


def run():
    replica = {}
    cursor = None
    ticks = 0
    with EStream(len(FIELDS), names=FIELDS) as detector, Store(len(FIELDS)) as store:
        for ticks, row in enumerate(weather_differences(), start=1):
            detector.update_row(row)
            for key in range(detector.n_channels):
                store.put(key, word(detector.channel_status(key).e_value))
            if ticks == 64:
                cursor, _ = catch_up(store, replica, cursor)

        if cursor is None or ticks <= 64:
            raise RuntimeError("reconnect example needs more than 64 observations")
        missed_writes = store.head - cursor.seq
        cursor, received = catch_up(store, replica, cursor)
        expected = {key: word(detector.channel_status(key).e_value)
                    for key in range(detector.n_channels)}
        if replica != expected or cursor.seq != store.head or received != len(FIELDS):
            raise RuntimeError("worker's reconstructed state differs from live detector")
        return {
            "observations": ticks,
            "score_writes_while_disconnected": missed_writes,
            "rows_received_on_reconnect": received,
            "e_values": {name: score(replica[key]) for key, name in enumerate(FIELDS)},
        }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
