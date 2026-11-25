import unittest

from rankfence import Page, fuse_partitioned, verify


class DeferredShards(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, pages, groups, *, deferred=("archive",), k=1, prune=True):
        started = []
        closed = []

        async def reader(i):
            started.append(i)
            try:
                for page in pages[i]:
                    yield page
            finally:
                closed.append(i)

        result = await fuse_partitioned([reader(i) for i in range(len(pages))],
                                        groups, k, defer_groups=deferred,
                                        prune=prune, receipt=True)
        self.assertTrue(verify(result.receipt, source_groups=groups))
        self.assertCountEqual(closed, started)
        return result, started

    async def test_certification_before_archive_first_pull(self):
        result, started = await self.run_case(
            [[Page(["a"])], [Page(["a"])], [Page(["z"])]], ["hot", "hot", "archive"])
        self.assertEqual(result.ids, ("a",))
        self.assertCountEqual(started, [0, 1])
        self.assertFalse(result.done[2])

    async def test_empty_first_wave_releases_archive(self):
        result, started = await self.run_case(
            [[], [], [Page(["z"], done=True)]], ["hot", "hot", "archive"], prune=False)
        self.assertEqual(result.ids, ("z",))
        self.assertEqual(started[-1], 2)

    async def test_two_equal_groups_must_resolve_tie_by_id(self):
        groups = ["hot", "hot", "archive", "archive"]
        result, started = await self.run_case([[Page([d], done=True)] for d in "zzaa"], groups)
        self.assertEqual(result.ids, ("a",))
        self.assertCountEqual(started, range(4))

    async def test_deferred_selection_requires_an_initial_group(self):
        for selection in [("hot", "archive"), ("typo",)]:
            with self.assertRaises(ValueError):
                await self.run_case([[], []], ["hot", "archive"], deferred=selection)
