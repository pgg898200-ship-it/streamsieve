"""Zero-echo provenance-deduplicated CRDT belief & e-value fusion (EvidenceLattice)."""

from __future__ import annotations

import base64
from ctypes import byref, c_int, c_size_t, c_uint8, c_uint32, c_uint64
from dataclasses import dataclass
import struct
from typing import Any

from ._native import (
    SSEvidenceItem,
    SSHypSummary,
    SS_ERR_INVALID_ARG,
    SS_ERR_PARSE,
    SS_MERGE_MAX_RELIABILITY,
    SS_MERGE_STRICT_ORIGIN,
    StreamSieveError,
    as_uint8_ptr,
    check_status,
    get_lib,
    hash_provenance,
    validate_uint32,
    validate_uint64,
)

_MERGE_MODE_MAP = {
    "strict_origin": SS_MERGE_STRICT_ORIGIN,
    "max_reliability": SS_MERGE_MAX_RELIABILITY,
    SS_MERGE_STRICT_ORIGIN: SS_MERGE_STRICT_ORIGIN,
    SS_MERGE_MAX_RELIABILITY: SS_MERGE_MAX_RELIABILITY,
}

_MERGE_MODE_NAMES = {
    SS_MERGE_STRICT_ORIGIN: "strict_origin",
    SS_MERGE_MAX_RELIABILITY: "max_reliability",
}


@dataclass(frozen=True)
class HypSummary:
    """Deduplicated posterior and Vovk-Wang merged e-value summary for a hypothesis."""

    hyp_id: int
    hyp_label: str | None
    posterior_prob: float
    total_log_odds: float
    merged_e_value: float
    merged_log_e: float
    unique_evidence: int
    echoes_suppressed: int
    distinct_agents: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize summary into a JSON-ready dictionary."""
        return {
            "hyp_id": self.hyp_id,
            "hyp_label": self.hyp_label,
            "posterior_prob": self.posterior_prob,
            "total_log_odds": self.total_log_odds,
            "merged_e_value": self.merged_e_value,
            "merged_log_e": self.merged_log_e,
            "unique_evidence": self.unique_evidence,
            "echoes_suppressed": self.echoes_suppressed,
            "distinct_agents": self.distinct_agents,
        }


class EvidenceLattice:
    """Order-independent CRDT join-semilattice for echo-free multi-agent belief fusion.

    Supports both ``str`` and ``int`` hypothesis/provenance identifiers (strings are
    deterministically hashed with ``ss_hash_provenance`` while keeping an in-memory
    reverse label map).
    """

    def __init__(
        self,
        merge_mode: str | int = "strict_origin",
        *,
        _raw_ptr: int | None = None,
    ) -> None:
        self._ptr: int | None = None
        self._lib = get_lib()

        if merge_mode not in _MERGE_MODE_MAP:
            raise ValueError(
                f"Invalid merge_mode {merge_mode!r}; expected 'strict_origin' or 'max_reliability'"
            )
        self._mode_code = _MERGE_MODE_MAP[merge_mode]
        self._hyp_labels: dict[int, str] = {}
        self._prov_labels: dict[int, str] = {}

        if _raw_ptr is not None:
            self._ptr = _raw_ptr
        else:
            ptr = self._lib.ss_lattice_create(self._mode_code)
            if not ptr:
                raise StreamSieveError(SS_ERR_INVALID_ARG, "ss_lattice_create failed")
            self._ptr = ptr

    def _require_open(self) -> int:
        if not self._ptr:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "EvidenceLattice has been closed")
        return self._ptr

    def close(self) -> None:
        """Explicitly release the underlying native lattice."""
        ptr = getattr(self, "_ptr", None)
        if ptr:
            self._ptr = None
            self._lib.ss_lattice_destroy(ptr)

    def __enter__(self) -> EvidenceLattice:
        self._require_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @property
    def merge_mode(self) -> str:
        """CRDT duplicate-provenance tie-break mode."""
        return _MERGE_MODE_NAMES[self._mode_code]

    @staticmethod
    def hash_id(value: str | bytes | bytearray | memoryview) -> int:
        """Deterministically hash a string/bytes identifier via ``ss_hash_provenance``."""
        return hash_provenance(value)

    def _resolve_id(
        self,
        value: str | int,
        label_map: dict[int, str],
        *,
        record_label: bool = True,
        name: str = "id",
    ) -> int:
        if isinstance(value, str):
            hashed = hash_provenance(value)
            if record_label:
                label_map[hashed] = value
            return hashed
        if isinstance(value, (bytes, bytearray, memoryview)):
            return hash_provenance(value)
        return validate_uint64(value, name)

    def set_prior(self, hyp: str | int, prior_log_odds: float) -> None:
        """Set the prior log-odds for a hypothesis."""
        ptr = self._require_open()
        hid = self._resolve_id(hyp, self._hyp_labels, name="hyp")
        rc = self._lib.ss_lattice_set_prior(ptr, hid, float(prior_log_odds))
        check_status(rc, "ss_lattice_set_prior")

    def set_agent_weight(self, agent_id: int, weight: float) -> None:
        """Set a non-negative trust weight for an agent."""
        ptr = self._require_open()
        aid = validate_uint32(agent_id, "agent_id")
        rc = self._lib.ss_lattice_set_agent_weight(ptr, aid, float(weight))
        check_status(rc, "ss_lattice_set_agent_weight")

    def observe(
        self,
        hyp: str | int,
        provenance_id: str | int,
        llr: float,
        log_e: float | None = None,
        agent_id: int = 0,
        reliability: float = 1.0,
        timestamp_ns: int = 0,
    ) -> bool:
        """Ingest one evidence item. Returns ``True`` if ``provenance_id`` was an echo.

        When ``log_e`` is ``None``, defaults to ``float(llr)`` (exact likelihood-ratio
        e-value under simple null). ``reliability`` in ``[0.0, 1.0]`` is quantized to
        ``reliability_q16`` in ``[0, 65536]``.
        """
        ptr = self._require_open()
        hid = self._resolve_id(hyp, self._hyp_labels, name="hyp")
        pid = self._resolve_id(provenance_id, self._prov_labels, name="provenance_id")
        aid = validate_uint32(agent_id, "agent_id")
        ts = validate_uint64(timestamp_ns, "timestamp_ns")

        llr_f = float(llr)
        log_e_f = llr_f if log_e is None else float(log_e)
        rel_clamped = max(0.0, min(1.0, float(reliability)))
        rel_q16 = int(round(rel_clamped * 65536.0))

        item = SSEvidenceItem(
            hyp_id=hid,
            provenance_id=pid,
            llr=llr_f,
            log_e=log_e_f,
            agent_id=aid,
            reliability_q16=rel_q16,
            timestamp_ns=ts,
        )
        echo_flag = c_int(0)
        rc = self._lib.ss_lattice_observe(ptr, byref(item), byref(echo_flag))
        check_status(rc, "ss_lattice_observe")
        return bool(echo_flag.value)

    def merge(self, other: EvidenceLattice) -> EvidenceLattice:
        """In-place CRDT join: ``self = merge(self, other)``.

        Strictly commutative, associative, and idempotent. Returns ``self``.
        """
        if not isinstance(other, EvidenceLattice):
            raise TypeError(f"Expected EvidenceLattice, got {type(other).__name__}")
        dst_ptr = self._require_open()
        src_ptr = other._require_open()
        rc = self._lib.ss_lattice_merge(dst_ptr, src_ptr)
        check_status(rc, "ss_lattice_merge")
        for k, v in other._hyp_labels.items():
            self._hyp_labels.setdefault(k, v)
        for k, v in other._prov_labels.items():
            self._prov_labels.setdefault(k, v)
        return self

    def merged(self, other: EvidenceLattice) -> EvidenceLattice:
        """Return a new :class:`EvidenceLattice` containing ``merge(self, other)``.

        Neither ``self`` nor ``other`` is mutated.
        """
        if not isinstance(other, EvidenceLattice):
            raise TypeError(f"Expected EvidenceLattice, got {type(other).__name__}")
        clone = EvidenceLattice.from_bytes(self.to_bytes())
        clone._hyp_labels.update(self._hyp_labels)
        clone._prov_labels.update(self._prov_labels)
        clone.merge(other)
        return clone

    def query(self, hyp: str | int) -> HypSummary:
        """Query the deduplicated belief posterior and Vovk-Wang merged e-value."""
        ptr = self._require_open()
        hid = self._resolve_id(hyp, self._hyp_labels, record_label=False, name="hyp")
        out = SSHypSummary()
        rc = self._lib.ss_lattice_query(ptr, hid, byref(out))
        check_status(rc, "ss_lattice_query")

        label = self._hyp_labels.get(hid)
        if label is None and isinstance(hyp, str):
            label = hyp
        return HypSummary(
            hyp_id=int(out.hyp_id),
            hyp_label=label,
            posterior_prob=float(out.posterior_prob),
            total_log_odds=float(out.total_log_odds),
            merged_e_value=float(out.merged_e_value),
            merged_log_e=float(out.merged_log_e),
            unique_evidence=int(out.unique_evidence),
            echoes_suppressed=int(out.echoes_suppressed),
            distinct_agents=int(out.distinct_agents),
        )

    def list_hyps(self, *, resolve_labels: bool = True) -> list[str | int]:
        """List all tracked hypothesis identifiers (resolved to string labels if known)."""
        ptr = self._require_open()
        count = c_uint32(0)
        rc = self._lib.ss_lattice_list_hyps(ptr, None, 0, byref(count))
        check_status(rc, "ss_lattice_list_hyps")
        n = int(count.value)
        if n == 0:
            return []
        buf = (c_uint64 * n)()
        rc = self._lib.ss_lattice_list_hyps(ptr, buf, n, byref(count))
        check_status(rc, "ss_lattice_list_hyps")
        raw_ids = [int(buf[i]) for i in range(min(n, int(count.value)))]
        if resolve_labels:
            return [self._hyp_labels.get(h, h) for h in raw_ids]
        return raw_ids

    def to_bytes(self) -> bytes:
        """Serialize lattice state into the compact binary ``SSLT`` wire format."""
        ptr = self._require_open()
        req = c_size_t(0)
        rc = self._lib.ss_lattice_serialize(ptr, None, 0, byref(req))
        check_status(rc, "ss_lattice_serialize(size)")
        size = int(req.value)
        buf = (c_uint8 * size)()
        written = c_size_t(0)
        rc = self._lib.ss_lattice_serialize(ptr, buf, size, byref(written))
        check_status(rc, "ss_lattice_serialize(write)")
        return bytes(buf[: int(written.value)])

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> EvidenceLattice:
        """Deserialize an :class:`EvidenceLattice` from binary ``SSLT`` wire bytes."""
        lib = get_lib()
        buf_ptr, length, _owner = as_uint8_ptr(data)
        if length < 16 or buf_ptr is None:
            raise StreamSieveError(SS_ERR_PARSE, "truncated or empty lattice wire payload")
        raw_ptr = lib.ss_lattice_deserialize(buf_ptr, length)
        if not raw_ptr:
            raise StreamSieveError(SS_ERR_PARSE, "invalid or corrupted lattice wire payload")
        # Header layout: magic(4) + ver(4) + mode(4)
        raw_head = bytes(data[:12])
        mode_val = struct.unpack("<I", raw_head[8:12])[0]
        mode_name = _MERGE_MODE_NAMES.get(mode_val, "strict_origin")
        return cls(merge_mode=mode_name, _raw_ptr=raw_ptr)

    def to_base85(self) -> str:
        """Serialize lattice state as an ASCII Base85 string for LLM/agent wire sync."""
        return base64.b85encode(self.to_bytes()).decode("ascii")

    @classmethod
    def from_base85(cls, text: str) -> EvidenceLattice:
        """Deserialize an :class:`EvidenceLattice` from a Base85 wire string."""
        if not isinstance(text, str):
            raise TypeError(f"Expected str, got {type(text).__name__}")
        try:
            raw = base64.b85decode(text.strip().encode("ascii"))
        except Exception as exc:
            raise StreamSieveError(SS_ERR_PARSE, f"invalid base85 lattice payload: {exc}") from exc
        return cls.from_bytes(raw)
