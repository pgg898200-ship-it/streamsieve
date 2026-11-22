"""Paged, epoch-scoped state replication over the Tailstate C library."""
import ctypes as ct
from dataclasses import dataclass
from functools import lru_cache
from importlib.machinery import EXTENSION_SUFFIXES
import os
from pathlib import Path
from threading import Lock
from typing import Optional, Tuple
from uuid import uuid4
from weakref import finalize


class EpochMismatch(ValueError):
    """The cursor belongs to another store lifetime. Rebuild the replica."""


class _Row(ct.Structure):
    _fields_ = [
        ("seq", ct.c_uint64),
        ("value", ct.c_uint64),
        ("key", ct.c_uint32),
        ("deleted", ct.c_int),
    ]


class _Iter(ct.Structure):
    _fields_ = [
        ("store", ct.c_void_p),
        ("version", ct.c_uint64),
        ("next_key", ct.c_uint32),
    ]


def _library_path():
    if "TS_LIBRARY" in os.environ:
        return os.environ["TS_LIBRARY"]
    module = Path(__file__).resolve()
    for suffix in EXTENSION_SUFFIXES:
        binary = module.with_name("_tailstate_native" + suffix)
        if binary.is_file():
            return str(binary)
    binary = module.parents[1] / "build" / "libtailstate.so"
    if binary.is_file():
        return str(binary)
    raise ImportError("Tailstate needs its native runtime. Install with pip, run make "
                      "from the source checkout, or set TS_LIBRARY to the shared library.")


@lru_cache(maxsize=None)
def _library(path):
    lib = ct.CDLL(path)
    store = ct.c_void_p
    key = ct.c_uint32
    word = ct.c_uint64
    row = ct.POINTER(_Row)
    it = ct.POINTER(_Iter)
    signatures = {
        "ts_create": ([key], store),
        "ts_create_indexed": ([key], store),
        "ts_destroy": ([store], None),
        "ts_head": ([store], word),
        "ts_store_bytes": ([store], ct.c_size_t),
        "ts_put": ([store, key, word], ct.c_int),
        "ts_delete": ([store, key], ct.c_int),
        "ts_get": ([store, key, row], ct.c_int),
        "ts_begin": ([store, word, it], ct.c_int),
        "ts_resume": ([store, word, key, it], ct.c_int),
        "ts_next": ([it, row], ct.c_int),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(lib, name)
        fn.argtypes = args
        fn.restype = result
    return lib


def _unsigned(value, bits, name):
    if type(value) is not int or value < 0 or value >= 1 << bits:
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer")
    return value


def _checked(status):
    if status == -2:
        raise ValueError("key exceeds store capacity or cursor exceeds head")
    if status == -3:
        raise OverflowError("store sequence space exhausted")
    if status < 0:
        raise RuntimeError(f"Tailstate returned status {status}")
    return status


@dataclass(frozen=True)
class Cursor:
    epoch: str
    seq: int = 0
    key: Optional[int] = None

    def __post_init__(self):
        _unsigned(self.seq, 64, "sequence")
        if self.key is not None:
            _unsigned(self.key, 32, "key hint")


@dataclass(frozen=True)
class Change:
    seq: int
    key: int
    value: int
    deleted: bool


@dataclass(frozen=True)
class Page:
    changes: Tuple[Change, ...]
    cursor: Cursor


def _change(row):
    return Change(row.seq, row.key, row.value, bool(row.deleted))


class Store:
    """Thread-safe, process-local publisher. Each instance gets a fresh epoch.

    Set indexed=True to spend extra memory on seeks after a lost or stale hint.
    Use as a context manager or call close(). Create new stores after forking.
    """

    def __init__(self, capacity, *, indexed=False):
        if not _unsigned(capacity, 32, "capacity"):
            raise ValueError("capacity must be positive")
        self._lib = _library(_library_path())
        self._lock = Lock()
        self._epoch = uuid4().hex
        create = self._lib.ts_create_indexed if indexed else self._lib.ts_create
        ptr = create(capacity)
        if not ptr:
            raise MemoryError("could not allocate Tailstate index")
        self._ptr = ptr
        self._release = finalize(self, self._lib.ts_destroy, ptr)

    def __copy__(self):
        raise TypeError("Store owns a native allocation and cannot be copied")

    def __deepcopy__(self, memo):
        return self.__copy__()

    def __enter__(self):
        with self._lock:
            self._handle()
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        with self._lock:
            self._release()
            self._ptr = None

    def _handle(self):
        if self._ptr is None:
            raise RuntimeError("store is closed")
        return self._ptr

    @property
    def epoch(self):
        return self._epoch

    @property
    def head(self):
        with self._lock:
            return self._lib.ts_head(self._handle())

    @property
    def store_bytes(self):
        with self._lock:
            return self._lib.ts_store_bytes(self._handle())

    def put(self, key, value):
        with self._lock:
            _checked(self._lib.ts_put(self._handle(), _unsigned(key, 32, "key"),
                                      _unsigned(value, 64, "value")))

    def delete(self, key):
        with self._lock:
            _checked(self._lib.ts_delete(self._handle(), _unsigned(key, 32, "key")))

    def get(self, key):
        row = _Row()
        with self._lock:
            status = _checked(self._lib.ts_get(self._handle(), _unsigned(key, 32, "key"),
                                               ct.byref(row)))
        return _change(row) if status else None

    def page(self, cursor=None, limit=128):
        """Copy at most limit current changes in mutation order.

        Commit the returned cursor with the effects of all rows in the page.
        Intermediate revisions can be coalesced across pages and reconnects.
        """
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if cursor is None:
            cursor = Cursor(self.epoch)
        if cursor.epoch != self.epoch:
            raise EpochMismatch("publisher epoch changed")
        it = _Iter()
        row = _Row()
        changes = []
        with self._lock:
            ptr = self._handle()
            if cursor.key is None:
                _checked(self._lib.ts_begin(ptr, cursor.seq, ct.byref(it)))
            else:
                _checked(self._lib.ts_resume(ptr, cursor.seq, cursor.key, ct.byref(it)))
            for _ in range(limit):
                if not _checked(self._lib.ts_next(ct.byref(it), ct.byref(row))):
                    break
                changes.append(_change(row))
        if changes:
            last = changes[-1]
            cursor = Cursor(self.epoch, last.seq, last.key)
        return Page(tuple(changes), cursor)
