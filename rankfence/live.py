"""Fuse asynchronous, pull-based pages. Use one iterator per stable ranking."""
import asyncio
import heapq
from dataclasses import dataclass
from typing import AsyncIterable, Sequence

from .core import Fence
from .partition import PartitionFence


@dataclass(frozen=True)
class Page:
    ids: Sequence[str]
    done: bool = False


@dataclass(frozen=True)
class Result:
    ids: tuple[str, ...]
    depths: tuple[int, ...]
    done: tuple[bool, ...]
    receipt: dict | None


async def fuse(sources: Sequence[AsyncIterable[Page]], k: int, *, c: int = 60,
               mode: str = "ordered", receipt: bool = False) -> Result:
    """Take ownership of the iterators, cancel pending pulls, and close on exit.

    Sources must cooperate with asyncio cancellation. A page marked done is the
    last page of that ranking. Natural iterator exhaustion also marks it done.
    Membership mode returns IDs sorted by string, without asserting score order.
    """
    return await _consume(sources, Fence(len(sources), k, c), k, c, mode, receipt)


async def fuse_partitioned(sources: Sequence[AsyncIterable[Page]], source_groups: Sequence[str],
                           k: int, *, c: int = 60, mode: str = "ordered",
                           receipt: bool = False, prune: bool = True) -> Result:
    """Fuse rankings with pairwise-disjoint group ID spaces.

    source_groups[i] names the partition of sources[i]. The caller guarantees
    that each ID belongs to one partition, including all unread pages. With
    prune=True the coordinator can cancel a losing partition before the global
    result is ready. Cancellation leaves its EOF flags false.
    """
    if isinstance(source_groups, (str, bytes)):
        raise TypeError("provide one group name per source")
    groups = tuple(source_groups)
    if len(groups) != len(sources):
        raise ValueError("provide one group name per source")
    if type(prune) is not bool:
        raise TypeError("prune must be a bool")
    fence = PartitionFence(groups, k, c)
    return await _consume(sources, fence, k, c, mode, receipt, groups, prune)


async def _consume(sources, fence, k, c, mode, receipt, groups=None, prune=False):
    if mode not in ("ordered", "members"):
        raise ValueError("mode must be ordered or members")
    check = fence.ordered if mode == "ordered" else fence.members
    prefixes = [[] for _ in sources] if receipt else None
    readers = []
    pending = {}
    retired = set()
    if prune:
        from .retire import retirable_sources

    async def pull(reader):
        try:
            return await anext(reader)
        except StopAsyncIteration:
            return Page((), done=True)

    async def close_reader(reader):
        close = getattr(reader, "aclose", None)
        if close is not None:
            await close()

    def result(answer):
        ids = tuple(answer) if mode == "ordered" else tuple(sorted(answer))
        proof = None if prefixes is None else {
            "format": "rankfence/1", "c": c, "k": k, "mode": mode,
            "prefixes": prefixes, "done": list(fence.done), "ids": list(ids),
        }
        if proof is not None and groups is not None:
            proof.update(format="rankfence-partition/1", source_groups=list(groups))
        return Result(ids, fence.depths, fence.done, proof)

    completed = False
    try:
        for source in sources:
            reader = aiter(source)
            if any(reader is other for other in readers):
                raise ValueError("each source requires a distinct iterator")
            readers.append(reader)
        for i, reader in enumerate(readers):
            pending[asyncio.create_task(pull(reader))] = i
        while pending:
            ready, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            resume = []
            for task in sorted(ready, key=pending.__getitem__):
                i = pending.pop(task)
                page = task.result()
                if not isinstance(page, Page):
                    raise TypeError("sources must yield Page objects")
                if isinstance(page.ids, (str, bytes)):
                    raise TypeError("expected a page of IDs")
                ids = tuple(page.ids)
                fence.push(i, ids, done=page.done)
                if prefixes is not None:
                    prefixes[i].extend(ids)
                if not page.done:
                    resume.append(i)
            answer = check()
            if answer is not None:
                completed = True
                return result(answer)
            if prune:
                losing = retirable_sources(fence) - retired
                if losing:
                    pulls = [task for task, i in pending.items() if i in losing]
                    for task in pulls:
                        task.cancel()
                    await asyncio.gather(*pulls, return_exceptions=True)
                    for task in pulls:
                        del pending[task]
                    errors = await asyncio.gather(*(close_reader(readers[i]) for i in losing),
                                                  return_exceptions=True)
                    retired.update(losing)
                    for error in errors:
                        if isinstance(error, BaseException):
                            raise error
            for i in resume:
                if i in retired:
                    continue
                pending[asyncio.create_task(pull(readers[i]))] = i
        if retired and all(closed or i in retired for i, closed in enumerate(fence.done)):
            # Only provably losing shards have unread terms. Every winner's score
            # is final. Resolve any sub-unit tie the fixed-point fast path deferred.
            order, _ = fence._lower_order()
            answer = heapq.nsmallest(k, fence._items, key=order)
            completed = True
            return result(answer)
        raise RuntimeError("completed sources did not produce a result")
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # All readers get a close attempt, including when calling aclose()
        # itself fails. Preserve a source error or caller cancellation.
        errors = await asyncio.gather(*(close_reader(reader) for i, reader in enumerate(readers)
                                        if i not in retired),
                                      return_exceptions=True)
        if completed:
            for error in errors:
                if isinstance(error, BaseException):
                    raise error
