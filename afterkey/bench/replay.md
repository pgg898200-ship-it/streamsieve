# Catch-up on two recorded streams

Each row below is one replay of a public trace with a simulated reader. Keys and timestamps come from the source. At every nonempty poll the harness checks the exact ordered sequence of revisions, keys and values against a forward log scan with a dense last-revision array. Both implementations emit the same rows. All 15,615 nonempty polls passed.

| Trace | Poll interval | Keys | Nonempty polls | Log records inspected | Latest rows emitted | Slots in a full map scan |
|---|---:|---:|---:|---:|---:|---:|
| Paris bike availability | 1 min | 1,489 | 14,344 | 5,143,865 | 5,143,771 | 21,358,216 |
| Paris bike availability | 1 hr | 1,489 | 720 | 5,143,865 | 895,673 | 1,072,080 |
| Paris bike availability | 1 day | 1,489 | 30 | 5,143,865 | 43,683 | 44,670 |
| Agent session progress | 1 min | 25 | 496 | 1,375 | 643 | 12,400 |
| Agent session progress | 1 hr | 25 | 23 | 1,375 | 64 | 575 |
| Agent session progress | 1 day | 25 | 2 | 1,375 | 28 | 50 |

The Paris minute reader saw just 94 superseded writes across the month. The hourly reader saw 5.74 log records per emitted row. At daily intervals nearly every station changed, making a full map scan competitive on inspection count. The full-map column is capacity times nonempty polls and excludes the cost of collecting and sorting by revision. Afterkey's ordered iteration can walk the changed suffix twice. These figures count workload and output, not elapsed time or CPU instructions.

## Run the replays

From the Afterkey directory with Python 3.11+, a C++17 compiler and curl:

~~~sh
c++ -std=c++17 -O3 -Iinclude bench/replay.cpp -o /tmp/afterkey-replay

# Agent progress. Requires the zstd CLI.
curl -fL 'https://huggingface.co/datasets/harvardMadsys/freeinference_agentic_trace/resolve/bb26cf72c9c20f2b05f765d88548156a74d3c253/release/2026-05-17_2026-05-23/traces/2026-05-17.jsonl.zst' -o /tmp/afterkey-agents.jsonl.zst
python3 bench/replay_prepare.py agents /tmp/afterkey-agents.jsonl.zst > /tmp/afterkey-agents.tsv
/tmp/afterkey-replay /tmp/afterkey-agents.tsv

# Station availability. The compressed download is 41 MB.
python3 -m venv /tmp/afterkey-replay-env
/tmp/afterkey-replay-env/bin/pip install duckdb==1.4.1
curl -fL 'https://data.citybik.es/dumps/by-network/2025/202509-velib-stats.parquet' -o /tmp/afterkey-city.parquet
/tmp/afterkey-replay-env/bin/python bench/replay_prepare.py city /tmp/afterkey-city.parquet > /tmp/afterkey-city.tsv
/tmp/afterkey-replay /tmp/afterkey-city.tsv
~~~

The preparation script verifies each original file's SHA-256. The output format is a space-separated line with epoch milliseconds, dense key and unsigned 64-bit value. Each simulation groups events into UTC-aligned intervals and flushes the last partial bucket. Empty intervals generate no work in the comparison. The replay reads the prepared file into memory, which lets its oracle inspect original events independently of Afterkey. A production reader can discard consumed history when its delivery guarantees allow it.

## Sources and transformations

**CityBikes.** Every September 2025 Paris Vélib’ record contributes a write keyed by station ID. The value packs available bikes and free docks into one integer. Ties between stations are resolved by station ID. A repeated value still advances the revision. Source data and generated row files are downloaded or generated locally. Bike-share data by CityBikes contributors and available from [https://data.citybik.es](https://data.citybik.es/). CityBikes contributors. (2026) Bike-share data retrieved from [https://data.citybik.es](https://data.citybik.es/). The source describes the compilation's ODbL 1.0 license and underlying operator terms.

**FreeInference.** The May 17 partition contributes one write per completed LLM request, including nested subagent sessions. The session is the key and the most recent completion-token count is the value. Completion timestamps set the order, with session ID and step breaking ties. Some sessions finish on the following date. This models a dashboard watching agent progress. The recording contains neither the modeled subscriber nor its polling schedule. William Nixon, Muxin Tian, Yunjia Zheng, Haryadi S. Gunawi and Juncheng Yang, *From Requests to Sessions: A Large-Scale Characterization of Human-Driven Agentic Workloads* (2026). [Dataset, CC BY 4.0](https://huggingface.co/datasets/harvardMadsys/freeinference_agentic_trace) · [Paper](https://doi.org/10.5281/zenodo.23903440)
