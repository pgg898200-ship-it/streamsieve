"""Pure-stdlib ctypes bindings for the frozen streamsieve C ABI (streamsieve_capi.h).

Zero third-party imports. Supports zero-copy bulk double buffer extraction from
``array('d')``, ``memoryview``, ``ctypes`` arrays, and duck-typed NumPy arrays
(via ``__array_interface__``) without importing NumPy.
"""

from __future__ import annotations

from array import array
import ctypes
from ctypes import (
    CDLL,
    POINTER,
    Structure,
    c_char,
    c_char_p,
    c_double,
    c_int,
    c_size_t,
    c_uint8,
    c_uint32,
    c_uint64,
    c_void_p,
)
import math
import os
import sys
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# Status codes & constants (mirroring csrc/streamsieve_capi.h)
# ---------------------------------------------------------------------------

SS_OK = 0
SS_ERR_NULL = -1
SS_ERR_INVALID_ARG = -2
SS_ERR_OOM = -3
SS_ERR_BOUNDS = -4
SS_ERR_PARSE = -5
SS_ERR_BUFFER_SMALL = -6

SS_MERGE_STRICT_ORIGIN = 0
SS_MERGE_MAX_RELIABILITY = 1

_STATUS_NAMES = {
    SS_OK: "SS_OK",
    SS_ERR_NULL: "SS_ERR_NULL (null pointer)",
    SS_ERR_INVALID_ARG: "SS_ERR_INVALID_ARG (invalid argument)",
    SS_ERR_OOM: "SS_ERR_OOM (out of memory)",
    SS_ERR_BOUNDS: "SS_ERR_BOUNDS (index or id out of bounds)",
    SS_ERR_PARSE: "SS_ERR_PARSE (malformed wire payload)",
    SS_ERR_BUFFER_SMALL: "SS_ERR_BUFFER_SMALL (output buffer too small)",
}

_NATIVE_F8_TYPESTRS = ("<f8", "=f8") if sys.byteorder == "little" else (">f8", "=f8")
_DOUBLE_PTR = POINTER(c_double)
_UINT8_PTR = POINTER(c_uint8)
_UINT32_PTR = POINTER(c_uint32)
_UINT64_PTR = POINTER(c_uint64)
_INT_PTR = POINTER(c_int)
_SIZE_PTR = POINTER(c_size_t)


class StreamSieveError(RuntimeError):
    """Exception raised when the native streamsieve C library returns an error."""

    def __init__(self, code: int, message: str = "") -> None:
        self.code = int(code)
        label = _STATUS_NAMES.get(self.code, f"SS_ERR({self.code})")
        full = f"{label}: {message}" if message else label
        super().__init__(full)


def check_status(rc: int, context: str = "") -> None:
    """Raise :class:`StreamSieveError` if ``rc != SS_OK``."""
    if rc != SS_OK:
        raise StreamSieveError(rc, context)


# ---------------------------------------------------------------------------
# C ABI Struct definitions (exact layout parity with csrc/streamsieve_capi.h)
# ---------------------------------------------------------------------------


class SSEStreamConfig(Structure):
    _fields_ = [
        ("n_channels", c_uint32),
        ("warmup", c_uint32),
        ("ring_capacity", c_uint32),
        ("alpha", c_double),
        ("burst_tail_p0", c_double),
        ("ewma_reset_decay", c_double),
        ("two_sided", c_uint8),
    ]


class SSChannelStatus(Structure):
    _fields_ = [
        ("channel", c_uint32),
        ("changepoint_tick", c_uint32),
        ("e_value", c_double),
        ("log_e_mean", c_double),
        ("log_e_burst", c_double),
        ("z_score", c_double),
        ("baseline_loc", c_double),
        ("baseline_scale", c_double),
        ("mode", c_uint8),
        ("rejected_ebh", c_uint8),
    ]


class SSEvidenceItem(Structure):
    _fields_ = [
        ("hyp_id", c_uint64),
        ("provenance_id", c_uint64),
        ("llr", c_double),
        ("log_e", c_double),
        ("agent_id", c_uint32),
        ("reliability_q16", c_uint32),
        ("timestamp_ns", c_uint64),
    ]


class SSHypSummary(Structure):
    _fields_ = [
        ("hyp_id", c_uint64),
        ("posterior_prob", c_double),
        ("total_log_odds", c_double),
        ("merged_e_value", c_double),
        ("merged_log_e", c_double),
        ("unique_evidence", c_uint32),
        ("echoes_suppressed", c_uint32),
        ("distinct_agents", c_uint32),
    ]


class SSSifterConfig(Structure):
    _fields_ = [
        ("n_channels", c_uint32),
        ("n_sites", c_uint32),
        ("prior_active", c_double),
        ("hot_site_prior", c_double),
        ("hot_site_boost", c_double),
        ("shift_snr", c_double),
        ("burst_prob", c_double),
        ("burst_snr", c_double),
        ("ticks_per_epoch", c_uint32),
    ]


class SSVOIAction(Structure):
    _fields_ = [
        ("agent_slot", c_uint32),
        ("tool_kind", c_uint8),
        ("target_id", c_uint32),
        ("cost", c_uint32),
        ("expected_voi", c_double),
        ("current_prob", c_double),
    ]


class SSTrustConfig(Structure):
    _fields_ = [
        ("n_agents", c_uint32),
        ("fixed_share_alpha", c_double),
        ("eta", c_double),
        ("quarantine_e_thresh", c_double),
        ("recovery_e_thresh", c_double),
        ("platt_lr", c_double),
        ("platt_l2", c_double),
        ("conformal_alpha", c_double),
    ]


class SSAgentTrust(Structure):
    _fields_ = [
        ("agent_id", c_uint32),
        ("weight", c_double),
        ("platt_slope", c_double),
        ("platt_bias", c_double),
        ("ema_brier", c_double),
        ("quarantine_e", c_double),
        ("quarantined", c_uint8),
        ("n_resolved", c_uint64),
    ]


class SSTrustDecision(Structure):
    _fields_ = [
        ("fused_prob", c_double),
        ("fused_point", c_double),
        ("ci_low", c_double),
        ("ci_high", c_double),
        ("optimal_action", c_double),
        ("active_agents", c_uint32),
    ]


# ---------------------------------------------------------------------------
# Shared library discovery & binding setup
# ---------------------------------------------------------------------------

_LIB: CDLL | None = None
_LIB_PATH: str | None = None


def _candidate_library_paths() -> list[str]:
    import glob

    env_path = os.environ.get("STREAMSIEVE_LIB")
    candidates: list[str] = []
    if env_path:
        candidates.append(env_path)

    if sys.platform == "darwin":
        names = ("_libstreamsieve.dylib", "libstreamsieve.dylib", "_libstreamsieve.so")
        globs = ("_libstreamsieve*.so", "_libstreamsieve*.dylib")
    elif sys.platform == "win32":
        names = ("_libstreamsieve.dll", "libstreamsieve.dll")
        globs = ("_libstreamsieve*.pyd", "_libstreamsieve*.dll")
    else:
        names = ("_libstreamsieve.so", "libstreamsieve.so", "_libstreamsieve.dylib")
        globs = ("_libstreamsieve*.so",)

    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    for name in names:
        candidates.append(os.path.join(pkg_dir, name))
    for pattern in globs:
        for hit in sorted(glob.glob(os.path.join(pkg_dir, pattern))):
            if hit not in candidates:
                candidates.append(hit)

    repo_root = os.path.abspath(os.path.join(pkg_dir, os.pardir, os.pardir))
    build_dir = os.path.join(repo_root, "build")
    for name in names:
        candidates.append(os.path.join(build_dir, name))

    return candidates


def _configure_signatures(lib: CDLL) -> None:
    # Version & CPU capabilities
    lib.ss_version.argtypes = []
    lib.ss_version.restype = c_char_p

    lib.ss_has_avx2.argtypes = []
    lib.ss_has_avx2.restype = c_int

    # 1. EStreamEngine
    lib.ss_estream_default_config.argtypes = [POINTER(SSEStreamConfig), c_uint32]
    lib.ss_estream_default_config.restype = None

    lib.ss_estream_create.argtypes = [POINTER(SSEStreamConfig)]
    lib.ss_estream_create.restype = c_void_p

    lib.ss_estream_destroy.argtypes = [c_void_p]
    lib.ss_estream_destroy.restype = None

    lib.ss_estream_reset.argtypes = [c_void_p]
    lib.ss_estream_reset.restype = c_int

    lib.ss_estream_update_row.argtypes = [c_void_p, _DOUBLE_PTR, c_uint32]
    lib.ss_estream_update_row.restype = c_int

    lib.ss_estream_update_batch.argtypes = [c_void_p, _DOUBLE_PTR, c_uint32, c_uint32]
    lib.ss_estream_update_batch.restype = c_int

    lib.ss_estream_update_one.argtypes = [c_void_p, c_uint32, c_double]
    lib.ss_estream_update_one.restype = c_int

    lib.ss_estream_ebh_reject.argtypes = [
        c_void_p,
        c_double,
        _UINT32_PTR,
        c_uint32,
        _UINT32_PTR,
    ]
    lib.ss_estream_ebh_reject.restype = c_int

    lib.ss_estream_topk.argtypes = [
        c_void_p,
        c_uint32,
        POINTER(SSChannelStatus),
        _UINT32_PTR,
    ]
    lib.ss_estream_topk.restype = c_int

    lib.ss_estream_get_channel.argtypes = [c_void_p, c_uint32, POINTER(SSChannelStatus)]
    lib.ss_estream_get_channel.restype = c_int

    lib.ss_estream_fetch_raw.argtypes = [
        c_void_p,
        c_uint32,
        _DOUBLE_PTR,
        _UINT32_PTR,
        c_uint32,
        _UINT32_PTR,
    ]
    lib.ss_estream_fetch_raw.restype = c_int

    lib.ss_estream_n_channels.argtypes = [c_void_p]
    lib.ss_estream_n_channels.restype = c_uint32

    lib.ss_estream_tick_count.argtypes = [c_void_p]
    lib.ss_estream_tick_count.restype = c_uint64

    # 2. EvidenceLattice
    lib.ss_lattice_create.argtypes = [c_int]
    lib.ss_lattice_create.restype = c_void_p

    lib.ss_lattice_destroy.argtypes = [c_void_p]
    lib.ss_lattice_destroy.restype = None

    lib.ss_lattice_set_prior.argtypes = [c_void_p, c_uint64, c_double]
    lib.ss_lattice_set_prior.restype = c_int

    lib.ss_lattice_set_agent_weight.argtypes = [c_void_p, c_uint32, c_double]
    lib.ss_lattice_set_agent_weight.restype = c_int

    lib.ss_lattice_observe.argtypes = [c_void_p, POINTER(SSEvidenceItem), _INT_PTR]
    lib.ss_lattice_observe.restype = c_int

    lib.ss_lattice_merge.argtypes = [c_void_p, c_void_p]
    lib.ss_lattice_merge.restype = c_int

    lib.ss_lattice_query.argtypes = [c_void_p, c_uint64, POINTER(SSHypSummary)]
    lib.ss_lattice_query.restype = c_int

    lib.ss_lattice_list_hyps.argtypes = [c_void_p, _UINT64_PTR, c_uint32, _UINT32_PTR]
    lib.ss_lattice_list_hyps.restype = c_int

    lib.ss_lattice_serialize.argtypes = [c_void_p, _UINT8_PTR, c_size_t, _SIZE_PTR]
    lib.ss_lattice_serialize.restype = c_int

    lib.ss_lattice_deserialize.argtypes = [_UINT8_PTR, c_size_t]
    lib.ss_lattice_deserialize.restype = c_void_p

    lib.ss_hash_provenance.argtypes = [_UINT8_PTR, c_size_t]
    lib.ss_hash_provenance.restype = c_uint64

    # 3. VOISifter
    lib.ss_sifter_default_config.argtypes = [POINTER(SSSifterConfig), c_uint32, c_uint32]
    lib.ss_sifter_default_config.restype = None

    lib.ss_sifter_create.argtypes = [POINTER(SSSifterConfig)]
    lib.ss_sifter_create.restype = c_void_p

    lib.ss_sifter_destroy.argtypes = [c_void_p]
    lib.ss_sifter_destroy.restype = None

    lib.ss_sifter_set_noise_scales.argtypes = [c_void_p, _DOUBLE_PTR, c_uint32]
    lib.ss_sifter_set_noise_scales.restype = c_int

    lib.ss_sifter_observe_site_agg.argtypes = [c_void_p, c_uint32, c_double]
    lib.ss_sifter_observe_site_agg.restype = c_int

    lib.ss_sifter_observe_mean.argtypes = [c_void_p, c_uint32, c_double]
    lib.ss_sifter_observe_mean.restype = c_int

    lib.ss_sifter_observe_ticks.argtypes = [c_void_p, c_uint32, _DOUBLE_PTR, c_uint32]
    lib.ss_sifter_observe_ticks.restype = c_int

    lib.ss_sifter_allocate.argtypes = [
        c_void_p,
        c_uint32,
        c_uint32,
        c_uint32,
        c_uint32,
        c_uint32,
        POINTER(SSVOIAction),
        c_uint32,
        _UINT32_PTR,
    ]
    lib.ss_sifter_allocate.restype = c_int

    lib.ss_sifter_posteriors.argtypes = [c_void_p, _DOUBLE_PTR, c_uint32]
    lib.ss_sifter_posteriors.restype = c_int

    lib.ss_sifter_step_epoch.argtypes = [c_void_p, c_double]
    lib.ss_sifter_step_epoch.restype = c_int

    # 4. TrustCalibrator
    lib.ss_trust_default_config.argtypes = [POINTER(SSTrustConfig), c_uint32]
    lib.ss_trust_default_config.restype = None

    lib.ss_trust_create.argtypes = [POINTER(SSTrustConfig)]
    lib.ss_trust_create.restype = c_void_p

    lib.ss_trust_destroy.argtypes = [c_void_p]
    lib.ss_trust_destroy.restype = None

    lib.ss_trust_predict_prob.argtypes = [
        c_void_p,
        c_uint64,
        _DOUBLE_PTR,
        c_uint32,
        c_double,
        c_double,
        POINTER(SSTrustDecision),
    ]
    lib.ss_trust_predict_prob.restype = c_int

    lib.ss_trust_predict_scalar.argtypes = [
        c_void_p,
        c_uint64,
        _DOUBLE_PTR,
        _DOUBLE_PTR,
        c_uint32,
        c_double,
        c_double,
        POINTER(SSTrustDecision),
    ]
    lib.ss_trust_predict_scalar.restype = c_int

    lib.ss_trust_resolve.argtypes = [c_void_p, c_uint64, c_double]
    lib.ss_trust_resolve.restype = c_int

    lib.ss_trust_get_agents.argtypes = [c_void_p, POINTER(SSAgentTrust), c_uint32]
    lib.ss_trust_get_agents.restype = c_int


def get_lib() -> CDLL:
    """Return the lazily loaded native ``_libstreamsieve`` shared library."""
    global _LIB, _LIB_PATH
    if _LIB is not None:
        return _LIB

    tried: list[str] = []
    for path in _candidate_library_paths():
        tried.append(path)
        if os.path.isfile(path):
            lib = CDLL(path)
            _configure_signatures(lib)
            _LIB = lib
            _LIB_PATH = path
            return lib

    raise OSError(
        "Unable to locate compiled streamsieve native library. Checked:\n  "
        + "\n  ".join(tried)
        + "\nBuild it with `make` or set STREAMSIEVE_LIB=/path/to/_libstreamsieve.so."
    )


def library_path() -> str:
    """Return the resolved filesystem path to the loaded shared library."""
    get_lib()
    assert _LIB_PATH is not None
    return _LIB_PATH


def version() -> str:
    """Return the native streamsieve runtime version string."""
    raw = get_lib().ss_version()
    return raw.decode("utf-8") if raw else "unknown"


def has_avx2() -> bool:
    """Return True if the compiled binary & host CPU support AVX2+FMA."""
    return bool(get_lib().ss_has_avx2())


def hash_provenance(data: bytes | bytearray | memoryview | str) -> int:
    """Compute the deterministic 64-bit SplitMix64 provenance hash via native C API."""
    if isinstance(data, str):
        raw = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    elif isinstance(data, memoryview):
        raw = data.tobytes()
    else:
        raise TypeError(f"Expected str or bytes-like object, got {type(data).__name__}")

    n = len(raw)
    if n == 0:
        return int(get_lib().ss_hash_provenance(None, 0))
    buf = (c_uint8 * n).from_buffer_copy(raw)
    return int(get_lib().ss_hash_provenance(buf, n))


# ---------------------------------------------------------------------------
# Zero-copy bulk double buffer helpers (zero third-party imports!)
# ---------------------------------------------------------------------------


def _is_c_contiguous_f64_strides(shape: tuple[int, ...], strides: Any) -> bool:
    if strides is None:
        return True
    if not isinstance(strides, tuple) or len(strides) != len(shape):
        return False
    expected = 8
    for dim, st in zip(reversed(shape), reversed(strides)):
        if dim > 1 and st != expected:
            return False
        expected *= dim
    return True


def _extract_array_interface(
    obj: Any, expected_ndim: int | None = None
) -> tuple[Any, tuple[int, ...], Any] | None:
    """Duck-type NumPy-compatible arrays via ``__array_interface__`` (no numpy import!).

    Returns ``(ptr, shape, owner)`` for zero-copy C-contiguous native float64 arrays,
    or ``None`` if the object does not expose a compatible zero-copy interface.
    """
    ai = getattr(obj, "__array_interface__", None)
    if not isinstance(ai, dict):
        return None
    typestr = ai.get("typestr")
    shape = ai.get("shape")
    data_info = ai.get("data")
    strides = ai.get("strides")
    if (
        typestr not in _NATIVE_F8_TYPESTRS
        or not isinstance(shape, tuple)
        or not isinstance(data_info, tuple)
        or len(data_info) < 1
    ):
        return None
    if expected_ndim is not None and len(shape) != expected_ndim:
        return None
    if not _is_c_contiguous_f64_strides(shape, strides):
        return None
    addr = data_info[0]
    if not isinstance(addr, int):
        return None
    total = 1
    for dim in shape:
        total *= int(dim)
    if total == 0:
        return (None, tuple(int(d) for d in shape), obj)
    if addr == 0:
        return None
    return (ctypes.cast(addr, _DOUBLE_PTR), tuple(int(d) for d in shape), obj)


def _memoryview_to_double_ptr(mv: memoryview) -> tuple[Any, int, Any]:
    """Extract a ``double*`` from a C-contiguous ``'d'`` memoryview."""
    n = mv.nbytes // 8
    if n == 0:
        return (None, 0, mv)
    byte_view = mv.cast("B", shape=(mv.nbytes,))
    if not mv.readonly:
        raw_byte = c_char.from_buffer(byte_view)
        addr = ctypes.addressof(raw_byte)
        return (ctypes.cast(addr, _DOUBLE_PTR), n, (mv, byte_view, raw_byte))
    # Read-only memoryview: single C memcpy into array('d')
    arr = array("d")
    arr.frombytes(byte_view)
    addr, length = arr.buffer_info()
    return (ctypes.cast(addr, _DOUBLE_PTR), length, arr)


def as_double_ptr(
    values: Any, *, allow_none_as_nan: bool = False
) -> tuple[Any, int, Any]:
    """Convert a 1D numeric buffer/sequence into ``(double*, length, owner)``.

    Zero-copy for:
      - ``array('d')``
      - writable C-contiguous ``memoryview`` with format ``'d'``
      - ``ctypes.Array`` of ``c_double``
      - duck-typed 1D C-contiguous native ``float64`` NumPy arrays (no numpy import)
    """
    if isinstance(values, array) and values.typecode == "d":
        addr, length = values.buffer_info()
        return (ctypes.cast(addr, _DOUBLE_PTR) if length else None, length, values)

    if isinstance(values, ctypes.Array) and getattr(values, "_type_", None) is c_double:
        length = len(values)
        return (ctypes.cast(values, _DOUBLE_PTR) if length else None, length, values)

    if isinstance(values, memoryview):
        if values.format == "d" and values.ndim == 1 and values.c_contiguous:
            return _memoryview_to_double_ptr(values)
        if values.ndim != 1:
            raise ValueError(f"Expected 1D buffer, got {values.ndim}D memoryview")
        arr = array("d", (float(x) for x in values))
        addr, length = arr.buffer_info()
        return (ctypes.cast(addr, _DOUBLE_PTR) if length else None, length, arr)

    extracted = _extract_array_interface(values, expected_ndim=1)
    if extracted is not None:
        ptr, shape, owner = extracted
        return (ptr, shape[0], owner)

    # Non-contiguous or non-float64 array-interface object
    ai = getattr(values, "__array_interface__", None)
    if isinstance(ai, dict):
        shape = ai.get("shape", ())
        if len(shape) != 1:
            raise ValueError(f"Expected 1D array, got shape {shape}")
        astype = getattr(values, "astype", None)
        if callable(astype):
            target_dtype = "<f8" if sys.byteorder == "little" else ">f8"
            converted = astype(target_dtype, order="C", copy=False)
            extracted2 = _extract_array_interface(converted, expected_ndim=1)
            if extracted2 is not None:
                ptr, shape2, owner2 = extracted2
                return (ptr, shape2[0], owner2)

    if allow_none_as_nan:
        arr = array(
            "d", (math.nan if x is None else float(x) for x in values)
        )
    else:
        arr = array("d", values)
    addr, length = arr.buffer_info()
    return (ctypes.cast(addr, _DOUBLE_PTR) if length else None, length, arr)


def as_double_matrix(
    matrix: Any, expected_cols: int | None = None
) -> tuple[Any, int, int, Any]:
    """Convert a 2D numeric matrix (or flat row-major buffer) into ``(double*, rows, cols, owner)``.

    Zero-copy for:
      - 2D C-contiguous native ``float64`` duck-typed NumPy arrays
      - 2D C-contiguous writable ``memoryview`` of ``'d'``
      - 1D ``array('d')`` / ``memoryview`` / ``ctypes`` array when ``expected_cols`` is given
    """
    # 1. Duck-typed 2D NumPy / array-interface buffer
    extracted = _extract_array_interface(matrix, expected_ndim=2)
    if extracted is not None:
        ptr, shape, owner = extracted
        rows, cols = shape
        if expected_cols is not None and cols != expected_cols:
            raise ValueError(f"Expected {expected_cols} channels, got {cols}")
        return (ptr, rows, cols, owner)

    # Duck-typed 1D array-interface buffer if expected_cols is known
    if expected_cols is not None:
        extracted1 = _extract_array_interface(matrix, expected_ndim=1)
        if extracted1 is not None:
            ptr, shape1, owner = extracted1
            total = shape1[0]
            if expected_cols <= 0 or total % expected_cols != 0:
                raise ValueError(
                    f"1D buffer length {total} is not divisible by {expected_cols} channels"
                )
            return (ptr, total // expected_cols, expected_cols, owner)

    # Non-contiguous or non-float64 array-interface object (e.g., float32 or transposed ndarray)
    ai = getattr(matrix, "__array_interface__", None)
    if isinstance(ai, dict):
        shape = ai.get("shape", ())
        if len(shape) == 2:
            astype = getattr(matrix, "astype", None)
            if callable(astype):
                target_dtype = "<f8" if sys.byteorder == "little" else ">f8"
                converted = astype(target_dtype, order="C", copy=False)
                extracted2 = _extract_array_interface(converted, expected_ndim=2)
                if extracted2 is not None:
                    ptr, shape2, owner2 = extracted2
                    rows, cols = shape2
                    if expected_cols is not None and cols != expected_cols:
                        raise ValueError(f"Expected {expected_cols} channels, got {cols}")
                    return (ptr, rows, cols, owner2)
        elif len(shape) != 2:
            raise ValueError(f"Expected 2D matrix, got shape {shape}")

    # 2. memoryview (2D or flat 1D when expected_cols is given)
    if isinstance(matrix, memoryview):
        if matrix.ndim == 2 and matrix.format == "d" and matrix.c_contiguous:
            rows, cols = matrix.shape
            if expected_cols is not None and cols != expected_cols:
                raise ValueError(f"Expected {expected_cols} channels, got {cols}")
            ptr, _n, owner = _memoryview_to_double_ptr(matrix)
            return (ptr, rows, cols, owner)
        if matrix.ndim == 1 and expected_cols is not None and matrix.format == "d" and matrix.c_contiguous:
            ptr, total, owner = _memoryview_to_double_ptr(matrix)
            if expected_cols <= 0 or total % expected_cols != 0:
                raise ValueError(
                    f"1D memoryview length {total} is not divisible by {expected_cols} channels"
                )
            return (ptr, total // expected_cols, expected_cols, owner)

    # 3. Flat 1D array('d') or ctypes c_double array when expected_cols is known
    if expected_cols is not None and (
        (isinstance(matrix, array) and matrix.typecode == "d")
        or (isinstance(matrix, ctypes.Array) and getattr(matrix, "_type_", None) is c_double)
    ):
        ptr, total, owner = as_double_ptr(matrix)
        if expected_cols <= 0 or total % expected_cols != 0:
            raise ValueError(
                f"1D buffer length {total} is not divisible by {expected_cols} channels"
            )
        return (ptr, total // expected_cols, expected_cols, owner)

    # 4. General Python iterable of rows (list[list[float]], tuple of arrays, etc.)
    flat = array("d")
    n_rows = 0
    cols = expected_cols
    for row in matrix:
        if isinstance(row, array) and row.typecode == "d":
            row_len = len(row)
            if cols is None:
                cols = row_len
            elif row_len != cols:
                raise ValueError(f"Row {n_rows} has {row_len} values; expected {cols}")
            flat.extend(row)
        else:
            row_list = list(row)
            row_len = len(row_list)
            if cols is None:
                cols = row_len
            elif row_len != cols:
                raise ValueError(f"Row {n_rows} has {row_len} values; expected {cols}")
            flat.extend(row_list)
        n_rows += 1

    if cols is None:
        cols = 0
    addr, total = flat.buffer_info()
    return (ctypes.cast(addr, _DOUBLE_PTR) if total else None, n_rows, cols, flat)


def as_uint8_ptr(data: bytes | bytearray | memoryview) -> tuple[Any, int, Any]:
    """Convert a bytes-like object to ``(uint8*, length, owner)``."""
    if isinstance(data, bytearray):
        n = len(data)
        if n == 0:
            return (None, 0, data)
        buf = (c_uint8 * n).from_buffer(data)
        return (ctypes.cast(buf, _UINT8_PTR), n, (data, buf))
    if isinstance(data, memoryview):
        raw = data.tobytes()
    elif isinstance(data, bytes):
        raw = data
    else:
        raise TypeError(f"Expected bytes-like object, got {type(data).__name__}")
    n = len(raw)
    if n == 0:
        return (None, 0, raw)
    buf = (c_uint8 * n).from_buffer_copy(raw)
    return (ctypes.cast(buf, _UINT8_PTR), n, buf)


def validate_uint64(value: int, name: str = "id") -> int:
    """Validate an unsigned 64-bit integer argument."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0 or value >= (1 << 64):
        raise ValueError(f"{name} must be in [0, 2^64 - 1], got {value}")
    return value


def validate_uint32(value: int, name: str = "id") -> int:
    """Validate an unsigned 32-bit integer argument."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0 or value >= (1 << 32):
        raise ValueError(f"{name} must be in [0, 2^32 - 1], got {value}")
    return value
