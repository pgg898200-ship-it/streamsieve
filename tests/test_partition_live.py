import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import sys
import unittest
from unittest.mock import patch

from rankfence import Page, fuse_partitioned, verify
from rankfence.__main__ import main


class PartitionLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_loser_before_resolution(self):
        waiting = asyncio.Event()
        stopped = asyncio.Event()
        closed = set()

        async def winner(source):
            try:
                await waiting.wait()
                yield Page(["a", "b"] if source == 0 else ["b", "a"])
                await asyncio.wait_for(stopped.wait(), 1)
                yield Page([], done=True)
            finally:
                closed.add(source)

        async def competitor():
            try:
                await waiting.wait()
                # Two missing positions can still change the order on the winner
                # shard, but the weak single-source shard cannot reach the cutoff.
                await asyncio.wait_for(stopped.wait(), 1)
                yield Page(["b"], done=True)
            finally:
                closed.add(2)

        async def loser():
            try:
                waiting.set()
                await asyncio.Event().wait()
                yield
            except asyncio.CancelledError:
                stopped.set()
                raise
            finally:
                closed.add(3)

        names = ["strong", "strong", "strong", "weak"]
        result = await asyncio.wait_for(
            fuse_partitioned([winner(0), winner(1), competitor(), loser()], names, 2,
                             receipt=True), 2)
        self.assertEqual(result.ids, ("b", "a"))
        self.assertEqual(result.done[3], False)
        self.assertEqual(result.depths[3], 0)
        self.assertEqual(closed, {0, 1, 2, 3})
        proof = json.loads(json.dumps(result.receipt))
        self.assertTrue(verify(proof))
        self.assertTrue(verify(proof, source_groups=names))
        self.assertFalse(verify(proof, source_groups=list(reversed(names))))
        self.assertFalse(verify({**proof, "ids": list(reversed(result.ids))}))

    async def test_disjoint_promise_rejected_on_observation(self):
        async def stream():
            yield Page(["collision"], done=True)
        with self.assertRaisesRegex(ValueError, "different source groups"):
            await fuse_partitioned([stream(), stream()], ["A", "B"], 2)

    async def test_conservative_fast_path_after_retirement(self):
        waiting = asyncio.Event()

        async def same_group():
            await waiting.wait()
            yield Page(["z", "a"], done=True)

        async def loser():
            waiting.set()
            await asyncio.Event().wait()
            yield

        # The two candidates' lower-score floors tie. Their exact scores differ.
        result = await asyncio.wait_for(
            fuse_partitioned([same_group(), same_group(), loser()], ["A", "A", "B"],
                             1, c=2**60, receipt=True), 2)
        self.assertEqual(result.ids, ("z",))
        self.assertTrue(verify(result.receipt))

    async def test_membership_pruning_disabled_and_shape_validation(self):
        async def rows(doc):
            yield Page([doc], done=True)
        groups = ["A", "A", "B"]
        r = await fuse_partitioned([rows("w"), rows("w"), rows("x")], groups, 2,
                                   mode="members", receipt=True, prune=False)
        self.assertEqual(set(r.ids), {"w", "x"})
        self.assertTrue(verify(r.receipt))
        with self.assertRaisesRegex(ValueError, "one group name"):
            await fuse_partitioned([rows("a")], [], 1)
        with self.assertRaises(TypeError):
            await fuse_partitioned([rows("a")], ["A"], 1, prune=1)


class PartitionReceiptTests(unittest.TestCase):
    def test_verifier_reconstructs_bounds_and_topology(self):
        proof = {
            "format": "rankfence-partition/1", "c": 60, "k": 1, "mode": "ordered",
            "source_groups": ["w", "e", "w", "e"], "done": [False] * 4,
            "prefixes": [["w"], ["a"], ["w"], ["b"]], "ids": ["w"],
        }
        self.assertTrue(verify(proof))
        for field, bad in [
            ("source_groups", ["w"] * 4), ("source_groups", ["w", "e"]),
            ("source_groups", ["w", False, "w", "e"]), ("prefixes", [["w"], ["w"], ["w"], ["b"]]),
            ("prefixes", [["w"], [], [], []]), ("ids", ["b"]), ("format", "rankfence/1"),
        ]:
            with self.subTest(field=field, bad=bad):
                self.assertFalse(verify({**proof, field: bad}))

    def test_cli_pipe_and_pin_layout(self):
        groups = ["w", "e", "w", "e"]
        pages = "\n".join(json.dumps({"source": i, "ids": [v]})
                          for i, v in enumerate(["w", "a", "w", "b"]))
        def invoke(args, body):
            output = io.StringIO()
            with patch.object(sys, "stdin", io.StringIO(body)), redirect_stderr(io.StringIO()), redirect_stdout(output):
                status = main(args)
            return status, output.getvalue()
        code, proof = invoke(["fuse", "--groups", *groups, "-k", "1", "--receipt"], pages)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(proof)["source_groups"], groups)
        self.assertEqual(invoke(["verify", "--groups", *groups], proof)[0], 0)
        self.assertEqual(invoke(["verify", "--groups", *reversed(groups)], proof)[0], 1)
