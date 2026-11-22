"""Hierarchical multi-agent Value-of-Information firehose sifter (VOISifter & sift_matrix)."""

from __future__ import annotations

from array import array
import ctypes
from ctypes import byref, c_double, c_uint32
from dataclasses import dataclass
from typing import Any, Sequence

from ._native import (
    SSSifterConfig,
    SSVOIAction,
    SS_ERR_INVALID_ARG,
    StreamSieveError,
    as_double_matrix,
    as_double_ptr,
    check_status,
    get_lib,
    validate_uint32,
)
from .monitor import EStream, StreamBrief

_TOOL_KIND_NAMES = {
    0: "site_agg",
    1: "channel_mean",
    2: "channel_ticks",
}


@dataclass(frozen=True)
class VOIAction:
    """A single attention read action allocated by :class:`VOISifter`."""

    agent_slot: int
    tool_kind: str
    tool_kind_code: int
    target_id: int
    cost: int
    expected_voi: float
    current_prob: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize action into a compact JSON-ready dictionary."""
        return {
            "agent_slot": self.agent_slot,
            "tool_kind": self.tool_kind,
            "tool_kind_code": self.tool_kind_code,
            "target_id": self.target_id,
            "cost": self.cost,
            "expected_voi": self.expected_voi,
            "current_prob": self.current_prob,
        }


class VOISifter:
    """Hierarchical Bayesian Value-of-Information multi-agent attention allocator."""

    def __init__(
        self,
        n_channels: int,
        n_sites: int = 1,
        *,
        prior_active: float = 0.02,
        hot_site_prior: float = 0.10,
        hot_site_boost: float = 0.25,
        shift_snr: float = 1.5,
        burst_prob: float = 0.15,
        burst_snr: float = 2.5,
        ticks_per_epoch: int = 8,
    ) -> None:
        self._ptr: int | None = None
        self._lib = get_lib()

        n_ch = validate_uint32(n_channels, "n_channels")
        n_st = validate_uint32(n_sites, "n_sites")
        if n_ch == 0:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "n_channels must be >= 1")
        if n_st == 0:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "n_sites must be >= 1")

        cfg = SSSifterConfig()
        self._lib.ss_sifter_default_config(byref(cfg), n_ch, n_st)
        cfg.prior_active = float(prior_active)
        cfg.hot_site_prior = float(hot_site_prior)
        cfg.hot_site_boost = float(hot_site_boost)
        cfg.shift_snr = float(shift_snr)
        cfg.burst_prob = float(burst_prob)
        cfg.burst_snr = float(burst_snr)
        cfg.ticks_per_epoch = validate_uint32(ticks_per_epoch, "ticks_per_epoch")

        ptr = self._lib.ss_sifter_create(byref(cfg))
        if not ptr:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "ss_sifter_create failed")

        self._ptr = ptr
        self._n_channels = n_ch
        self._n_sites = n_st
        self._ticks_per_epoch = int(cfg.ticks_per_epoch)

    def _require_open(self) -> int:
        if not self._ptr:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "VOISifter has been closed")
        return self._ptr

    def close(self) -> None:
        """Explicitly release the underlying native sifter."""
        ptr = getattr(self, "_ptr", None)
        if ptr:
            self._ptr = None
            self._lib.ss_sifter_destroy(ptr)

    def __enter__(self) -> VOISifter:
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
    def n_channels(self) -> int:
        return self._n_channels

    @property
    def n_sites(self) -> int:
        return self._n_sites

    @property
    def ticks_per_epoch(self) -> int:
        return self._ticks_per_epoch

    def set_noise_scales(self, scales: Any) -> None:
        """Set per-channel noise standard deviations (enabling loud-channel immunity)."""
        ptr = self._require_open()
        buf_ptr, length, _owner = as_double_ptr(scales)
        if length != self._n_channels:
            raise StreamSieveError(
                SS_ERR_INVALID_ARG,
                f"set_noise_scales expected {self._n_channels} values, got {length}",
            )
        rc = self._lib.ss_sifter_set_noise_scales(ptr, buf_ptr, length)
        check_status(rc, "ss_sifter_set_noise_scales")

    def observe_site_agg(self, site_id: int, value: float) -> None:
        """Feed a standardized aggregate observation for a site."""
        ptr = self._require_open()
        sid = validate_uint32(site_id, "site_id")
        rc = self._lib.ss_sifter_observe_site_agg(ptr, sid, float(value))
        check_status(rc, "ss_sifter_observe_site_agg")

    def observe_mean(self, channel: int, value: float) -> None:
        """Feed an epoch mean observation for a single channel."""
        ptr = self._require_open()
        ch = validate_uint32(channel, "channel")
        rc = self._lib.ss_sifter_observe_mean(ptr, ch, float(value))
        check_status(rc, "ss_sifter_observe_mean")

    def observe_ticks(self, channel: int, ticks: Any) -> None:
        """Feed a raw tick array for a single channel (zero-copy for float64 buffers)."""
        ptr = self._require_open()
        ch = validate_uint32(channel, "channel")
        buf_ptr, length, _owner = as_double_ptr(ticks)
        if length == 0 or buf_ptr is None:
            raise StreamSieveError(SS_ERR_INVALID_ARG, "ticks must be non-empty")
        rc = self._lib.ss_sifter_observe_ticks(ptr, ch, buf_ptr, length)
        check_status(rc, "ss_sifter_observe_ticks")

    def allocate(
        self,
        budget_per_agent: int,
        n_agents: int = 1,
        allow_site_agg: bool = True,
        allow_mean: bool = True,
        allow_ticks: bool = True,
    ) -> list[VOIAction]:
        """Greedily allocate non-overlapping high-VOI read actions across ``n_agents``."""
        ptr = self._require_open()
        budget = validate_uint32(budget_per_agent, "budget_per_agent")
        agents = validate_uint32(n_agents, "n_agents")
        if budget == 0 or agents == 0:
            return []

        max_out = max(1, budget * agents)
        out_buf = (SSVOIAction * max_out)()
        out_count = c_uint32(0)
        rc = self._lib.ss_sifter_allocate(
            ptr,
            budget,
            agents,
            1 if allow_site_agg else 0,
            1 if allow_mean else 0,
            1 if allow_ticks else 0,
            out_buf,
            max_out,
            byref(out_count),
        )
        check_status(rc, "ss_sifter_allocate")

        actions: list[VOIAction] = []
        for i in range(out_count.value):
            raw = out_buf[i]
            code = int(raw.tool_kind)
            actions.append(
                VOIAction(
                    agent_slot=int(raw.agent_slot),
                    tool_kind=_TOOL_KIND_NAMES.get(code, f"unknown_{code}"),
                    tool_kind_code=code,
                    target_id=int(raw.target_id),
                    cost=int(raw.cost),
                    expected_voi=float(raw.expected_voi),
                    current_prob=float(raw.current_prob),
                )
            )
        return actions

    def posteriors(self) -> list[float]:
        """Return current posterior ``P(channel active)`` for all channels."""
        ptr = self._require_open()
        out = array("d", [0.0]) * self._n_channels
        addr, _ = out.buffer_info()
        rc = self._lib.ss_sifter_posteriors(
            ptr, ctypes.cast(addr, ctypes.POINTER(c_double)), self._n_channels
        )
        check_status(rc, "ss_sifter_posteriors")
        return list(out)

    def step_epoch(self, stay_prob: float = 0.99) -> None:
        """Advance one epoch, decaying posteriors gently toward the prior."""
        ptr = self._require_open()
        rc = self._lib.ss_sifter_step_epoch(ptr, float(stay_prob))
        check_status(rc, "ss_sifter_step_epoch")


def sift_matrix(
    matrix: Any,
    names: Sequence[str] | None = None,
    alpha: float = 0.05,
    warmup: int = 32,
    max_channels: int = 10,
    ring_capacity: int = 256,
    *,
    season_period: int = 0,
    detrend_alpha: float = 0.0,
) -> tuple[EStream, StreamBrief]:
    """One-line firehose sifter: ingest a multivariate stream and return ``(EStream, StreamBrief)``.

    Agents can inspect ``brief.to_compact_text()`` and drill down into exact raw ticks
    losslessly via ``estream.fetch_raw(handle)``.
    """
    buf_ptr, n_ticks, n_cols, owner = as_double_matrix(matrix)
    if n_cols <= 0:
        raise ValueError("matrix must have at least one channel (non-empty 2D numeric data)")

    est = EStream(
        n_cols,
        warmup=warmup,
        ring_capacity=ring_capacity,
        alpha=alpha,
        season_period=season_period,
        detrend_alpha=detrend_alpha,
        names=names,
    )
    if n_ticks > 0:
        rc = est._lib.ss_estream_update_batch(est._require_open(), buf_ptr, n_ticks, n_cols)
        check_status(rc, "ss_estream_update_batch")
    # Keep owner alive until batch update completes
    _ = owner
    brief = est.brief(max_channels=max_channels, names=names, alpha=alpha)
    return (est, brief)
