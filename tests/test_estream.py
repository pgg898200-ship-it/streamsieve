"""Pure-stdlib unittest suite for EStream, ChannelStatus, StreamBrief, and zero-copy buffers."""

from __future__ import annotations

from array import array
import ctypes
import json
import math
import random
import sys
import unittest

import streamsieve
from streamsieve import (
    ChannelStatus,
    EStream,
    StreamBrief,
    StreamSieveError,
    parse_channel_handle,
)


class _DuckArray2D:
    """Pure-Python duck-typed NumPy-like 2D float64 array via __array_interface__."""

    def __init__(self, rows: list[list[float]]) -> None:
        self._rows = len(rows)
        self._cols = len(rows[0]) if rows else 0
        self._buf = array("d")
        for r in rows:
            self._buf.extend(r)
        addr, _ = self._buf.buffer_info()
        typestr = "<f8" if sys.byteorder == "little" else ">f8"
        self.__array_interface__ = {
            "shape": (self._rows, self._cols),
            "typestr": typestr,
            "data": (addr, True),
            "strides": None,
            "version": 3,
        }


class _DuckArray1D:
    """Pure-Python duck-typed NumPy-like 1D float64 array via __array_interface__."""

    def __init__(self, vals: list[float]) -> None:
        self._buf = array("d", vals)
        addr, n = self._buf.buffer_info()
        typestr = "<f8" if sys.byteorder == "little" else ">f8"
        self.__array_interface__ = {
            "shape": (n,),
            "typestr": typestr,
            "data": (addr, True),
            "strides": None,
            "version": 3,
        }


class TestEStream(unittest.TestCase):
    def test_version_and_capabilities(self) -> None:
        ver = streamsieve.version()
        self.assertIsInstance(ver, str)
        self.assertGreater(len(ver), 0)
        self.assertIsInstance(streamsieve.has_avx2(), bool)

    def test_lifecycle_and_validation(self) -> None:
        with self.assertRaises(StreamSieveError):
            EStream(0)

        with self.assertRaises(ValueError):
            EStream(4, names=["a", "b"])

        est = EStream(4, warmup=16, ring_capacity=32, alpha=0.05)
        self.assertEqual(est.n_channels, 4)
        self.assertEqual(est.tick_count, 0)
        self.assertEqual(est.warmup, 16)
        self.assertEqual(est.ring_capacity, 32)
        self.assertAlmostEqual(est.alpha, 0.05)

        with self.assertRaises(StreamSieveError):
            est.update_row([1.0, 2.0])

        with self.assertRaises(StreamSieveError):
            est.update_one(99, 1.0)

        with self.assertRaises(StreamSieveError):
            est.channel_status(99)

        est.close()
        est.close()  # Idempotent double-close
        with self.assertRaises(StreamSieveError):
            est.update_row([0.0, 0.0, 0.0, 0.0])

    def test_zero_copy_buffers_and_batch_row_equivalence(self) -> None:
        rng = random.Random(101)
        n_ticks, n_ch = 120, 8
        mat = [[rng.gauss(0.0, 1.0) for _ in range(n_ch)] for _ in range(n_ticks)]
        for t in range(50, n_ticks):
            mat[t][0] += 2.0
            mat[t][3] -= 1.8

        # 1. Row-by-row with mixed 1D buffer types
        with EStream(n_ch, warmup=32, ring_capacity=64) as e_row:
            for t, row in enumerate(mat):
                mod = t % 5
                if mod == 0:
                    e_row.update_row(row)
                elif mod == 1:
                    e_row.update_row(array("d", row))
                elif mod == 2:
                    e_row.update_row(memoryview(array("d", row)))
                elif mod == 3:
                    e_row.update_row((ctypes.c_double * n_ch)(*row))
                else:
                    e_row.update_row(_DuckArray1D(row))
            ref_statuses = [e_row.channel_status(c) for c in range(n_ch)]
            ref_raw = e_row.fetch_raw(0)

        # 2. Batch with list-of-lists
        with EStream(n_ch, warmup=32, ring_capacity=64) as e_list:
            e_list.update_batch(mat)
            list_statuses = [e_list.channel_status(c) for c in range(n_ch)]
            self.assertEqual(e_list.fetch_raw(0), ref_raw)

        # 3. Batch with flat 1D array('d')
        flat = array("d")
        for r in mat:
            flat.extend(r)
        with EStream(n_ch, warmup=32, ring_capacity=64) as e_arr:
            e_arr.update_batch(flat)
            arr_statuses = [e_arr.channel_status(c) for c in range(n_ch)]

        # 4. Batch with writable & read-only 1D and 2D memoryviews
        with EStream(n_ch, warmup=32, ring_capacity=64) as e_mv:
            e_mv.update_batch(memoryview(flat))
            mv_statuses = [e_mv.channel_status(c) for c in range(n_ch)]

        mv_2d = memoryview(flat).cast("B").cast("d", shape=(n_ticks, n_ch))
        with EStream(n_ch, warmup=32, ring_capacity=64) as e_mv2:
            e_mv2.update_batch(mv_2d)
            mv2_statuses = [e_mv2.channel_status(c) for c in range(n_ch)]

        ro_mv = memoryview(flat.tobytes()).cast("d")
        with EStream(n_ch, warmup=32, ring_capacity=64) as e_ro:
            e_ro.update_batch(ro_mv)
            ro_statuses = [e_ro.channel_status(c) for c in range(n_ch)]

        ro_mv2 = memoryview(flat.tobytes()).cast("d", shape=(n_ticks, n_ch))
        with EStream(n_ch, warmup=32, ring_capacity=64) as e_ro2:
            e_ro2.update_batch(ro_mv2)
            ro2_statuses = [e_ro2.channel_status(c) for c in range(n_ch)]

        # 5. Batch with duck-typed 2D __array_interface__ (zero-copy NumPy protocol)
        duck = _DuckArray2D(mat)
        with EStream(n_ch, warmup=32, ring_capacity=64) as e_duck:
            e_duck.update_batch(duck)
            duck_statuses = [e_duck.channel_status(c) for c in range(n_ch)]

        # All ingestion paths must yield bit-exact identical states!
        for other in (
            list_statuses,
            arr_statuses,
            mv_statuses,
            mv2_statuses,
            ro_statuses,
            ro2_statuses,
            duck_statuses,
        ):
            self.assertEqual(other, ref_statuses)

        # Verify empty batch is a clean no-op
        with EStream(n_ch) as e_empty:
            e_empty.update_batch([])
            self.assertEqual(e_empty.tick_count, 0)

    def test_firehose_detection_loud_channel_immunity_and_brief(self) -> None:
        rng = random.Random(42)
        n_ch = 64
        n_ticks = 450
        names = [f"metric_{i}" for i in range(n_ch)]
        mat: list[list[float]] = []

        for t in range(n_ticks):
            row = []
            for c in range(n_ch):
                scale = 5.0 if 16 <= c < 32 else 1.0  # 16 loud null channels
                x = scale * rng.gauss(0.0, 1.0)
                if t >= 150:
                    if c < 3:
                        x += 1.6 * scale  # Sustained mean shift
                    elif 3 <= c < 5 and rng.random() < 0.22:
                        x += 3.8 * scale  # Sparse bursty channel
                row.append(x)
            mat.append(row)

        with EStream(n_ch, warmup=32, ring_capacity=128, alpha=0.05, names=names) as est:
            est.update_batch(mat)
            self.assertEqual(est.tick_count, n_ticks)

            rejected = est.ebh_reject()
            self.assertTrue({0, 1, 2}.issubset(set(rejected)), f"Missed mean shifts: {rejected}")
            burst_hits = len({3, 4} & set(rejected))
            self.assertGreaterEqual(burst_hits, 1, f"Missed burst channels: {rejected}")
            loud_fps = [c for c in rejected if 16 <= c < 32]
            self.assertEqual(loud_fps, [], f"Loud null false positives: {loud_fps}")

            # Tighter alpha yields subset (monotonicity of e-BH)
            strict_rej = est.ebh_reject(alpha=0.001)
            self.assertLessEqual(len(strict_rej), len(rejected))

            # topk() returns ChannelStatus sorted by e_value descending
            top = est.topk(10)
            self.assertEqual(len(top), 10)
            self.assertIsInstance(top[0], ChannelStatus)
            for i in range(len(top) - 1):
                self.assertGreaterEqual(top[i].e_value, top[i + 1].e_value)
            self.assertEqual(est.topk(0), [])

            # brief() returns JSON-serializable StreamBrief(dict) + .to_compact_text()
            brief = est.brief(max_channels=8)
            self.assertIsInstance(brief, dict)
            self.assertIsInstance(brief, StreamBrief)
            self.assertEqual(brief["n_channels"], n_ch)
            self.assertEqual(brief["ticks"], n_ticks)
            self.assertEqual(brief["n_rejected"], len(rejected))
            self.assertEqual(brief.rejected_channels, rejected)
            self.assertEqual(len(brief["top"]), 8)

            # JSON roundtrip works natively without custom encoder
            encoded = json.dumps(brief)
            decoded = json.loads(encoded)
            self.assertEqual(decoded["n_rejected"], len(rejected))

            # Compact text contains exact lossless handle ch:<id>@t=<cp>..<tick>
            text = brief.to_compact_text()
            self.assertIn("STREAMSIEVE BRIEF", text)
            first_handle = brief["top"][0]["handle"]
            self.assertIn(first_handle, text)
            ch_id, cp, end_t = parse_channel_handle(first_handle)
            self.assertEqual(ch_id, brief["top"][0]["channel"])
            self.assertEqual(end_t, n_ticks)
            self.assertGreaterEqual(cp, 0)

            # Lossless raw drill-down using both int index and exact handle string
            raw_all = est.fetch_raw(0)
            self.assertEqual(len(raw_all), 128)
            self.assertEqual(raw_all[-1][0], n_ticks)
            self.assertEqual(raw_all[-1][1], mat[-1][0])
            self.assertEqual(raw_all[0][0], n_ticks - 128 + 1)
            self.assertEqual(raw_all[0][1], mat[n_ticks - 128][0])

            raw_lim = est.fetch_raw(0, limit=10)
            self.assertEqual(len(raw_lim), 10)
            self.assertEqual(raw_lim, raw_all[-10:])

            raw_by_handle = est.fetch_raw(brief["top"][0]["handle"])
            self.assertGreater(len(raw_by_handle), 0)
            self.assertEqual(raw_by_handle[-1][0], n_ticks)

            # reset() clears ticks, e-values, and ring buffers
            est.reset()
            self.assertEqual(est.tick_count, 0)
            self.assertEqual(est.ebh_reject(), [])
            self.assertEqual(est.fetch_raw(0), [])

    def test_update_one_ring_zero_and_nan_resilience(self) -> None:
        rng = random.Random(303)
        with EStream(3, warmup=16, ring_capacity=0) as est:
            for t in range(100):
                est.update_one(0, rng.gauss(0.0, 1.0))
                if t == 20:
                    est.update_one(1, math.nan)
                else:
                    val1 = rng.gauss(0.0, 1.0) + (3.0 if t >= 50 else 0.0)
                    est.update_one(1, val1)
                est.update_one(2, math.inf if t == 25 else rng.gauss(0.0, 1.0))
            self.assertEqual(est.fetch_raw(0), [])
            st1 = est.channel_status(1)
            st2 = est.channel_status(2)
            self.assertTrue(math.isfinite(st1.e_value))
            self.assertTrue(math.isfinite(st2.e_value))
            self.assertGreater(st1.e_value, 10.0)

    def test_handle_parser_validation(self) -> None:
        self.assertEqual(parse_channel_handle(5), (5, None, None))
        self.assertEqual(parse_channel_handle("ch:5"), (5, None, None))
        self.assertEqual(parse_channel_handle("ch:5@t=10..200"), (5, 10, 200))
        self.assertEqual(parse_channel_handle("ch:5@10..200"), (5, 10, 200))
        with self.assertRaises(ValueError):
            parse_channel_handle("bad_handle")


if __name__ == "__main__":
    unittest.main()
