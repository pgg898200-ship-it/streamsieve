# Tailstate

**Catch a reconnecting worker up to the latest value of each changed key.** Tailstate is an in-process C99 change feed with a Python binding. If a pool publishes 12, then 18, then 21 free slots while a scheduler is offline, the scheduler receives one row carrying 21.

Embed the index next to the service that owns the values. Write each replacement value or deletion into the index. A worker sends its saved cursor to that service, applies a page of changes to its local replica, and commits the new cursor with those changes. Your service owns transport and authoritative storage.

[Try the eight-write trace](https://pgg-tailstate.tancurl3.chatgpt.site/#workbench) · [Browse the C and Python source](https://github.com/pgg898200-ship-it/streamsieve/tree/43f8c6830a4fb7f6c4695389944ee334ee4e754c/afterkey/companions/tailstate) · [Download the input and expected pages as JSON](starter-trace.json)

## Run a worker reconnect

On Linux with Make, a C99 compiler and Python 3.8+, download the source and run the SQLite worker. The example saves one page, closes SQLite, then resumes from that saved cursor after the publisher changes two keys.

```sh
curl -fsSLO https://pgg-tailstate.tancurl3.chatgpt.site/tailstate.tar.gz
tar -xzf tailstate.tar.gz
cd tailstate
make
python3 examples/reconnect.py
```

```text
Before disconnect: {0: 10}
After reconnect (2 changes): {1: 22}
After publisher restart: {2: 30}
```

Key 1 was updated twice while the reader was disconnected. It received the final value, 22, and a deletion for key 0. The last line shows the worker rebuilding its replica after the publisher starts a new epoch. [Example source](https://github.com/pgg898200-ship-it/streamsieve/blob/43f8c6830a4fb7f6c4695389944ee334ee4e754c/afterkey/companions/tailstate/examples/reconnect.py)

## Add it to an application

| Your application | Integration |
| --- | --- |
| Python publisher and worker | Install the downloaded archive with `python3 -m pip install ./tailstate.tar.gz` in an environment with a C99 compiler and CPython development headers. Import `Store` and `Cursor` from `tailstate`. |
| C publisher | Vendor `include/tailstate.h` and `src/tailstate.c` from the source kit. Serialize calls to a shared store. |
| Native StreamSieve publisher with a Python replica | Run the [score-feed example](../../../examples/tailstate_scores.py). It commits the scores and the cursor in one SQLite transaction. |
| C++17 publisher | Use the [Afterkey header](../../) for a native C++ change feed. |

Use dense integer keys from 0 to capacity minus one. Values are unsigned 64-bit words. Each page delivers the latest revision for each changed key, oldest surviving revision first. A deletion sets `deleted` to true. Persist the entire returned cursor, including its publisher epoch. Fetching successive pages while writes continue can coalesce more changes between reads.

The service keeps one current revision per key in memory. Keep an event log for commands, increments or audit history that require every transition. [API, memory costs, and benchmark limits](https://github.com/pgg898200-ship-it/streamsieve/blob/43f8c6830a4fb7f6c4695389944ee334ee4e754c/afterkey/companions/tailstate/README.md)

## Check a consumer against the browser trace

[starter-trace.json](starter-trace.json) contains the eight writes from the workbench in sequence order, the two-row page limit, and the two expected pages from the native library. Apply all writes before reading the first page. Start with a fresh cursor and make the next read using the cursor returned by the previous one. The fixture omits the randomly generated epoch so you can compare sequences and keys across runs.

| Page | Rows as (sequence, key, value) | Last acknowledged sequence |
| --- | --- | ---: |
| 1 | (4, 2, 26), (6, 3, 70) | 6 |
| 2 | (7, 1, 49), (8, 0, 21) | 8 |

The next read returns no changes. The browser also lets you rewrite the cursor key or delete a live key between reads.
