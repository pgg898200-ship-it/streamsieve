import asyncio
import copy
import json
import random
import subprocess
import sys
import unittest

from rankfence import Fence, Page, fuse, verify


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_certification_cancels_straggler_and_closes_every_reader(self):
        started = asyncio.Event()
        closed = set()
        cancelled = []

        async def fast(name):
            try:
                await started.wait()
                yield Page(["shared"], done=True)
                self.fail("read past explicit EOF")
            finally:
                closed.add(name)

        async def slow():
            try:
                started.set()
                await asyncio.Event().wait()
                yield Page(["unreachable"])
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            finally:
                closed.add("slow")

        result = await asyncio.wait_for(
            fuse([fast("one"), fast("two"), slow()], 1, c=1, receipt=True), 2)
        self.assertEqual(result.ids, ("shared",))
        self.assertEqual(result.depths, (1, 1, 0))
        self.assertEqual(result.done, (True, True, False))
        self.assertEqual(cancelled, [True])
        self.assertEqual(closed, {"one", "two", "slow"})
        self.assertTrue(verify(result.receipt))

    async def test_natural_eof_and_empty_result(self):
        async def ranked(items):
            for item in items:
                yield Page([item])

        for pages, expected in [([[], []], ()), ([["z"], ["a"]], ("a", "z"))]:
            with self.subTest(pages=pages):
                result = await fuse([ranked(p) for p in pages], 3, mode="members",
                                    receipt=True)
                self.assertEqual(result.ids, expected)
                self.assertEqual(result.done, (True, True))
                self.assertTrue(verify(result.receipt))

    async def test_source_failure_propagates_and_all_readers_close(self):
        started = asyncio.Event()
        closed = set()

        async def broken():
            try:
                await started.wait()
                raise ValueError("source unavailable")
                yield
            finally:
                closed.add("broken")

        async def blocked():
            try:
                started.set()
                await asyncio.Event().wait()
                yield
            finally:
                closed.add("blocked")

        with self.assertRaisesRegex(ValueError, "source unavailable"):
            await asyncio.wait_for(fuse([broken(), blocked()], 1), 2)
        self.assertEqual(closed, {"blocked", "broken"})

    async def test_reader_cleanup_failure_keeps_primary_error_and_closes_others(self):
        closed = []

        class Failed:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise ValueError("read failure")

            def aclose(self):
                raise RuntimeError("close failure")

        class Other(Failed):
            async def __anext__(self):
                await asyncio.Event().wait()

            async def aclose(self):
                closed.append(True)

        with self.assertRaisesRegex(ValueError, "read failure"):
            await asyncio.wait_for(fuse([Failed(), Other()], 1), 2)
        self.assertEqual(closed, [True])

    async def test_external_cancellation_closes_active_readers(self):
        started = [asyncio.Event(), asyncio.Event()]
        closed = set()

        async def reader(i):
            try:
                started[i].set()
                await asyncio.Event().wait()
                yield
            finally:
                closed.add(i)

        task = asyncio.create_task(fuse([reader(0), reader(1)], 1))
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in started)), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(closed, {0, 1})

    async def test_cleanup_failure_on_success_is_visible(self):
        class Source:
            def __aiter__(self):
                return self

            async def __anext__(self):
                return Page(["a"], done=True)

            async def aclose(self):
                raise RuntimeError("cleanup failed")

        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            await fuse([Source()], 1)


class ReceiptTests(unittest.TestCase):
    def test_rejects_forged_winner_order_and_unjustified_prefix(self):
        proof = {
            "format": "rankfence/1", "mode": "ordered", "c": 1, "k": 2,
            "done": [False, False], "prefixes": [["a", "b"], ["a", "b"]],
            "ids": ["a", "b"],
        }
        self.assertTrue(verify(proof))
        for key, value in [
            ("format", "other"), ("ids", ["b", "a"]), ("ids", ["a", "a"]),
            ("ids", ["a", "invented"]), ("prefixes", [["a", "b"], []]),
            ("prefixes", [["a", "b"], ["a", "a"]]), ("done", [True]),
            ("done", [False, 0]), ("k", True), ("mode", "scores"),
        ]:
            with self.subTest(key=key, value=value):
                self.assertFalse(verify({**proof, key: value}))

    def test_verifier_accepts_random_core_certificates(self):
        rng = random.Random(719)
        checked_open = 0
        for run in range(100):
            n = rng.randint(2, 5)
            k = rng.randint(1, 4)
            c = rng.choice((0, 1, 60, 2**110))
            shared = list("abcd"[:k]) if run % 2 else []
            lists = [shared + rng.sample(list("efghijk"), rng.randrange(8))
                     for _ in range(n)]
            prefixes = [[] for _ in lists]
            done = [False] * n
            f = Fence(n, k, c)
            while not all(done):
                i = rng.choice([i for i in range(n) if not done[i]])
                pos = len(prefixes[i])
                batch = lists[i][pos:pos + rng.randint(1, 3)]
                prefixes[i].extend(batch)
                done[i] = len(prefixes[i]) == len(lists[i])
                f.push(i, batch, done=done[i])
                for mode, answer in [("members", f.members()), ("ordered", f.ordered())]:
                    if answer is None:
                        continue
                    receipt = {"format": "rankfence/1", "c": c, "k": k, "mode": mode,
                               "ids": list(answer), "prefixes": prefixes, "done": done}
                    self.assertTrue(verify(receipt), receipt)
                    if answer:
                        forged = copy.copy(receipt)
                        forged["ids"] = ["bogus", *receipt["ids"][1:]]
                        self.assertFalse(verify(forged))
                    checked_open += not all(done)
        self.assertGreater(checked_open, 50)


class CliTests(unittest.TestCase):
    def invoke(self, args, data):
        return subprocess.run([sys.executable, "-m", "rankfence", *args], input=data,
                              capture_output=True, text=True, timeout=5)

    def test_pipe_receipt_round_trip_and_early_stop(self):
        lines = [
            json.dumps({"source": 0, "ids": ["x", "y"]}),
            json.dumps({"source": 1, "ids": ["x", "y"]}),
            "unconsumed trailing input",
        ]
        result = self.invoke(["fuse", "--sources", "2", "-k", "2", "--receipt"],
                             "\n".join(lines))
        self.assertEqual(result.returncode, 0, result.stderr)
        proof = json.loads(result.stdout)
        self.assertEqual(proof["ids"], ["x", "y"])
        verified = self.invoke(["verify"], result.stdout)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout), {"valid": True})
        proof["ids"].reverse()
        rejected = self.invoke(["verify"], json.dumps(proof))
        self.assertEqual(rejected.returncode, 1, rejected.stderr)
        self.assertEqual(json.loads(rejected.stdout), {"valid": False})

    def test_incomplete_invalid_json_and_invalid_pages(self):
        for data in ["", "}", "{}", '{"source":2,"ids":[]}', '{"source":0,"ids":["a"]}']:
            with self.subTest(data=data):
                result = self.invoke(["fuse", "--sources", "2", "-k", "2"], data)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr)
                self.assertFalse(result.stdout)
        malformed = self.invoke(["verify"], "{")
        self.assertEqual(malformed.returncode, 2)
        self.assertTrue(malformed.stderr)


if __name__ == "__main__":
    unittest.main()
