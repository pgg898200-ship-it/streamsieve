"""Prepare release-pinned public traces for replay.cpp. See replay.md."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

CITY_HASH = "3d4b3961d5d89b501d8a40fd8d00ac743688aaa0577f9dc60d6c65f34d4bef0d"
AGENT_HASH = "f768c3a44d6c7eb02340adcb7bceaf5a451e20dc568d7a96d69f78bfb70e09a8"


def prepare_city(path):
    import duckdb

    db = duckdb.connect(config={'threads': 1, 'memory_limit': '128MB'})
    # Station and time uniquely identify each row in this source.
    result = db.execute("""
        WITH keys AS (
            SELECT id, row_number() OVER (ORDER BY id) - 1 AS key
            FROM (SELECT DISTINCT id FROM read_parquet($1))
        )
        SELECT epoch_ms(timestamp), key,
               (bikes::UBIGINT << 32) | free::UBIGINT
        FROM read_parquet($1) JOIN keys USING (id)
        ORDER BY timestamp, id
    """, [str(path)])
    while rows := result.fetchmany(32768):
        sys.stdout.write("".join(f"{t} {k} {v}\n" for t, k, v in rows))


def prepare_agents(path):
    rows = []

    def collect(session):
        for request in session["requests"]:
            rows.append((request["end_ms"], session["session_id"],
                         request["step"], request["provider_tokens"]["completion"]))
            for call in request.get("tool_calls", []):
                if call.get("subagent"):
                    collect(call["subagent"])

    with subprocess.Popen(["zstd", "-dq", "-c", str(path)], stdout=subprocess.PIPE) as proc:
        for line in proc.stdout:
            collect(json.loads(line))
        if proc.wait():
            raise ValueError("could not decompress the agent trace")
    keys = {key: idx for idx, key in enumerate(sorted({key for _, key, _, _ in rows}))}
    for end_ms, key, step, completion in sorted(rows):
        print(end_ms, keys[key], completion)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", choices=["city", "agents"])
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    with args.source.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    expected = CITY_HASH if args.trace == "city" else AGENT_HASH
    if checksum != expected:
        sys.exit(f"file does not match the published replay, got SHA-256 {checksum}")
    (prepare_city if args.trace == "city" else prepare_agents)(args.source)
